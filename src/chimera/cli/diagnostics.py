"""Operator-oriented diagnosis and rendering of expected CLI failures.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from typing import Literal

from chimera.cli.client import ChimeraClient, ClientError
from chimera.runtime import resolve_runtime_paths
from chimera.utils.unixsocket import inspect_unix_socket

OutputFormat = Literal["table", "json"]


def diagnose_client_error(error: ClientError) -> ClientError:
    """Add context for server availability without mixing local and remote hosts."""
    if error.transport == "tls":
        return _diagnose_remote_client_error(error)
    generic_doctor_suggestion = bool(error.suggestion and "chimeractl doctor" in error.suggestion)
    if error.code in {"server_unavailable", "server_unreachable"} and (
        not error.suggestion or generic_doctor_suggestion
    ):
        return _copy_client_error(error, suggestion=_server_start_suggestion())
    if error.code == "permission_denied" and not error.suggestion:
        return _copy_client_error(
            error,
            suggestion=(
                "Ask a system administrator to add you to chimera-admin. "
                "That group is root-equivalent for container administration."
            ),
        )
    return error


def _copy_client_error(error: ClientError, *, suggestion: str | None = None) -> ClientError:
    """Rebuild a ClientError without dropping transport or timeout-stage metadata."""
    return ClientError(
        error.code,
        error.message,
        detail=error.detail,
        suggestion=error.suggestion if suggestion is None else suggestion,
        transport=error.transport,
        stage=error.stage,
    )


def _diagnose_remote_client_error(error: ClientError) -> ClientError:
    """Keep remote failures on the remote target; never probe the local server."""
    suggestion = error.suggestion
    looks_local = bool(
        suggestion
        and ("chimeractl doctor" in suggestion or "systemctl start chimera-server" in suggestion)
    )
    if error.code == "tls_handshake_failed":
        suggestion = (
            "Check the remote host identity and the --tls-ca, --tls-cert, and --tls-key files."
        )
    elif error.code in {"server_unavailable", "server_unreachable", "timeout"} and (
        not suggestion or looks_local
    ):
        suggestion = (
            "Confirm the remote host, port, TLS credentials, and that the remote server "
            "is listening. This error is not about the local chimera-server."
        )
    elif looks_local:
        suggestion = (
            "Inspect the remote server TLS configuration and journal. "
            "Do not start the local chimera-server for a remote --host failure."
        )
    return _copy_client_error(error, suggestion=suggestion)


def render_error(error: ClientError, output_format: OutputFormat) -> str:
    """Render a stable JSON error or concise human diagnostic."""
    error = diagnose_client_error(error)
    payload = {
        "success": False,
        "error": {
            "code": error.code,
            "message": error.message,
            "detail": error.detail,
            "suggestion": error.suggestion,
        },
    }
    if output_format == "json":
        return json.dumps(payload, sort_keys=True)

    lines = [f"Error: {error.message}"]
    if error.detail:
        lines.append(f"Cause: {error.detail}")
    if error.suggestion:
        lines.append(f"Next: {error.suggestion}")
    return "\n".join(lines)


def _server_start_suggestion() -> str:
    """Explain how to recover an unavailable installed server."""
    unit_exists = any(
        os.path.exists(path)
        for path in (
            "/etc/systemd/system/chimera-server.service",
            "/lib/systemd/system/chimera-server.service",
            "/usr/lib/systemd/system/chimera-server.service",
        )
    )
    if unit_exists and shutil.which("systemctl"):
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", "chimera-server"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return "The Chimera server service is installed but stopped. Start it with: sudo systemctl start chimera-server"

    return (
        "Install and configure chimera-spawn-server, then start the packaged "
        "chimera-server service."
    )


def _tls_file_check(path: str | None) -> dict[str, object]:
    """Report whether a TLS file is readable without exposing its contents."""
    info: dict[str, object] = {"path": path, "exists": False, "readable": False}
    if not path:
        info["error"] = "not configured"
        return info
    try:
        info["exists"] = os.path.exists(path)
        info["readable"] = os.access(path, os.R_OK)
    except OSError as error:
        info["error"] = str(error)
    return info


def _remote_connection_flags(error: ClientError) -> dict[str, bool]:
    """Keep TLS success distinct from a later remote application failure."""
    if error.code == "timeout" and error.stage == "request":
        return {
            "tls": True,
            "server_certificate": True,
            "client_authentication": True,
            "api_reachable": False,
        }
    if error.code in {
        "invalid_argument",
        "invalid_configuration",
        "tls_handshake_failed",
        "server_unavailable",
        "server_unreachable",
        "timeout",
    }:
        return {
            "tls": False,
            "server_certificate": False,
            "client_authentication": False,
            "api_reachable": False,
        }
    return {
        "tls": True,
        "server_certificate": True,
        "client_authentication": True,
        "api_reachable": True,
    }


def remote_doctor(client: ChimeraClient, *, timeout: float | None = None) -> dict[str, object]:
    """Diagnose a remote TLS client without inspecting the local host server."""
    connection = {
        "tls": False,
        "server_certificate": False,
        "client_authentication": False,
        "api_reachable": False,
    }
    result: dict[str, object] = {
        "mode": "remote",
        "healthy": False,
        "checks": {
            "target": {"host": client.remote_host, "port": client.remote_port},
            "tls_ca": _tls_file_check(client.tls_ca),
            "tls_cert": _tls_file_check(client.tls_cert),
            "tls_key": _tls_file_check(client.tls_key),
            "connection": connection,
        },
        "server": None,
        "server_error": None,
    }
    try:
        server_result = client.request("doctor", timeout=timeout)
    except ClientError as error:
        diagnosed = diagnose_client_error(error)
        connection_flags = _remote_connection_flags(diagnosed)
        connection.update(connection_flags)
        result["server_error"] = {
            "code": diagnosed.code,
            "message": diagnosed.message,
            "detail": diagnosed.detail,
            "suggestion": diagnosed.suggestion,
        }
        return result
    connection["tls"] = True
    connection["server_certificate"] = True
    connection["client_authentication"] = True
    connection["api_reachable"] = True
    result["server"] = server_result
    result["healthy"] = bool(server_result.get("healthy"))
    return result


def local_doctor(socket_path: str | None = None) -> dict[str, object]:
    """Collect host diagnostics without requiring a server or socket connection."""
    paths = resolve_runtime_paths(socket_path=socket_path)
    socket = paths.socket_path
    socket_info: dict[str, object] = {
        "path": str(socket),
        "exists": False,
        "is_socket": False,
        "accessible": False,
        "mode": None,
        "owner": None,
    }
    try:
        socket_stat = os.lstat(socket)
    except FileNotFoundError:
        pass
    except OSError as error:
        socket_info["error"] = str(error)
    else:
        socket_info.update(
            {
                "exists": True,
                "is_socket": stat.S_ISSOCK(socket_stat.st_mode),
                "accessible": os.access(socket, os.R_OK | os.W_OK),
                "mode": oct(stat.S_IMODE(socket_stat.st_mode)),
                "owner": f"{socket_stat.st_uid}:{socket_stat.st_gid}",
            }
        )

    socket_state = inspect_unix_socket(socket)
    socket_info["state"] = socket_state
    live_socket = socket_state == "live" and bool(socket_info["accessible"])

    unit_paths = (
        "/etc/systemd/system/chimera-server.service",
        "/lib/systemd/system/chimera-server.service",
        "/usr/lib/systemd/system/chimera-server.service",
    )
    unit_exists = any(os.path.exists(path) for path in unit_paths)
    unit_state = "not-installed"
    if unit_exists and shutil.which("systemctl"):
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "chimera-server"],
                check=False,
                capture_output=True,
                text=True,
            )
            unit_state = result.stdout.strip() or "unknown"
        except OSError as error:
            unit_state = f"error: {error}"

    service_ok = unit_exists and unit_state == "active"
    return {
        "mode": "installed",
        "healthy": bool(live_socket and service_ok),
        "checks": {
            "server_unit": {"installed": unit_exists, "state": unit_state},
            "socket": socket_info,
            "machinectl": shutil.which("machinectl") is not None,
            "systemctl": shutil.which("systemctl") is not None,
            "dbus_socket": os.path.exists("/run/dbus/system_bus_socket"),
            "config_directory": os.path.isdir(paths.config_dir),
            "state_directory": os.path.isdir(paths.state_dir),
            "machines_directory": os.path.isdir("/var/lib/machines"),
        },
    }
