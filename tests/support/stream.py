"""Helpers for real subprocess stream-protocol tests.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from chimera.cli.client import ChimeraClient
from chimera.endpoint import encode_stream_control, parse_stream_control

STAGED_PY = """
import sys
import time
print("FIRST", flush=True)
time.sleep(0.4)
print("SECOND", flush=True)
print("PROMPT", flush=True)
line = sys.stdin.readline()
print("ECHO:" + line.rstrip("\\r\\n"), flush=True)
raise SystemExit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
"""

EOF_PY = """
import sys
print("BEFORE", flush=True)
sys.stdin.read()
print("AFTER_EOF", flush=True)
"""

RESIZE_PY = """
import fcntl
import struct
import sys
import termios
print("READY", flush=True)
sys.stdin.readline()
packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\\0" * 8)
rows, cols, _x, _y = struct.unpack("HHHH", packed)
print(f"SIZE:{rows}x{cols}", flush=True)
"""

SLEEP_PY = "import time; time.sleep(30)"

INTERRUPT_PY = """
import fcntl
import os
import signal
import sys
import termios
import time
def handle(_signum, _frame):
    os.write(1, b"INTERRUPTED\\n")
    raise SystemExit(130)
signal.signal(signal.SIGINT, handle)
try:
    fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
except OSError:
    pass
os.write(1, b"WAITING\\n")
time.sleep(30)
os.write(1, b"NOT_REACHED\\n")
raise SystemExit(1)
"""

THROTTLE_PY = """
import os
import sys
payload = bytes(range(256)) * 800
os.write(sys.stdout.fileno(), payload)
os._exit(0)
"""


def staged_argv(exit_status: int = 0) -> list[str]:
    """Return a local interpreter invocation of the staged output/input program."""
    return [sys.executable, "-c", STAGED_PY, str(exit_status)]


def bind_local_stream_argv(server: Any) -> None:
    """Use submitted argv as a host subprocess instead of systemd-run/machinectl."""

    def _stream_argv(
        _container_name: str,
        command: list[str] | None,
        *,
        use_tty: bool = True,
        term: str | None = None,
    ) -> list[str]:
        if command is None:
            return [sys.executable, "-c", SLEEP_PY]
        return list(command)

    server._stream_argv = _stream_argv


async def run_exec_session(
    client: ChimeraClient,
    command: list[str],
    *,
    answer: bytes | None = None,
    resize: tuple[int, int] | None = None,
    send_eof: bool = False,
    stdin_payload: bytes | None = None,
    stdin_chunk_size: int = 16384,
    stdin_complete: bool = True,
    tty: bool = True,
    timeout: float = 8.0,
) -> tuple[bytes, dict[str, Any] | None, float | None]:
    """Drive one exec stream and return terminal bytes, completion, and FIRST timing."""
    output = bytearray()
    complete: dict[str, Any] | None = None
    first_at: float | None = None
    answered = False
    payload_sent = stdin_payload is None
    started = time.monotonic()
    async with client.stream_connect(
        "/api/v1/stream/exec", {"name": "demo"}, timeout=5
    ) as websocket:
        await websocket.send(
            encode_stream_control({"type": "start", "command": command, "tty": tty})
        )
        if resize is not None:
            await websocket.send(
                encode_stream_control({"type": "resize", "rows": resize[0], "cols": resize[1]})
            )
        if stdin_payload is not None and not tty and not payload_sent:
            for index in range(0, len(stdin_payload), stdin_chunk_size):
                await websocket.send(stdin_payload[index : index + stdin_chunk_size])
            if stdin_complete:
                await websocket.send(encode_stream_control({"type": "stdin_eof"}))
            payload_sent = True
        while True:
            message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            if isinstance(message, bytes):
                output.extend(message)
                if first_at is None and b"FIRST" in output:
                    first_at = time.monotonic() - started
                if send_eof and not answered and b"BEFORE" in output:
                    await websocket.send(encode_stream_control({"type": "stdin_eof"}))
                    answered = True
                if stdin_payload is not None and not payload_sent and b"READY" in output:
                    for index in range(0, len(stdin_payload), stdin_chunk_size):
                        await websocket.send(stdin_payload[index : index + stdin_chunk_size])
                    if stdin_complete:
                        await websocket.send(encode_stream_control({"type": "stdin_eof"}))
                    payload_sent = True
                if (
                    answer is not None
                    and not answered
                    and (b"PROMPT" in output or b"READY" in output)
                ):
                    await websocket.send(answer if answer.endswith(b"\n") else answer + b"\n")
                    answered = True
                continue
            if not isinstance(message, str):
                continue
            control = parse_stream_control(message)
            if control is None:
                continue
            if control.get("type") == "complete":
                complete = control
                break
            if control.get("type") == "error":
                complete = control
                break
    return bytes(output), complete, first_at
