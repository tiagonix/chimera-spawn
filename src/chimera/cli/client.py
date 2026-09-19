"""HTTP/WebSocket client for the Chimera server.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import os
import ssl
import urllib.parse
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import websockets

from chimera.endpoint import parse_remote_endpoint, remote_http_url, remote_ws_url
from chimera.errors import ChimeraError
from chimera.runtime import resolve_runtime_paths
from chimera.tls import build_client_ssl_context, resolve_client_tls_paths

TransportMode = Literal["unix", "tls"]
STREAM_OPEN_TIMEOUT = 30.0


class ClientError(Exception):
    """A structured failure returned by or encountered before the server API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: str | None = None,
        suggestion: str | None = None,
        transport: TransportMode | None = None,
        stage: Literal["connect", "request"] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.suggestion = suggestion
        self.transport = transport
        self.stage = stage

    @classmethod
    def from_chimera(
        cls, error: ChimeraError, *, transport: TransportMode | None = None
    ) -> ClientError:
        """Preserve structured errors raised before a connection is opened."""
        return cls(
            error.code,
            error.message,
            detail=error.detail,
            suggestion=error.suggestion,
            transport=transport,
        )


class ChimeraClient:
    """Communicate with the server over Unix HTTP/WS or remote HTTPS/WSS."""

    def __init__(
        self,
        socket_path: str | None = None,
        timeout: float = 30.0,
        *,
        host: str | None = None,
        tls_ca: str | None = None,
        tls_cert: str | None = None,
        tls_key: str | None = None,
    ):
        """Resolve a local socket or a remote TLS endpoint.

        Environment defaults: CHIMERA_HOST, CHIMERA_TLS_CA, CHIMERA_TLS_CERT,
        CHIMERA_TLS_KEY. CHIMERA_SOCKET is the local socket override.
        """
        self.timeout = timeout
        self.mode: TransportMode
        self.socket_path: Path | None = None
        self.remote_host: str | None = None
        self.remote_port: int | None = None
        self.ssl_context: ssl.SSLContext | None = None
        self.tls_ca = tls_ca or os.getenv("CHIMERA_TLS_CA")
        self.tls_cert = tls_cert or os.getenv("CHIMERA_TLS_CERT")
        self.tls_key = tls_key or os.getenv("CHIMERA_TLS_KEY")
        self.transport: httpx.BaseTransport | None = None

        cli_host = host
        if cli_host is not None and not str(cli_host).strip():
            raise ClientError(
                "invalid_argument",
                "--host must not be empty.",
                suggestion="Pass a hostname, IPv4 address, or bracketed IPv6 address.",
            )
        env_host = os.getenv("CHIMERA_HOST") or None
        env_socket = os.getenv("CHIMERA_SOCKET") or None
        if cli_host and socket_path:
            raise ClientError(
                "invalid_argument",
                "--socket and --host cannot be used together.",
                suggestion="Choose a local Unix socket or a remote TLS host, not both.",
            )
        if cli_host and env_socket:
            raise ClientError(
                "invalid_argument",
                "--host cannot be combined with CHIMERA_SOCKET.",
                suggestion="Unset CHIMERA_SOCKET or omit --host.",
            )
        if socket_path and env_host:
            raise ClientError(
                "invalid_argument",
                "--socket cannot be combined with CHIMERA_HOST.",
                suggestion="Unset CHIMERA_HOST or omit --socket.",
            )
        if cli_host is None and socket_path is None and env_host and env_socket:
            raise ClientError(
                "invalid_argument",
                "CHIMERA_SOCKET and CHIMERA_HOST cannot be set together.",
                suggestion="Choose a local Unix socket or a remote TLS host, not both.",
            )

        selected_host = cli_host or (None if socket_path else env_host)
        if selected_host:
            self._configure_remote(selected_host)
            return
        self._configure_unix(socket_path)

    def _configure_unix(self, socket_path: str | None) -> None:
        """Use HTTP over the local Unix-domain socket."""
        self.mode = "unix"
        resolved = (
            Path(socket_path).expanduser() if socket_path else resolve_runtime_paths().socket_path
        )
        if not resolved.is_absolute():
            resolved = Path.cwd() / resolved
        self.socket_path = resolved
        self.base_url = "http://localhost"
        self.ws_base_url = "ws://localhost"
        self.transport = httpx.HTTPTransport(uds=str(self.socket_path))

    def _configure_remote(self, host: str) -> None:
        """Use HTTPS/WSS with mandatory mutual TLS."""
        try:
            remote_host, remote_port = parse_remote_endpoint(host)
        except ChimeraError as error:
            raise ClientError.from_chimera(error, transport="tls") from error
        if not self.tls_ca or not self.tls_cert or not self.tls_key:
            raise ClientError(
                "invalid_argument",
                "Remote --host requires --tls-ca, --tls-cert, and --tls-key.",
                suggestion=(
                    "Pass the files on the command line or set CHIMERA_TLS_CA, "
                    "CHIMERA_TLS_CERT, and CHIMERA_TLS_KEY."
                ),
                transport="tls",
            )
        try:
            ca_path, cert_path, key_path = resolve_client_tls_paths(
                self.tls_ca, self.tls_cert, self.tls_key
            )
            self.tls_ca = str(ca_path)
            self.tls_cert = str(cert_path)
            self.tls_key = str(key_path)
            self.ssl_context = build_client_ssl_context(self.tls_ca, self.tls_cert, self.tls_key)
        except ChimeraError as error:
            raise ClientError.from_chimera(error, transport="tls") from error
        self.mode = "tls"
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.base_url = remote_http_url(remote_host, remote_port)
        self.ws_base_url = remote_ws_url(remote_host, remote_port)
        self.transport = None

    def request(
        self,
        command: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one command and raise a stable error when it cannot complete."""
        self._require_ready("Run 'chimeractl doctor' for setup guidance.")
        payload = {"command": command, "args": args or {}}
        try:
            with self._http_client(timeout=timeout) as client:
                response = client.post("/api/v1/command", json=payload)
        except httpx.TimeoutException as error:
            raise self._timeout_error(error) from error
        except httpx.RequestError as error:
            raise self._request_error(error) from error

        return self._unwrap_response(response)

    def _timeout_error(self, error: httpx.TimeoutException) -> ClientError:
        """Distinguish connection/TLS setup timeouts from request/response timeouts."""
        connect_types: tuple[type[Exception], ...] = (httpx.ConnectTimeout,)
        pool_timeout = getattr(httpx, "PoolTimeout", None)
        if isinstance(pool_timeout, type):
            connect_types = (httpx.ConnectTimeout, pool_timeout)
        if isinstance(error, connect_types):
            stage: Literal["connect", "request"] = "connect"
            message = "The Chimera server connection timed out before the request could be sent."
            if self.mode == "tls":
                suggestion = (
                    "Confirm the remote host, port, and TLS listener. "
                    "This error is not about the local chimera-server."
                )
            else:
                suggestion = "Retry with a larger --timeout or inspect 'chimeractl doctor'."
        else:
            stage = "request"
            message = "The Chimera server did not finish the request before the command timeout."
            if self.mode == "tls":
                suggestion = (
                    "The TLS session was established, but the remote API did not finish in time. "
                    "Retry with a larger --timeout. This is not a certificate verification failure."
                )
            else:
                suggestion = "Retry with a larger --timeout or inspect 'chimeractl doctor'."
        return ClientError(
            "timeout",
            message,
            detail=str(error),
            suggestion=suggestion,
            transport=self.mode,
            stage=stage,
        )

    def _request_error(self, error: httpx.RequestError) -> ClientError:
        """Classify transport failures without probing a different endpoint."""
        detail = str(error)
        if self.mode == "tls":
            lowered = detail.lower()
            if any(token in lowered for token in ("certificate", "ssl", "tls", "handshake")):
                return ClientError(
                    "tls_handshake_failed",
                    "TLS verification or client authentication failed for the remote server.",
                    detail=detail,
                    suggestion=(
                        "Check --tls-ca, --tls-cert, --tls-key, and the remote listen identity."
                    ),
                    transport="tls",
                )
            return ClientError(
                "server_unreachable",
                "The remote Chimera server could not be reached over TLS.",
                detail=detail,
                suggestion=(
                    "Confirm the host, port, and that remote listen is enabled on the server."
                ),
                transport="tls",
            )
        return ClientError(
            "server_unreachable",
            "The Chimera server is present but unavailable.",
            detail=detail,
            suggestion="Run 'chimeractl doctor' and inspect the server journal.",
            transport="unix",
        )

    def stream_connect(
        self, endpoint: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> AbstractAsyncContextManager[Any]:
        """Open an async WebSocket stream using the same trust model as HTTP."""
        self._require_ready(
            "Start the server, then retry the command.",
            permission_suggestion=(
                "Use sudo or join chimera-admin before opening a container terminal."
                if self.mode == "unix"
                else "Present a client certificate trusted by the remote server."
            ),
        )
        safe_params = {key: value for key, value in params.items() if key != "command"}
        query = urllib.parse.urlencode(safe_params)
        url = f"{self.ws_base_url}{endpoint}?{query}" if query else f"{self.ws_base_url}{endpoint}"
        open_timeout = STREAM_OPEN_TIMEOUT if timeout is None else min(timeout, STREAM_OPEN_TIMEOUT)
        if self.mode == "unix":
            assert self.socket_path is not None
            return websockets.unix_connect(
                str(self.socket_path),
                uri=url,
                open_timeout=open_timeout,
            )
        assert self.ssl_context is not None
        return websockets.connect(url, ssl=self.ssl_context, open_timeout=open_timeout)

    def _http_client(self, *, timeout: float | None) -> httpx.Client:
        """Build an HTTPX client for the selected transport."""
        if self.mode == "unix":
            return httpx.Client(
                transport=self.transport,
                base_url=self.base_url,
                timeout=timeout or self.timeout,
            )
        assert self.ssl_context is not None
        return httpx.Client(
            base_url=self.base_url,
            timeout=timeout or self.timeout,
            verify=self.ssl_context,
        )

    def _unwrap_response(self, response: httpx.Response) -> dict[str, Any]:
        """Convert a JSON envelope into data or a structured ClientError."""
        try:
            data = response.json()
        except ValueError as error:
            raise ClientError(
                "server_unreachable",
                "The Chimera server returned an invalid response.",
                detail=str(error),
                suggestion="Restart the server and run 'chimeractl doctor'.",
                transport=self.mode,
            ) from error

        if not response.is_success or not data.get("success"):
            payload_error = data.get("error", {})
            if isinstance(payload_error, str):
                payload_error = {"code": "host_operation_failed", "message": payload_error}
            raise ClientError(
                payload_error.get("code", "server_unreachable"),
                payload_error.get(
                    "message", f"Server request failed with HTTP {response.status_code}."
                ),
                detail=payload_error.get("detail"),
                suggestion=payload_error.get("suggestion"),
                transport=self.mode,
            )
        result = data.get("data", {})
        if not isinstance(result, dict):
            raise ClientError(
                "server_unreachable",
                "The Chimera server returned an invalid response payload.",
                suggestion="Restart the server and inspect its journal.",
                transport=self.mode,
            )
        return cast(dict[str, Any], result)

    def _require_ready(
        self, unavailable_suggestion: str, *, permission_suggestion: str | None = None
    ) -> None:
        """Fail before connecting when the selected transport is unusable."""
        if self.mode == "tls":
            return
        self._require_socket_access(
            unavailable_suggestion, permission_suggestion=permission_suggestion
        )

    def _require_socket_access(
        self, unavailable_suggestion: str, *, permission_suggestion: str | None = None
    ) -> None:
        """Distinguish a missing server from an inaccessible root/admin socket."""
        assert self.socket_path is not None
        try:
            self.socket_path.stat()
        except FileNotFoundError as error:
            raise ClientError(
                "server_unavailable",
                "The Chimera server is not reachable.",
                suggestion=unavailable_suggestion,
                transport="unix",
            ) from error
        except PermissionError as error:
            raise ClientError(
                "permission_denied",
                "You do not have permission to access the Chimera server.",
                suggestion=permission_suggestion
                or (
                    "Use sudo or ask a system administrator to add you to chimera-admin. "
                    "That group is root-equivalent for container administration."
                ),
                transport="unix",
            ) from error
        if not os.access(self.socket_path, os.R_OK | os.W_OK):
            raise ClientError(
                "permission_denied",
                "You do not have permission to access the Chimera server.",
                suggestion=permission_suggestion
                or (
                    "Use sudo or ask a system administrator to add you to chimera-admin. "
                    "That group is root-equivalent for container administration."
                ),
                transport="unix",
            )
