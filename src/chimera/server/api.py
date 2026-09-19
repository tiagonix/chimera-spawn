"""Authenticated Unix-socket and TLS HTTP/WebSocket transport for the Chimera server.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import grp
import json
import logging
import os
import pty
import pwd
import re
import secrets
import signal
import socket
import ssl
import struct
import termios
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from aiohttp import WSMsgType, web
from aiohttp.abc import AbstractAccessLogger
from aiohttp.web_exceptions import HTTPRequestEntityTooLarge
from aiohttp.web_request import BaseRequest
from aiohttp.web_response import StreamResponse

from chimera.server.service import PRIVILEGED_COMMANDS, CommandService, PeerCredentials
from chimera.endpoint import encode_stream_control, is_valid_stream_term, parse_stream_control
from chimera.errors import ChimeraError
from chimera.tls import certificate_sha256, certificate_subject
from chimera.utils.systemd import _reap_owned_child
from chimera.utils.unixsocket import SocketPathLock, inspect_unix_socket, socket_identity

MAX_REQUEST_BODY_BYTES = 1024 * 1024
STREAM_SETUP_TIMEOUT = 30.0
GUEST_UNIT_STOP_TIMEOUT = 8.0
OUTPUT_QUEUE_LIMIT = 32
OUTPUT_SEND_STALL = 60.0
CONTROL_SEND_TIMEOUT = 5.0
STDIN_EOF_BYTES = b"\x04"
EXEC_UNIT_PREFIX = "chimera-exec-"
EXEC_UNIT_RE = re.compile(r"\Achimera-exec-[0-9a-f]{32}\.service\Z")
_TERMINAL_INACTIVE = {"inactive", "failed"}
_TRANSITIONAL_ACTIVE = {"activating", "deactivating", "reloading", "maintenance"}


def parse_systemctl_show(stdout: str) -> dict[str, str] | None:
    """Parse `systemctl show -p ...` KEY=VALUE lines regardless of property order.

    Returns None when the payload is empty of usable properties, contains a
    duplicate key, or a line without '='. Callers treat None as unknown, not
    inactive or absent.
    """
    properties: dict[str, str] = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "=" not in line:
            return None
        key, value = line.split("=", 1)
        if not key or key in properties:
            return None
        properties[key] = value
    return properties if properties else None


@dataclass(frozen=True)
class GuestCleanupResult:
    """Outcome of stopping one Chimera-owned guest exec unit."""

    resolved: bool
    status: str
    detail: str | None = None


def interpret_guest_unit_show(code: int, stdout: str, stderr: str = "") -> GuestCleanupResult:
    """Map systemctl show exit + KEY=VALUE stdout to absent/inactive/active/unknown.

    LoadState=not-found is absence only for the native cases exit 0 and exit 4.
    Any other nonzero exit remains unknown even when stdout contains properties.
    """
    detail = stderr.strip() or None
    properties = parse_systemctl_show(stdout)
    if properties is None:
        return GuestCleanupResult(
            False,
            "unknown",
            detail or (f"systemctl show exited {code}" if code else "malformed unit properties"),
        )
    active = properties.get("ActiveState")
    load = properties.get("LoadState")
    if load is None or active is None:
        return GuestCleanupResult(
            False,
            "unknown",
            detail or "systemctl show omitted ActiveState or LoadState",
        )
    if load == "not-found" and code in {0, 4}:
        return GuestCleanupResult(True, "absent", detail)
    if code != 0:
        return GuestCleanupResult(False, "unknown", detail or f"systemctl show exited {code}")
    if active in _TERMINAL_INACTIVE:
        return GuestCleanupResult(True, "inactive", detail)
    if active in _TRANSITIONAL_ACTIVE:
        return GuestCleanupResult(False, "transitional", active)
    if active == "active":
        return GuestCleanupResult(False, "active", None)
    return GuestCleanupResult(False, "unknown", detail or active or f"exit {code}")


@dataclass(frozen=True)
class StreamStart:
    """Parsed stream-start control: optional argv, TTY mode, and TERM."""

    command: list[str] | None
    use_tty: bool
    term: str | None = None


logger = logging.getLogger(__name__)
http_access_logger = logging.getLogger("chimera.server.http")


class SanitizedAccessLogger(AbstractAccessLogger):
    """Log method, path, and status without query strings or bodies."""

    def log(self, request: BaseRequest, response: StreamResponse, time: float) -> None:
        """Emit a route-only access line."""
        try:
            status = response.status
        except Exception:
            status = 0
        self.logger.info("%s %s %s", request.method, request.path, status)


class ApiServer:
    """Serve the Chimera API over a Unix socket and an optional TLS listener."""

    def __init__(
        self,
        socket_path: Path,
        service: CommandService,
        admin_group: str,
        *,
        remote_host: str | None = None,
        remote_port: int = 8080,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.socket_path = Path(socket_path)
        self.service = service
        self.admin_group = admin_group
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.ssl_context = ssl_context
        self.app = web.Application(
            client_max_size=MAX_REQUEST_BODY_BYTES, middlewares=[self._payload_limit_middleware]
        )
        self.runner: web.AppRunner | None = None
        self._socket_lock = SocketPathLock(self.socket_path)
        self._bound_socket_identity: tuple[int, int] | None = None
        self._bound = False
        self._accepting = True
        self._stream_tasks: set[asyncio.Task[None]] = set()
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Register the sole control API and interactive streams."""
        self.app.router.add_post("/api/v1/command", self._handle_command)
        self.app.router.add_get("/api/v1/stream/exec", self._handle_stream_exec)
        self.app.router.add_get("/api/v1/stream/shell", self._handle_stream_shell)
        self.app.router.add_get("/api/v1/stream/logs", self._handle_stream_logs)

    async def start(self) -> None:
        """Bind the Unix socket and, when configured, a TLS TCP listener."""
        if self.remote_host is not None and self.ssl_context is None:
            raise ChimeraError(
                code="invalid_configuration",
                message="A remote listener requires a complete TLS configuration.",
                suggestion=(
                    "Set server.tls.certificate, server.tls.private_key, and "
                    "server.tls.client_ca, then restart chimera-server."
                ),
                status=500,
            )
        self._socket_lock.acquire()
        try:
            self.runner = web.AppRunner(
                self.app,
                access_log=http_access_logger,
                access_log_class=SanitizedAccessLogger,
            )
            await self.runner.setup()
            self._remove_stale_socket()
            self.socket_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            site = web.UnixSite(self.runner, str(self.socket_path))
            await site.start()
            identity = socket_identity(self.socket_path)
            if identity is None:
                raise ChimeraError(
                    code="service_unavailable",
                    message="The server bound a Unix socket but could not confirm its identity.",
                    status=500,
                )
            self._bound_socket_identity = identity
            self._bound = True
            self._accepting = True
            self._set_socket_permissions()
            logger.info("Server listening on its local Unix socket")
            if self.remote_host is not None:
                assert self.ssl_context is not None
                remote = web.TCPSite(
                    self.runner,
                    self.remote_host,
                    self.remote_port,
                    ssl_context=self.ssl_context,
                )
                await remote.start()
                logger.info("Server listening on TLS %s:%s", self.remote_host, self.remote_port)
        except Exception:
            if self._bound:
                with suppress(Exception):
                    self._remove_owned_socket()
            if self.runner is not None:
                with suppress(Exception):
                    await self.runner.cleanup()
                self.runner = None
            self._socket_lock.release()
            raise

    async def stop(self) -> None:
        """Stop accepting requests, drain owned connections, then remove this socket."""
        self._accepting = False
        sessions = list(self._stream_tasks)
        for task in sessions:
            task.cancel()
        if sessions:
            await asyncio.gather(*sessions, return_exceptions=True)
        try:
            if self.runner:
                await self.runner.cleanup()
        finally:
            try:
                self._remove_owned_socket()
            except ChimeraError as error:
                logger.warning(
                    "Leaving unexpected socket path untouched during shutdown: %s", error.message
                )
            finally:
                self._socket_lock.release()
                logger.info("API listener stopped")

    @web.middleware
    async def _payload_limit_middleware(
        self, request: web.Request, handler: Any
    ) -> web.StreamResponse:
        """Return a size-specific failure instead of flattening oversize bodies."""
        try:
            result = await handler(request)
            if not isinstance(result, web.StreamResponse):
                raise TypeError("request handler did not return a stream response")
            return result
        except HTTPRequestEntityTooLarge as error:
            response = ChimeraError(
                code="payload_too_large",
                message="Request body exceeds the 1 MiB limit.",
                detail=str(error),
                suggestion="Reduce the request size and retry.",
                status=413,
            )
            return web.json_response(
                {"success": False, "error": response.as_dict()}, status=response.status
            )

    async def _handle_command(self, request: web.Request) -> web.Response:
        """Process one REST command with a stable response envelope."""
        if not self._accepting:
            response = ChimeraError(
                code="service_unavailable",
                message="The Chimera server is shutting down.",
                suggestion="Retry after the server has restarted.",
                status=503,
            )
            return web.json_response(
                {"success": False, "error": response.as_dict()}, status=response.status
            )
        payload: dict[str, Any] = {}
        peer = PeerCredentials(uid=None)
        try:
            raw_payload = await request.json()
            if not isinstance(raw_payload, dict):
                raise ChimeraError(
                    code="invalid_argument",
                    message="Request body must be a JSON object.",
                    status=400,
                )
            payload = cast(dict[str, Any], raw_payload)
            peer = self._caller_identity(request)
            raw_args = payload.get("args")
            if raw_args is not None and not isinstance(raw_args, dict):
                raise ChimeraError(
                    code="invalid_argument",
                    message="Command arguments must be a JSON object.",
                    status=400,
                )
            command = payload.get("command")
            result = await self.service.execute(
                command if isinstance(command, str) else None, raw_args, peer
            )
            if isinstance(command, str) and command in PRIVILEGED_COMMANDS:
                logger.info("Authorized command=%s %s", command, peer.audit_identity())
            return web.json_response({"success": True, "data": result})
        except HTTPRequestEntityTooLarge as error:
            response = ChimeraError(
                code="payload_too_large",
                message="Request body exceeds the 1 MiB limit.",
                detail=str(error),
                suggestion="Reduce the request size and retry.",
                status=413,
            )
            return web.json_response(
                {"success": False, "error": response.as_dict()}, status=response.status
            )
        except ChimeraError as error:
            self._audit_error(error, peer, payload.get("command"))
            return web.json_response(
                {"success": False, "error": error.as_dict()}, status=error.status
            )
        except (json.JSONDecodeError, ValueError) as error:
            response = ChimeraError(
                code="invalid_argument",
                message="Request body is not valid JSON.",
                detail=str(error),
                status=400,
            )
            return web.json_response(
                {"success": False, "error": response.as_dict()}, status=response.status
            )
        except Exception as error:
            logger.exception("Unexpected command failure")
            response = ChimeraError(
                code="service_unavailable",
                message="The Chimera server could not complete the request.",
                detail=str(error),
                suggestion="Run 'chimeractl doctor' and inspect the server journal.",
                status=503,
            )
            return web.json_response(
                {"success": False, "error": response.as_dict()}, status=response.status
            )

    async def _handle_stream_exec(self, request: web.Request) -> web.StreamResponse:
        """Authorize and proxy an interactive command execution session."""
        try:
            return await self._open_stream(request, request.query.get("name"), expect_command=True)
        except ChimeraError as error:
            self._audit_error(error, self._caller_identity(request), "stream_exec")
            return web.json_response(
                {"success": False, "error": error.as_dict()}, status=error.status
            )

    async def _handle_stream_shell(self, request: web.Request) -> web.StreamResponse:
        """Authorize and proxy an interactive shell session."""
        try:
            return await self._open_stream(request, request.query.get("name"), expect_command=False)
        except ChimeraError as error:
            self._audit_error(error, self._caller_identity(request), "stream_shell")
            return web.json_response(
                {"success": False, "error": error.as_dict()}, status=error.status
            )

    async def _handle_stream_logs(self, request: web.Request) -> web.StreamResponse:
        """Authorize and stream journalctl from the Chimera server host."""
        peer = self._caller_identity(request)
        try:
            self.service.authorize("stream_logs", peer)
            name = request.query.get("name")
            if not name:
                raise ChimeraError(
                    code="invalid_argument",
                    message="Container name is required.",
                    status=400,
                )
            try:
                lines = int(request.query.get("lines", "200"))
            except ValueError as error:
                raise ChimeraError(
                    code="invalid_argument",
                    message="Log line count must be an integer.",
                    status=400,
                ) from error
            if lines < 0:
                raise ChimeraError(
                    code="invalid_argument",
                    message="Log line count must be zero or greater.",
                    status=400,
                )
            unit = request.query.get("unit")
            if unit is not None and (
                not unit or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in unit)
            ):
                raise ChimeraError(
                    code="invalid_argument",
                    message="Log unit must be non-empty and contain no control characters.",
                    status=400,
                )
            follow = request.query.get("follow") == "1"
            supervisor = request.query.get("supervisor") == "1"
            if supervisor and unit is not None:
                raise ChimeraError(
                    code="invalid_argument",
                    message="--unit cannot be combined with --supervisor.",
                    status=400,
                )
            await self.service.state_engine.validate_log_target(name, supervisor=supervisor)
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            task = asyncio.current_task()
            if task is not None:
                self._stream_tasks.add(task)
            try:
                await self._proxy_logs(
                    websocket,
                    name,
                    unit=unit,
                    lines=lines,
                    follow=follow,
                    supervisor=supervisor,
                )
            finally:
                if task is not None:
                    self._stream_tasks.discard(task)
            logger.info("Authorized stream_logs %s", peer.audit_identity())
            return websocket
        except ChimeraError as error:
            self._audit_error(error, peer, "stream_logs")
            return web.json_response(
                {"success": False, "error": error.as_dict()}, status=error.status
            )

    async def _proxy_logs(
        self,
        websocket: web.WebSocketResponse,
        name: str,
        *,
        unit: str | None,
        lines: int,
        follow: bool,
        supervisor: bool,
    ) -> None:
        """Stream one bounded journalctl child and reap it on disconnect."""
        argv = ["journalctl"]
        if supervisor:
            argv.extend(["-u", f"systemd-nspawn@{name}.service"])
        else:
            argv.extend(["-M", name])
        argv.extend(["-b", "--no-pager", "-n", str(lines)])
        if unit is not None:
            argv.extend(["-u", unit])
        if follow:
            argv.append("-f")
        process: asyncio.subprocess.Process | None = None
        read_task: asyncio.Task[bytes] | None = None
        receive_task: asyncio.Task[Any] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if process.stdout is None:
                raise OSError("journalctl did not provide stdout")
            receive_task = asyncio.create_task(websocket.receive())
            while True:
                read_task = asyncio.create_task(process.stdout.read(4096))
                done, _pending = await asyncio.wait(
                    {read_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if receive_task in done:
                    await _reap_owned_child(process)
                    process = None
                    return
                chunk = read_task.result()
                read_task = None
                if not chunk:
                    code = await process.wait()
                    await self._send_stream_control(
                        websocket,
                        {"type": "complete", "kind": "process", "exit_status": code},
                    )
                    process = None
                    return
                await asyncio.wait_for(websocket.send_bytes(chunk), timeout=OUTPUT_SEND_STALL)
        except (OSError, TimeoutError, ConnectionError):
            await self._send_stream_error(websocket, "The journal stream failed.")
        finally:
            for task in (read_task, receive_task):
                if task is not None and not task.done():
                    task.cancel()
            if process is not None and process.returncode is None:
                await _reap_owned_child(process)

    async def _open_stream(
        self, request: web.Request, name: str | None, *, expect_command: bool
    ) -> web.StreamResponse:
        """Open one authorized stream after validating container availability."""
        if not self._accepting:
            raise ChimeraError(
                code="service_unavailable",
                message="The Chimera server is shutting down.",
                suggestion="Retry after the server has restarted.",
                status=503,
            )
        peer = self._caller_identity(request)
        stream_command = "stream_exec" if expect_command else "stream_shell"
        self.service.authorize(stream_command, peer)
        if not name:
            raise ChimeraError(
                code="invalid_argument",
                message="Container name is required.",
                status=400,
            )
        await self.service.state_engine.validate_stream_target(name)
        logger.info("Authorized %s %s", stream_command, peer.audit_identity())
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        task = asyncio.current_task()
        if task is not None:
            self._stream_tasks.add(task)
        try:
            await self._proxy_stream(websocket, name, expect_command=expect_command)
        finally:
            if task is not None:
                self._stream_tasks.discard(task)
        return websocket

    def _stream_argv(
        self,
        container_name: str,
        command: list[str] | None,
        *,
        use_tty: bool = True,
        term: str | None = None,
    ) -> list[str]:
        """Build the distro-native invocation for a stream session.

        Exec uses systemd-run so the guest command's status is available.
        Interactive sessions use --pty; non-TTY stdin uses --pipe so close is EOF.
        Shell uses machinectl shell, which does not propagate a guest status.
        TTY sessions may pass one validated TERM via --setenv; pipe sessions do not.
        """
        setenv: list[str] = []
        if use_tty and term is not None and is_valid_stream_term(term):
            setenv = [f"--setenv=TERM={term}"]
        if command is None:
            return ["machinectl", *setenv, "shell", container_name]
        unit = f"{EXEC_UNIT_PREFIX}{secrets.token_hex(16)}.service"
        return [
            "systemd-run",
            f"--machine={container_name}",
            "--quiet",
            "--wait",
            "--pty" if use_tty else "--pipe",
            "--collect",
            "--service-type=exec",
            "--expand-environment=no",
            *setenv,
            f"--unit={unit}",
            "--",
            *command,
        ]

    @staticmethod
    def _exec_unit_from_argv(argv: list[str]) -> str | None:
        """Return the Chimera-owned exec unit encoded in a systemd-run argv."""
        for part in argv:
            if not part.startswith("--unit="):
                continue
            unit = part.removeprefix("--unit=")
            if EXEC_UNIT_RE.fullmatch(unit):
                return unit
        return None

    async def _run_systemctl_bounded(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        """Run one systemctl helper with a remaining-time deadline and reap it."""
        if timeout <= 0:
            return 124, "", "cleanup helper timed out before start"
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            return 1, "", str(error)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            await _reap_owned_child(process)
            return 124, "", "cleanup helper timed out"
        except asyncio.CancelledError:
            await _reap_owned_child(process)
            raise
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def _observe_guest_unit(
        self, container_name: str, unit: str, timeout: float
    ) -> GuestCleanupResult:
        """Read ActiveState/LoadState for one exact unit; unknown is not stopped."""
        code, out, err = await self._run_systemctl_bounded(
            [
                "systemctl",
                f"--machine={container_name}",
                "show",
                unit,
                "-p",
                "ActiveState",
                "-p",
                "LoadState",
            ],
            timeout,
        )
        return interpret_guest_unit_show(code, out, err)

    async def _stop_guest_exec_unit(self, container_name: str, unit: str) -> GuestCleanupResult:
        """Stop one exact guest unit and confirm inactive/absent, not observation failure."""
        if not EXEC_UNIT_RE.fullmatch(unit):
            logger.warning("Refusing to stop unexpected exec unit name %s", unit)
            return GuestCleanupResult(False, "refused", "unit name is not a Chimera exec unit")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + GUEST_UNIT_STOP_TIMEOUT

        def remaining() -> float:
            return max(0.0, deadline - loop.time())

        stop_code, _stop_out, stop_err = await self._run_systemctl_bounded(
            ["systemctl", f"--machine={container_name}", "stop", unit],
            remaining(),
        )
        last = await self._observe_guest_unit(container_name, unit, remaining())
        while loop.time() < deadline:
            if last.resolved:
                return last
            if last.status == "unknown":
                logger.warning(
                    "Guest exec unit %s in %s could not be observed after stop (stop_exit=%s): %s",
                    unit,
                    container_name,
                    stop_code,
                    last.detail,
                )
                return GuestCleanupResult(False, "unknown", last.detail)
            await asyncio.sleep(0.1)
            last = await self._observe_guest_unit(container_name, unit, remaining())
        logger.warning(
            "Guest exec unit %s in %s cleanup unresolved (status=%s stop_exit=%s)",
            unit,
            container_name,
            last.status,
            stop_code,
        )
        return GuestCleanupResult(False, last.status, last.detail or stop_err or None)

    async def _abandon_exec_session(
        self,
        container_name: str,
        unit: str | None,
        process: asyncio.subprocess.Process | None,
    ) -> GuestCleanupResult:
        """Stop the owned guest unit, reap systemd-run, then confirm the exact unit."""
        result = GuestCleanupResult(True, "reaped")
        if unit:
            result = await self._stop_guest_exec_unit(container_name, unit)
        if process is not None and process.returncode is None:
            await _reap_owned_child(process)
        if unit:
            result = await self._stop_guest_exec_unit(container_name, unit)
            if not result.resolved:
                logger.error(
                    "Unresolved guest exec cleanup unit=%s container=%s status=%s detail=%s",
                    unit,
                    container_name,
                    result.status,
                    result.detail,
                )
        return result

    @staticmethod
    async def _write_all_to_fd(fd: int, data: bytes) -> None:
        """Write every byte to a nonblocking fd, waiting for writable as needed."""
        if not data:
            return
        loop = asyncio.get_running_loop()
        offset = 0
        while offset < len(data):
            try:
                written = os.write(fd, data[offset:])
            except BlockingIOError:
                written = 0
            except OSError as error:
                if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    written = 0
                else:
                    raise
            if written > 0:
                offset += written
                continue
            writable = loop.create_future()

            def on_writable() -> None:
                if not writable.done():
                    writable.set_result(None)

            try:
                loop.add_writer(fd, on_writable)
                await writable
            finally:
                with suppress(Exception):
                    loop.remove_writer(fd)

    @classmethod
    async def _write_all_to_pty(cls, master: int, data: bytes) -> None:
        """Write every byte to a nonblocking PTY master."""
        await cls._write_all_to_fd(master, data)

    async def _send_stream_control(
        self, websocket: web.WebSocketResponse, payload: dict[str, Any]
    ) -> None:
        """Send one control frame with a bounded wait; never hang a failed transport."""
        if websocket.closed:
            return
        try:
            await asyncio.wait_for(
                websocket.send_str(encode_stream_control(payload)),
                timeout=CONTROL_SEND_TIMEOUT,
            )
        except Exception:
            logger.debug("Could not deliver stream control frame")

    async def _send_stream_error(self, websocket: web.WebSocketResponse, message: str) -> None:
        """Best-effort protocol error when the session cannot complete successfully."""
        await self._send_stream_control(
            websocket, {"type": "error", "code": "stream_failed", "message": message}
        )

    async def _proxy_stream(
        self,
        websocket: web.WebSocketResponse,
        container_name: str,
        *,
        expect_command: bool,
    ) -> None:
        """Own session I/O, spawn, proxy, wait, and cleanup for one session."""
        try:
            start = await self._read_stream_start(websocket, expect_command=expect_command)
        except ChimeraError as error:
            if not websocket.closed:
                with suppress(Exception):
                    await websocket.send_str(
                        encode_stream_control(
                            {
                                "type": "error",
                                "code": error.code,
                                "message": error.message,
                            }
                        )
                    )
            return
        command = start.command
        use_tty = start.use_tty
        master = -1
        slave = -1
        stdout_fd = -1
        stdin_fd = -1
        process: asyncio.subprocess.Process | None = None
        guest_unit: str | None = None
        loop = asyncio.get_running_loop()
        output_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        eof_event = asyncio.Event()
        reader_installed = False
        paused = False
        try:
            argv = self._stream_argv(container_name, command, use_tty=use_tty, term=start.term)
            guest_unit = self._exec_unit_from_argv(argv)
            try:
                if use_tty:
                    master, slave = pty.openpty()
                    os.set_blocking(master, False)
                    process = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        start_new_session=True,
                    )
                    with suppress(OSError):
                        os.close(slave)
                    slave = -1
                    stdout_fd = master
                    stdin_fd = master
                else:
                    process = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                    )
                    if process.stdin is None or process.stdout is None:
                        raise OSError("pipe session did not provide stdio")
            except Exception:
                with suppress(OSError):
                    if master >= 0:
                        os.close(master)
                master = -1
                with suppress(OSError):
                    if slave >= 0:
                        os.close(slave)
                slave = -1
                if not websocket.closed:
                    with suppress(Exception):
                        await websocket.send_str(
                            encode_stream_control(
                                {
                                    "type": "error",
                                    "code": "host_operation_failed",
                                    "message": "Could not start the container terminal session.",
                                }
                            )
                        )
                return

            space_event = asyncio.Event()
            space_event.set()

            def pause_reader() -> None:
                nonlocal paused, reader_installed
                if reader_installed and stdout_fd >= 0:
                    with suppress(Exception):
                        loop.remove_reader(stdout_fd)
                    reader_installed = False
                paused = True
                if output_queue.qsize() >= OUTPUT_QUEUE_LIMIT:
                    space_event.clear()

            def resume_reader() -> None:
                nonlocal paused, reader_installed
                if eof_event.is_set():
                    space_event.set()
                    return
                if output_queue.qsize() < OUTPUT_QUEUE_LIMIT:
                    space_event.set()
                if not use_tty or stdout_fd < 0:
                    return
                if not reader_installed:
                    loop.add_reader(stdout_fd, on_stdout_read)
                    reader_installed = True
                paused = False

            def mark_eof() -> None:
                pause_reader()
                output_queue.put_nowait(None)
                eof_event.set()
                space_event.set()

            def on_stdout_read() -> None:
                if output_queue.qsize() >= OUTPUT_QUEUE_LIMIT:
                    pause_reader()
                    return
                try:
                    chunk = os.read(stdout_fd, 4096)
                except BlockingIOError:
                    return
                except OSError as error:
                    if (
                        error.errno == errno.EIO
                        and process is not None
                        and process.returncode is None
                    ):
                        return
                    chunk = b""
                if not chunk:
                    if process is not None and process.returncode is None:
                        return
                    mark_eof()
                    return
                output_queue.put_nowait(chunk)
                if output_queue.qsize() >= OUTPUT_QUEUE_LIMIT:
                    pause_reader()

            async def pump_pipe_stdout() -> None:
                assert process is not None and process.stdout is not None
                while True:
                    if output_queue.qsize() >= OUTPUT_QUEUE_LIMIT:
                        space_event.clear()
                        if output_queue.qsize() >= OUTPUT_QUEUE_LIMIT:
                            await space_event.wait()
                            continue
                    chunk = await process.stdout.read(4096)
                    if not chunk:
                        mark_eof()
                        return
                    output_queue.put_nowait(chunk)
                    if output_queue.qsize() >= OUTPUT_QUEUE_LIMIT:
                        space_event.clear()

            async def pump_output() -> None:
                while True:
                    chunk = await output_queue.get()
                    if chunk is None:
                        space_event.set()
                        return
                    await asyncio.wait_for(websocket.send_bytes(chunk), timeout=OUTPUT_SEND_STALL)
                    if output_queue.qsize() < OUTPUT_QUEUE_LIMIT:
                        space_event.set()
                    if paused and output_queue.qsize() < max(1, OUTPUT_QUEUE_LIMIT // 2):
                        resume_reader()

            async def pump_input() -> None:
                async for message in websocket:
                    if message.type == WSMsgType.BINARY:
                        if not message.data:
                            continue
                        if use_tty and stdin_fd >= 0:
                            await self._write_all_to_fd(stdin_fd, message.data)
                        elif process is not None and process.stdin is not None:
                            process.stdin.write(message.data)
                            await asyncio.wait_for(process.stdin.drain(), timeout=OUTPUT_SEND_STALL)
                    elif message.type == WSMsgType.TEXT:
                        control = parse_stream_control(message.data)
                        if control is None:
                            continue
                        kind = control.get("type")
                        if kind == "stdin_eof":
                            if use_tty and stdin_fd >= 0:
                                await self._write_all_to_fd(stdin_fd, STDIN_EOF_BYTES)
                            elif process is not None and process.stdin is not None:
                                process.stdin.close()
                        elif kind == "resize" and use_tty and stdin_fd >= 0:
                            self._apply_resize_payload(stdin_fd, control)
                            if process is not None and process.pid:
                                with suppress(ProcessLookupError, PermissionError, OSError):
                                    os.kill(process.pid, signal.SIGWINCH)
                    elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSE}:
                        return

            async def deliver_remaining() -> None:
                if use_tty:
                    resume_reader()
                    on_stdout_read()
                if not eof_event.is_set():
                    await eof_event.wait()
                await output_task

            def unblock_output() -> None:
                space_event.set()
                if not eof_event.is_set():
                    with suppress(Exception):
                        output_queue.put_nowait(None)
                    eof_event.set()

            if use_tty:
                resume_reader()
            pipe_stdout_task = None if use_tty else asyncio.create_task(pump_pipe_stdout())
            output_task = asyncio.create_task(pump_output())
            input_task = asyncio.create_task(pump_input())
            wait_task = asyncio.create_task(process.wait())
            eof_wait_task = asyncio.create_task(eof_event.wait())
            tasks = {output_task, input_task, wait_task, eof_wait_task}
            if pipe_stdout_task is not None:
                tasks.add(pipe_stdout_task)
            outcome = "running"
            try:
                while outcome == "running":
                    success = (
                        wait_task.done()
                        and eof_event.is_set()
                        and output_task.done()
                        and not output_task.cancelled()
                    )
                    if success:
                        self._raise_completed_task_errors(
                            {
                                task
                                for task in (wait_task, output_task, pipe_stdout_task)
                                if task is not None and task.done()
                            }
                        )
                        outcome = "success"
                        break
                    pending = {task for task in tasks if not task.done()}
                    if not pending:
                        if wait_task.done() and eof_event.is_set() and output_task.done():
                            self._raise_completed_task_errors({wait_task, output_task})
                            outcome = "success"
                        else:
                            outcome = "disconnect"
                        break
                    done, _pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    self._raise_completed_task_errors(done)
                    if wait_task.done() and not eof_event.is_set() and use_tty:
                        resume_reader()
                        on_stdout_read()
                    if input_task.done() and not (
                        wait_task.done() and eof_event.is_set() and output_task.done()
                    ):
                        outcome = "disconnect"
                        break
                if outcome == "disconnect":
                    unblock_output()
                    cleanup = await self._abandon_exec_session(container_name, guest_unit, process)
                    process = None
                    with suppress(TimeoutError, OSError, ConnectionError, asyncio.CancelledError):
                        await deliver_remaining()
                    message = "The terminal session ended before the command completed."
                    if not cleanup.resolved:
                        message = (
                            "The terminal session ended before the command completed "
                            "and the guest exec unit could not be confirmed stopped."
                        )
                    if expect_command:
                        await self._send_stream_error(websocket, message)
                elif outcome == "success":
                    try:
                        await deliver_remaining()
                    except (TimeoutError, OSError, ConnectionError):
                        await self._send_stream_error(
                            websocket,
                            "The terminal session ended before all output could be delivered.",
                        )
                        return
                    await self._send_stream_control(
                        websocket,
                        {
                            "type": "complete",
                            "kind": "process" if expect_command else "session",
                            "exit_status": process.returncode if expect_command else None,
                        },
                    )
            except (TimeoutError, OSError, ConnectionError):
                unblock_output()
                if process is not None and process.returncode is None:
                    await self._abandon_exec_session(container_name, guest_unit, process)
                    process = None
                await self._send_stream_error(
                    websocket,
                    "The terminal session ended before all output could be delivered.",
                )
                return
            finally:
                extras = [output_task, input_task, wait_task, eof_wait_task]
                if pipe_stdout_task is not None:
                    extras.append(pipe_stdout_task)
                for task in extras:
                    if not task.done():
                        task.cancel()
                finished = await asyncio.gather(*extras, return_exceptions=True)
                for item in finished:
                    if isinstance(item, Exception) and not isinstance(
                        item, (asyncio.CancelledError, OSError, ConnectionResetError)
                    ):
                        logger.debug("Stream proxy task ended: %s", item)
        except asyncio.CancelledError:
            await self._abandon_exec_session(container_name, guest_unit, process)
            process = None
            raise
        finally:
            if reader_installed and stdout_fd >= 0:
                with suppress(Exception):
                    loop.remove_reader(stdout_fd)
            if slave >= 0:
                with suppress(OSError):
                    os.close(slave)
            if master >= 0:
                with suppress(OSError):
                    os.close(master)
            if process is not None and process.returncode is None:
                await self._abandon_exec_session(container_name, guest_unit, process)

    async def _read_stream_start(
        self, websocket: web.WebSocketResponse, *, expect_command: bool
    ) -> StreamStart:
        """Read stream-start metadata from a control frame, not the URL."""
        try:
            message = await asyncio.wait_for(websocket.receive(), timeout=STREAM_SETUP_TIMEOUT)
        except TimeoutError as error:
            raise ChimeraError(
                code="timeout",
                message="The stream session did not start before the setup timeout.",
                suggestion="Retry the command and keep the connection open until it starts.",
                status=408,
            ) from error
        if message.type != WSMsgType.TEXT:
            raise ChimeraError(
                code="invalid_argument",
                message="Streams require a start control message.",
                status=400,
            )
        control = parse_stream_control(message.data)
        if control is None or control.get("type") != "start":
            raise ChimeraError(
                code="invalid_argument",
                message="Streams require a start control message.",
                status=400,
            )
        return self._parse_stream_start(control, expect_command=expect_command)

    @staticmethod
    def _parse_stream_start(control: dict[str, Any], *, expect_command: bool) -> StreamStart:
        """Validate start-frame argv, TTY mode, and optional TERM metadata."""
        command: list[str] | None = None
        if expect_command:
            raw_command = control.get("command")
            if (
                not isinstance(raw_command, list)
                or not raw_command
                or not all(isinstance(part, str) for part in raw_command)
            ):
                raise ChimeraError(
                    code="invalid_argument",
                    message="Stream command must be a JSON list of arguments.",
                    status=400,
                )
            command = [str(part) for part in raw_command]
        use_tty = True
        if "tty" in control:
            if not isinstance(control.get("tty"), bool):
                raise ChimeraError(
                    code="invalid_argument",
                    message="Stream tty must be a boolean.",
                    status=400,
                )
            use_tty = bool(control["tty"])
        term: str | None = None
        if "term" in control:
            raw_term = control["term"]
            if not isinstance(raw_term, str) or not is_valid_stream_term(raw_term):
                raise ChimeraError(
                    code="invalid_argument",
                    message="Stream term must be a terminal type name.",
                    status=400,
                )
            if use_tty:
                term = raw_term
        return StreamStart(command, use_tty, term)

    @staticmethod
    def _raise_completed_task_errors(done: set[asyncio.Task[Any]]) -> None:
        """Surface unexpected exceptions from finished proxy tasks."""
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error is None or isinstance(error, asyncio.CancelledError):
                continue
            raise error

    @staticmethod
    def _apply_resize_payload(master: int, control: dict[str, Any]) -> None:
        """Apply a parsed resize control object to the session PTY."""
        try:
            rows = int(control.get("rows", 24))
            columns = int(control.get("cols", 80))
            if rows < 1 or columns < 1:
                return
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        except (ValueError, TypeError, OSError):
            logger.debug("Ignored invalid terminal resize payload")

    @staticmethod
    def _apply_resize(master: int, payload: str) -> None:
        """Apply valid client terminal resize messages."""
        control = parse_stream_control(payload)
        if control is None or control.get("type") != "resize":
            return
        ApiServer._apply_resize_payload(master, control)

    def _set_socket_permissions(self) -> None:
        """Set administrator-group access, falling back to root-only permissions."""
        try:
            group_gid = grp.getgrnam(self.admin_group).gr_gid
            os.chown(self.socket_path, 0, group_gid)
            os.chmod(self.socket_path, 0o660)
        except (KeyError, PermissionError) as error:
            os.chmod(self.socket_path, 0o600)
            logger.warning(
                "The %s group is unavailable; the server socket is root-only: %s",
                self.admin_group,
                error,
            )

    def _remove_stale_socket(self) -> None:
        """Unlink only a proven-stale Unix socket after confirming its identity."""
        identity = socket_identity(self.socket_path)
        state = inspect_unix_socket(self.socket_path)
        if state == "absent":
            return
        if state == "live":
            raise ChimeraError(
                code="already_running",
                message=f"A process is already listening at '{self.socket_path}'.",
                suggestion="Stop the running listener or choose a different --socket path.",
                status=409,
            )
        if state == "permission_denied":
            raise ChimeraError(
                code="permission_denied",
                message=f"Cannot inspect socket path '{self.socket_path}'.",
                suggestion="Run the server as root or correct socket-path permissions.",
                status=403,
            )
        if state == "not_socket":
            raise ChimeraError(
                code="invalid_socket_path",
                message=f"Refusing to replace non-socket path '{self.socket_path}'.",
                suggestion="Choose an unused socket path or remove the unexpected file manually.",
                status=409,
            )
        current = socket_identity(self.socket_path)
        if identity is None or current is None or current != identity:
            raise ChimeraError(
                code="already_running",
                message=f"Socket path '{self.socket_path}' changed during stale inspection.",
                suggestion="Retry after inspecting which process owns the Unix socket.",
                status=409,
            )
        self.socket_path.unlink()

    def _remove_owned_socket(self) -> None:
        """Unlink only the socket this instance bound."""
        if not self._bound:
            return
        identity = socket_identity(self.socket_path)
        if identity is None:
            self._bound = False
            return
        if self._bound_socket_identity is not None and identity != self._bound_socket_identity:
            raise ChimeraError(
                code="already_running",
                message=f"Refusing to remove a replacement socket at '{self.socket_path}'.",
                status=409,
            )
        self.socket_path.unlink()
        self._bound = False
        self._bound_socket_identity = None

    def _caller_identity(self, request: web.Request) -> PeerCredentials:
        """Establish identity from Unix SO_PEERCRED or the verified TLS peer."""
        transport = request.transport
        sock = transport.get_extra_info("socket") if transport else None
        ssl_object = transport.get_extra_info("ssl_object") if transport else None
        if ssl_object is not None:
            return self._tls_identity(ssl_object)
        if sock is not None and sock.family == socket.AF_UNIX:
            return self._unix_identity(sock)
        raise ChimeraError(
            code="permission_denied",
            message="Unauthenticated network access is not permitted.",
            detail="Remote API access requires a verified mutual TLS connection.",
            suggestion="Use chimeractl --host with --tls-ca, --tls-cert, and --tls-key.",
            status=403,
        )

    @staticmethod
    def _tls_identity(ssl_object: ssl.SSLSocket | ssl.SSLObject) -> PeerCredentials:
        """Build a remote administrator identity from the handshake certificate."""
        try:
            der = ssl_object.getpeercert(binary_form=True)
        except ssl.SSLError as error:
            raise ChimeraError(
                code="permission_denied",
                message="The TLS client certificate could not be read.",
                detail=str(error),
                status=403,
            ) from error
        if not der:
            raise ChimeraError(
                code="permission_denied",
                message="Remote access requires a verified client certificate.",
                status=403,
            )
        try:
            parsed = ssl_object.getpeercert()
        except ssl.SSLError:
            parsed = None
        return PeerCredentials(
            transport="tls",
            cert_sha256=certificate_sha256(der),
            cert_subject=certificate_subject(parsed),
        )

    @staticmethod
    def _unix_identity(sock: socket.socket) -> PeerCredentials:
        """Read Linux SO_PEERCRED and the peer's supplementary groups."""
        try:
            credentials = sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            pid, uid, gid = struct.unpack("3i", credentials)
            groups: tuple[int, ...] = (gid,)
            try:
                username = pwd.getpwuid(uid).pw_name
                groups = tuple(os.getgrouplist(username, gid))
            except (KeyError, OSError):
                pass
            return PeerCredentials(transport="unix", uid=uid, gid=gid, pid=pid, gids=groups)
        except OSError:
            return PeerCredentials(transport="unix", uid=None)

    @staticmethod
    def _audit_error(error: ChimeraError, peer: PeerCredentials, command: object | None) -> None:
        """Log denial and failure codes without leaking request payloads."""
        if error.code == "permission_denied":
            logger.warning(
                "Denied command=%s %s: %s",
                command,
                peer.audit_identity(),
                error.message,
            )
