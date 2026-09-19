"""Real CLI stream behavior under a temporary PTY and a pipe."""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from chimera.server.api import ApiServer
from tests.support.stream import (
    INTERRUPT_PY,
    RESIZE_PY,
    bind_local_stream_argv,
    staged_argv,
)

ROOT = Path(__file__).resolve().parents[2]


def _imported_package_root() -> Path:
    """Directory that contains the chimera package loaded by this test process."""
    import chimera

    return Path(chimera.__file__).resolve().parent.parent


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    # pybuild copies chimera next to tests/, not under src/. Point subprocesses
    # at the imported package so they cannot pick up a host install.
    env["PYTHONPATH"] = str(_imported_package_root())
    env.pop("CHIMERA_HOST", None)
    env.pop("CHIMERA_SOCKET", None)
    return env


@pytest.fixture
def service():
    command_service = Mock()
    command_service.execute = AsyncMock(return_value={"ok": True})
    command_service.authorize = Mock()
    command_service.state_engine = Mock()
    command_service.state_engine.validate_stream_target = AsyncMock()
    return command_service


def _run_cli_pty(
    argv: list[str],
    *,
    answer: bytes | None = b"pong\n",
    resize: tuple[int, int] | None = None,
    interrupt: bool = False,
    timeout: float = 8,
) -> tuple[bytes, int | None, bool, float]:
    """Drive the real CLI under a PTY. helper_killed True means the assertion must fail."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        argv,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=_cli_env(),
        cwd=str(ROOT),
        close_fds=True,
    )
    os.close(slave)
    collected = bytearray()
    started = time.monotonic()
    deadline = started + timeout
    answered = False
    resized = False
    interrupted = False
    helper_killed = False
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    collected.extend(chunk)
                    if answer is not None and not answered and b"PROMPT" in collected:
                        os.write(master, answer if answer.endswith(b"\n") else answer + b"\n")
                        answered = True
                    if resize is not None and not resized and b"READY" in collected:
                        fcntl.ioctl(
                            master,
                            termios.TIOCSWINSZ,
                            struct.pack("HHHH", resize[0], resize[1], 0, 0),
                        )
                        os.kill(process.pid, signal.SIGWINCH)
                        time.sleep(0.15)
                        os.write(master, b"go\n")
                        resized = True
                    if interrupt and not interrupted and b"WAITING" in collected:
                        os.write(master, b"\x03")
                        interrupted = True
            if process.poll() is not None:
                while True:
                    ready, _, _ = select.select([master], [], [], 0.05)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    collected.extend(chunk)
                break
        if process.poll() is None:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                helper_killed = True
                process.kill()
                process.wait(timeout=2)
        return bytes(collected), process.returncode, helper_killed, time.monotonic() - started
    finally:
        if process.poll() is None:
            helper_killed = True
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        os.close(master)


@pytest.mark.asyncio
async def test_cli_pty_preserves_input_output_and_exit_status(service, tmp_path):
    """The real CLI under a PTY forwards answers and returns guest status 7."""

    socket_path = tmp_path / "server.sock"
    server = ApiServer(socket_path, service, "chimera-admin")
    bind_local_stream_argv(server)
    await server.start()
    try:
        argv = [
            sys.executable,
            "-s",
            "-m",
            "chimera.cli",
            "exec",
            "demo",
            "--socket",
            str(socket_path),
            "--",
            *staged_argv(7),
        ]
        collected, returncode, helper_killed, _elapsed = await asyncio.to_thread(_run_cli_pty, argv)
        text = collected.decode("utf-8", errors="replace")
        assert not helper_killed
        assert "FIRST" in text
        assert "ECHO:pong" in text
        assert returncode == 7
        assert "complete" not in text
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_cli_pipe_forwards_stdin_and_nonzero_status(service, tmp_path):
    """Non-TTY stdin still reaches the process and preserves exit status."""

    socket_path = tmp_path / "server.sock"
    server = ApiServer(socket_path, service, "chimera-admin")
    bind_local_stream_argv(server)
    await server.start()
    try:
        argv = [
            sys.executable,
            "-s",
            "-m",
            "chimera.cli",
            "exec",
            "demo",
            "--socket",
            str(socket_path),
            "--",
            *staged_argv(7),
        ]
        result = await asyncio.to_thread(
            subprocess.run,
            argv,
            input=b"piped\n",
            capture_output=True,
            env=_cli_env(),
            cwd=str(ROOT),
            timeout=8,
            check=False,
        )
        text = result.stdout.decode("utf-8", errors="replace") + result.stderr.decode(
            "utf-8", errors="replace"
        )
        assert "FIRST" in text
        assert "ECHO:piped" in text
        assert result.returncode == 7
    finally:
        await server.stop()


def _cli_exec_argv(socket_path: Path, *command: str) -> list[str]:
    return [
        sys.executable,
        "-s",
        "-m",
        "chimera.cli",
        "exec",
        "demo",
        "--socket",
        str(socket_path),
        "--",
        *command,
    ]


@pytest.mark.asyncio
async def test_cli_pty_resize_and_interrupt(service, tmp_path):
    """SIGWINCH updates the guest PTY and Ctrl-C interrupts a waiting command."""

    socket_path = tmp_path / "server.sock"
    server = ApiServer(socket_path, service, "chimera-admin")
    bind_local_stream_argv(server)
    await server.start()
    try:
        resize_argv = _cli_exec_argv(socket_path, sys.executable, "-c", RESIZE_PY)
        collected, returncode, helper_killed, _elapsed = await asyncio.to_thread(
            _run_cli_pty, resize_argv, answer=None, resize=(31, 92)
        )
        text = collected.decode("utf-8", errors="replace")
        assert not helper_killed
        assert "SIZE:31x92" in text
        assert returncode == 0
        interrupt_argv = _cli_exec_argv(socket_path, sys.executable, "-c", INTERRUPT_PY)
        interrupted, interrupt_status, interrupt_killed, elapsed = await asyncio.to_thread(
            _run_cli_pty, interrupt_argv, answer=None, interrupt=True, timeout=12
        )
        interrupt_text = interrupted.decode("utf-8", errors="replace")
        assert not interrupt_killed
        assert "WAITING" in interrupt_text
        assert "INTERRUPTED" in interrupt_text
        assert "NOT_REACHED" not in interrupt_text
        assert interrupt_status == 130
        assert elapsed < 8
    finally:
        await server.stop()
