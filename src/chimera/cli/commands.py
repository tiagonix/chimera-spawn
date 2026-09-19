"""CLI response rendering and interactive terminal helpers.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import signal
import stat
import struct
import sys
import termios
import tty
from typing import Any

from rich.console import Console
from rich.table import Table

from chimera.cli.client import ChimeraClient, ClientError, TransportMode
from chimera.endpoint import encode_stream_control, parse_stream_control, select_client_term

console = Console()
STDIN_QUEUE_LIMIT = 32
STDOUT_WRITE_STALL = 60.0


def print_json(data: dict[str, Any]) -> None:
    """Write clean machine-readable JSON to stdout."""
    print(json.dumps(data, default=str, sort_keys=True))


def print_success(message: str, data: dict[str, Any], output_format: str) -> None:
    """Print one concise mutation result or its JSON equivalent."""
    if output_format == "json":
        print_json({"success": True, "data": data})
    else:
        console.print(message)


def print_resources(
    response: dict[str, Any],
    output_format: str,
    *,
    console_out: Console | None = None,
) -> None:
    """Render resource lists as compact tables or clean JSON."""
    printer = console_out if console_out is not None else console
    if output_format == "json":
        print_json(response)
        return
    if "images" in response:
        table = Table(title="Images")
        table.add_column("Name", overflow="fold")
        table.add_column("Type", overflow="fold")
        table.add_column("Verify", overflow="fold")
        table.add_column("Source", overflow="fold")
        for info in response["images"].values():
            table.add_row(info["name"], info["type"], info["verify"], info["source"])
        printer.print(table)
    if "containers" in response:
        table = Table(title="Containers")
        table.add_column("Name", overflow="fold")
        table.add_column("Observed", overflow="fold")
        table.add_column("Desired", overflow="fold")
        table.add_column("Image", overflow="fold")
        table.add_column("Profile", overflow="fold")
        for info in response["containers"].values():
            state = "deleting" if info["deleting"] else info["observed_state"]
            table.add_row(
                info["name"], state, info["desired_state"], info["image"], info["profile"]
            )
        printer.print(table)
    if "profiles" in response:
        table = Table(title="Profiles")
        table.add_column("Name", overflow="fold")
        table.add_column("Description", overflow="fold")
        for info in response["profiles"].values():
            table.add_row(info["name"], info.get("description") or "")
        printer.print(table)


def print_info(response: dict[str, Any], name: str, output_format: str) -> None:
    """Render desired and observed state for a single container."""
    if output_format == "json":
        print_json(response)
        return
    info = response["containers"][name]
    console.print(f"Container: {name}")
    console.print(f"Observed state: {info['observed_state']}")
    console.print(f"Desired state: {info['desired_state']}")
    console.print(f"Image: {info['image']}")
    console.print(f"Profile: {info['profile']}")
    for mount in info.get("bind_mounts") or []:
        mode = "read-only" if mount.get("read_only") else "writable"
        options = f":{mount['options']}" if mount.get("options") else ""
        console.print(f"Bind ({mode}): {mount['source']} -> {mount['destination']}{options}")
    for mount in info.get("tmpfs_mounts") or []:
        options = f":{mount['options']}" if mount.get("options") else ""
        console.print(f"Tmpfs: {mount['destination']}{options}")
    for forward in info.get("port_forwards") or []:
        console.print(
            "Publish: " f"{forward['protocol']}:{forward['host_port']}:{forward['container_port']}"
        )
    controls = info.get("resource_controls") or {}
    if controls:
        rendered = ", ".join(f"{key}={value}" for key, value in controls.items())
        console.print(f"Resources: {rendered}")
    console.print(f"Provisioning: {info.get('provisioning_state', 'unknown')}")
    if info.get("provisioning_drift"):
        console.print("Creation-time drift: recreate required")
    host_pending = info.get("host_config_pending")
    if host_pending is True:
        console.print("Host configuration: pending")
    elif host_pending is None:
        console.print("Host configuration: unknown")
    if info.get("host_config_missing"):
        console.print("Host configuration artifacts: missing")
    if info["deleting"]:
        console.print("Deletion: pending")
    if info["last_error"]:
        console.print(f"Last error: {info['last_error']}")
    if info.get("next_action"):
        console.print(f"Next: {info['next_action']}")


def print_doctor(response: dict[str, Any], output_format: str) -> None:
    """Render operator readiness diagnostics."""
    if output_format == "json":
        print_json(response)
        return
    checks = response["checks"]
    console.print("Chimera doctor")
    if response.get("mode") == "remote":
        target = checks["target"]
        connection = checks["connection"]
        console.print(f"target: {target['host']}:{target['port']}")
        console.print(
            "tls ca: " + ("readable" if checks["tls_ca"].get("readable") else "unavailable")
        )
        console.print(
            "tls cert: " + ("readable" if checks["tls_cert"].get("readable") else "unavailable")
        )
        console.print(
            "tls key: " + ("readable" if checks["tls_key"].get("readable") else "unavailable")
        )
        console.print("tls connection: " + ("ok" if connection.get("tls") else "failed"))
        console.print(
            "server certificate: "
            + ("verified" if connection.get("server_certificate") else "unverified")
        )
        console.print(
            "client authentication: "
            + ("accepted" if connection.get("client_authentication") else "rejected")
        )
        console.print(
            "remote api: " + ("reachable" if connection.get("api_reachable") else "unreachable")
        )
        if response.get("server"):
            server_checks = response["server"]["checks"]
            console.print(
                f"remote catalog: {'valid' if server_checks['catalog']['valid'] else 'invalid'}"
            )
            console.print(
                f"remote machinectl: {'available' if server_checks['machinectl'] else 'missing'}"
            )
        server_error = response.get("server_error")
        if isinstance(server_error, dict):
            console.print(f"Remote check: {server_error.get('message')}")
            if server_error.get("suggestion"):
                console.print(f"Next: {server_error['suggestion']}")
        return
    if "server_unit" in checks:
        server_unit = checks["server_unit"]
        socket = checks["socket"]
        console.print(
            f"server service: {server_unit['state']} "
            f"({'installed' if server_unit['installed'] else 'not installed'})"
        )
        console.print(
            f"socket: {'usable' if socket.get('state') == 'live' and socket['accessible'] else 'unavailable'}"
        )
        console.print(f"machinectl: {'available' if checks['machinectl'] else 'missing'}")
        if response.get("server"):
            server_checks = response["server"]["checks"]
            console.print(
                f"server catalog: {'valid' if server_checks['catalog']['valid'] else 'invalid'}"
            )
            unmanaged = server_checks.get("unmanaged_resources") or {}
            machines = unmanaged.get("machines") or server_checks.get("unmanaged_machines") or []
            if machines:
                console.print("unmanaged host machines: " + ", ".join(machines))
            if unmanaged.get("storage_entries"):
                console.print(
                    "unmanaged storage entries: " + ", ".join(unmanaged["storage_entries"])
                )
            if unmanaged.get("nspawn_configs"):
                console.print("unmanaged nspawn configs: " + ", ".join(unmanaged["nspawn_configs"]))
            if unmanaged.get("systemd_overrides"):
                console.print(
                    "unmanaged systemd overrides: " + ", ".join(unmanaged["systemd_overrides"])
                )
            if server_checks.get("machine_observation_error"):
                console.print(
                    "host observation: " + str(server_checks["machine_observation_error"])
                )
        server_error = response.get("server_error")
        if isinstance(server_error, dict):
            console.print(f"Server check: {server_error.get('message')}")
            if server_error.get("suggestion"):
                console.print(f"Next: {server_error['suggestion']}")
        elif server_error:
            console.print(f"Server check: {server_error}")
    else:
        console.print(f"machinectl: {'available' if checks['machinectl'] else 'missing'}")
        console.print(f"catalog: {'valid' if checks['catalog']['valid'] else 'invalid'}")
        console.print(
            f"container storage: {'available' if checks['machines_directory'] else 'missing'}"
        )
        if checks["catalog"]["error"]:
            console.print(f"Catalog error: {checks['catalog']['error']}")


async def _write_all_to_fd(fd: int, data: bytes) -> None:
    """Write every byte to a nonblocking fd without blocking the event loop."""
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
            await asyncio.wait_for(writable, timeout=STDOUT_WRITE_STALL)
        finally:
            try:
                loop.remove_writer(fd)
            except Exception:
                pass


def _write_blocking(fd: int, data: bytes) -> None:
    """Write every byte on a regular file descriptor."""
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "stdout write returned no bytes")
        offset += written


async def _write_all_stdout(
    data: bytes, *, file_stdout: bool, in_flight: set[asyncio.Task[None]]
) -> None:
    """Write every output byte using a descriptor-appropriate path."""
    if not data:
        return
    fd = sys.stdout.fileno()
    if file_stdout:
        task = asyncio.create_task(asyncio.to_thread(_write_blocking, fd, data))
        in_flight.add(task)
        try:
            await task
        finally:
            in_flight.discard(task)
        return
    await _write_all_to_fd(fd, data)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _start_control(command: list[str] | None, is_tty: bool) -> dict[str, Any]:
    """Build the stream-start frame, including TERM only for a real TTY."""
    payload: dict[str, Any] = {"type": "start", "tty": is_tty}
    if command is not None:
        payload["command"] = command
    term = select_client_term(os.environ.get("TERM"), is_tty=is_tty)
    if term is not None:
        payload["term"] = term
    return payload


def _tty_winsize(fd: int) -> tuple[int, int] | None:
    """Read rows/cols from a tty fd. Ignore COLUMNS/LINES environment overrides."""
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, _x, _y = struct.unpack("HHHH", packed)
    except OSError:
        return None
    if rows <= 0 or cols <= 0:
        return None
    return rows, cols


def _loop_signal_handler(
    loop: asyncio.AbstractEventLoop, sig: int
) -> tuple[Any, tuple[Any, ...]] | None:
    """Return the current asyncio signal callback, if the loop owns this signal."""
    handlers = getattr(loop, "_signal_handlers", None)
    if not isinstance(handlers, dict):
        return None
    handle = handlers.get(sig)
    if handle is None:
        return None
    callback = getattr(handle, "_callback", None)
    if callback is None:
        return None
    args = getattr(handle, "_args", ()) or ()
    return callback, tuple(args)


def _restore_loop_signal(
    loop: asyncio.AbstractEventLoop,
    sig: int,
    previous: tuple[Any, tuple[Any, ...]] | None,
    previous_signal: Any,
) -> None:
    """Remove this session's handler and put back the prior loop or libc handler."""
    try:
        loop.remove_signal_handler(sig)
    except Exception:
        pass
    if previous is not None:
        try:
            loop.add_signal_handler(sig, previous[0], *previous[1])
            return
        except Exception:
            pass
    try:
        signal.signal(sig, previous_signal)
    except Exception:
        pass


async def _proxy_terminal(
    websocket: Any,
    *,
    expect_status: bool,
    transport: TransportMode | None,
    start_command: list[str] | None = None,
) -> int:
    """Proxy local stdin/stdout to the server until a control completion arrives.

    Input EOF is forwarded as a control message; it is not session completion.
    Terminal bytes stay binary. Control frames are never written to stdout.
    """
    loop = asyncio.get_running_loop()
    stdin_queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue(
        maxsize=STDIN_QUEUE_LIMIT
    )
    is_tty = False
    fd: int | None = None
    stdout_fd: int | None = None
    old_settings = None
    old_flags = None
    stdout_flags = None
    reader_added = False
    stdin_paused = False
    file_task: asyncio.Task[None] | None = None
    file_stdout = False
    in_flight: set[asyncio.Task[None]] = set()
    previous_winch = signal.getsignal(signal.SIGWINCH)
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_loop_sigint: tuple[Any, tuple[Any, ...]] | None = None
    previous_loop_winch: tuple[Any, tuple[Any, ...]] | None = None
    sigint_installed = False
    winch_loop_installed = False
    outcome: dict[str, Any] | None = None
    eof_sent = False
    start_ready = False

    try:
        fd = sys.stdin.fileno()
        is_tty = sys.stdin.isatty()
        old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if is_tty:
            old_settings = termios.tcgetattr(fd)
    except Exception:
        fd = None
        is_tty = False

    try:
        stdout_fd = sys.stdout.fileno()
        stdout_flags = fcntl.fcntl(stdout_fd, fcntl.F_GETFL)
        file_stdout = stat.S_ISREG(os.fstat(stdout_fd).st_mode)
        if not file_stdout:
            fcntl.fcntl(stdout_fd, fcntl.F_SETFL, stdout_flags | os.O_NONBLOCK)
    except Exception:
        stdout_fd = None
        stdout_flags = None
        file_stdout = False

    def stop_stdin_reader() -> None:
        nonlocal reader_added, stdin_paused
        if reader_added and fd is not None:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            reader_added = False
        stdin_paused = True

    def resume_stdin_reader() -> None:
        nonlocal reader_added, stdin_paused
        if fd is None or file_task is not None:
            return
        if stdin_queue.qsize() >= STDIN_QUEUE_LIMIT:
            return
        if not reader_added:
            loop.add_reader(fd, on_stdin)
            reader_added = True
        stdin_paused = False

    def on_stdin() -> None:
        if fd is None:
            stdin_queue.put_nowait(None)
            stop_stdin_reader()
            return
        if stdin_queue.qsize() >= STDIN_QUEUE_LIMIT:
            stop_stdin_reader()
            return
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            stop_stdin_reader()
            try:
                stdin_queue.put_nowait(error)
            except asyncio.QueueFull:
                pass
            return
        if not data:
            stop_stdin_reader()
            try:
                stdin_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        try:
            stdin_queue.put_nowait(data)
        except asyncio.QueueFull:
            stop_stdin_reader()
        if stdin_queue.qsize() >= STDIN_QUEUE_LIMIT:
            stop_stdin_reader()

    def send_resize(*_args: Any) -> None:
        if not start_ready:
            return
        size_fd = stdout_fd if stdout_fd is not None else fd
        winsize = _tty_winsize(size_fd) if size_fd is not None else None
        if winsize is None and fd is not None and fd != size_fd:
            winsize = _tty_winsize(fd)
        if winsize is None:
            return
        rows, cols = winsize
        payload = encode_stream_control({"type": "resize", "cols": cols, "rows": rows})
        asyncio.run_coroutine_threadsafe(websocket.send(payload), loop)

    async def read_regular_file() -> None:
        assert fd is not None
        while True:
            data = await asyncio.to_thread(os.read, fd, 65536)
            if not data:
                await stdin_queue.put(None)
                return
            await stdin_queue.put(data)

    try:
        if fd is not None and is_tty and old_settings is not None and old_flags is not None:
            tty.setraw(fd)
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)
            signal.signal(signal.SIGWINCH, send_resize)
        stdin_mode = "none"
        if fd is not None:
            mode = os.fstat(fd).st_mode
            if stat.S_ISREG(mode):
                stdin_mode = "file"
            elif is_tty:
                stdin_mode = "tty"
            else:
                stdin_mode = "pipe"
                fcntl.fcntl(fd, fcntl.F_SETFL, (old_flags or 0) | os.O_NONBLOCK)
        if stdin_mode == "file":
            file_task = asyncio.create_task(read_regular_file())
        elif fd is not None:
            try:
                loop.add_reader(fd, on_stdin)
                reader_added = True
            except (PermissionError, OSError):
                file_task = asyncio.create_task(read_regular_file())
        else:
            stdin_queue.put_nowait(None)

        def inject_interrupt() -> None:
            try:
                stdin_queue.put_nowait(b"\x03")
            except asyncio.QueueFull:
                pass

        if is_tty:
            previous_loop_sigint = _loop_signal_handler(loop, signal.SIGINT)
            previous_loop_winch = _loop_signal_handler(loop, signal.SIGWINCH)
            loop.add_signal_handler(signal.SIGINT, inject_interrupt)
            sigint_installed = True
            try:
                loop.add_signal_handler(signal.SIGWINCH, send_resize)
                winch_loop_installed = True
            except (NotImplementedError, RuntimeError, ValueError):
                pass

        async def watch_winsize() -> None:
            last: tuple[int, int] | None = None
            while True:
                if start_ready:
                    size_fd = stdout_fd if stdout_fd is not None else fd
                    winsize = _tty_winsize(size_fd) if size_fd is not None else None
                    if winsize is not None and winsize != last:
                        last = winsize
                        rows, cols = winsize
                        await websocket.send(
                            encode_stream_control({"type": "resize", "cols": cols, "rows": rows})
                        )
                await asyncio.sleep(0.1)

        async def copy_from_server() -> None:
            nonlocal outcome
            async for message in websocket:
                if isinstance(message, bytes):
                    await _write_all_stdout(message, file_stdout=file_stdout, in_flight=in_flight)
                    continue
                if not isinstance(message, str):
                    continue
                control = parse_stream_control(message)
                if control is None:
                    continue
                kind = control.get("type")
                if kind == "complete":
                    outcome = control
                    return
                if kind == "error":
                    raise ClientError(
                        str(control.get("code") or "stream_failed"),
                        str(control.get("message") or "The terminal session failed."),
                        transport=transport,
                    )

        async def copy_to_server() -> None:
            nonlocal eof_sent
            while True:
                data = await stdin_queue.get()
                if isinstance(data, BaseException):
                    raise data
                if data is None:
                    if not eof_sent:
                        await websocket.send(encode_stream_control({"type": "stdin_eof"}))
                        eof_sent = True
                    return
                await websocket.send(data)
                if stdin_paused:
                    resume_stdin_reader()

        read_task = asyncio.create_task(copy_from_server())
        write_task: asyncio.Task[Any] | None = None
        winsize_task: asyncio.Task[Any] | None = None
        watched: set[asyncio.Task[Any]] = {read_task}
        if file_task is not None:
            watched.add(file_task)
        session_error: BaseException | None = None
        winsize_error: BaseException | None = None
        try:
            await websocket.send(encode_stream_control(_start_control(start_command, is_tty)))
            start_ready = True
            if is_tty:
                send_resize()
                winsize_task = asyncio.create_task(watch_winsize())
                watched.add(winsize_task)
            write_task = asyncio.create_task(copy_to_server())
            watched.add(write_task)
            while True:
                pending = {task for task in watched if not task.done()}
                if read_task.done():
                    read_task.result()
                    break
                if not pending:
                    break
                done, _pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled():
                        continue
                    error = task.exception()
                    if error is not None:
                        raise error
                if file_task is not None and file_task.done() and not file_task.cancelled():
                    file_task.result()
                if write_task is not None and write_task.done() and not write_task.cancelled():
                    write_task.result()
                if (
                    winsize_task is not None
                    and winsize_task.done()
                    and not winsize_task.cancelled()
                ):
                    winsize_task.result()
                if read_task.done():
                    read_task.result()
                    break
        except Exception as error:
            session_error = error
            raise
        finally:
            if write_task is not None and not write_task.done():
                write_task.cancel()
            if not read_task.done():
                read_task.cancel()
            if file_task is not None and not file_task.done():
                file_task.cancel()
            if winsize_task is not None:
                if not winsize_task.done():
                    winsize_task.cancel()
                try:
                    await winsize_task
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    winsize_error = error
            if write_task is not None:
                await asyncio.gather(write_task, return_exceptions=True)
            if file_task is not None:
                await asyncio.gather(file_task, return_exceptions=True)
            await asyncio.gather(read_task, return_exceptions=True)
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
        if session_error is None and winsize_error is not None:
            raise winsize_error
    finally:
        stop_stdin_reader()
        if sigint_installed:
            _restore_loop_signal(loop, signal.SIGINT, previous_loop_sigint, previous_sigint)
        if winch_loop_installed:
            _restore_loop_signal(loop, signal.SIGWINCH, previous_loop_winch, previous_winch)
        elif is_tty:
            signal.signal(signal.SIGWINCH, previous_winch)
        if old_settings is not None and fd is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        if old_flags is not None and fd is not None:
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
        if stdout_flags is not None and stdout_fd is not None:
            fcntl.fcntl(stdout_fd, fcntl.F_SETFL, stdout_flags)

    if outcome is None:
        raise ClientError(
            "stream_failed",
            "The terminal session ended without a completion status.",
            suggestion="Retry the command and inspect the server journal.",
            transport=transport,
        )
    if expect_status:
        status = outcome.get("exit_status")
        if not isinstance(status, int):
            raise ClientError(
                "stream_failed",
                "The exec session finished without a known command exit status.",
                suggestion="Retry the command and inspect the server journal.",
                transport=transport,
            )
        return status
    return 0


def stream_terminal(
    client: ChimeraClient, name: str, command: list[str] | None, timeout: float
) -> int:
    """Open a shell or command session and return the known process status.

    Exec returns the guest command's exit status when the server reports it.
    Shell returns 0 after a completed session and does not invent a guest
    status. Missing completion evidence is an error, not success.
    """
    operation = "exec" if command is not None else "shell"
    client.request(
        "stream_preflight",
        {"name": name, "operation": operation},
        timeout=timeout,
    )

    async def run() -> int:
        endpoint = "/api/v1/stream/exec" if command is not None else "/api/v1/stream/shell"
        async with client.stream_connect(endpoint, {"name": name}, timeout=timeout) as websocket:
            return await _proxy_terminal(
                websocket,
                expect_status=command is not None,
                transport=client.mode,
                start_command=command,
            )

    try:
        return asyncio.run(run())
    except ClientError:
        raise
    except Exception as error:
        raise ClientError(
            "stream_failed",
            "Could not complete the terminal session.",
            detail=str(error),
            suggestion="Retry the command and inspect the server journal.",
            transport=client.mode,
        ) from error


def stream_logs(
    client: ChimeraClient,
    name: str,
    *,
    unit: str | None,
    lines: int,
    follow: bool,
    supervisor: bool,
    timeout: float,
) -> int:
    """Stream server-host journalctl output over the existing WebSocket transport."""
    operation = "supervisor_logs" if supervisor else "logs"
    client.request(
        "stream_preflight",
        {"name": name, "operation": operation},
        timeout=timeout,
    )

    async def run() -> int:
        params: dict[str, Any] = {
            "name": name,
            "lines": lines,
            "follow": "1" if follow else "0",
            "supervisor": "1" if supervisor else "0",
        }
        if unit is not None:
            params["unit"] = unit
        async with client.stream_connect(
            "/api/v1/stream/logs", params, timeout=timeout
        ) as websocket:
            async for message in websocket:
                if isinstance(message, bytes):
                    await asyncio.to_thread(_write_blocking, sys.stdout.fileno(), message)
                    continue
                if not isinstance(message, str):
                    continue
                control = parse_stream_control(message)
                if control is None:
                    continue
                if control.get("type") == "error":
                    raise ClientError(
                        str(control.get("code") or "stream_failed"),
                        str(control.get("message") or "The journal stream failed."),
                        transport=client.mode,
                    )
                if control.get("type") == "complete":
                    status = control.get("exit_status")
                    if not isinstance(status, int):
                        raise ClientError(
                            "stream_failed",
                            "The journal stream ended without an exit status.",
                            transport=client.mode,
                        )
                    return status
        raise ClientError(
            "stream_failed",
            "The journal stream ended without a completion status.",
            transport=client.mode,
        )

    try:
        return asyncio.run(run())
    except ClientError:
        raise
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        raise ClientError(
            "stream_failed",
            "Could not complete the journal stream.",
            detail=str(error),
            transport=client.mode,
        ) from error
