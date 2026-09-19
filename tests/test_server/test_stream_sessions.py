"""Unix and shared stream-preservation tests against the submitted protocol."""

from __future__ import annotations

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

import pytest

from chimera.cli.client import ChimeraClient
from chimera.endpoint import encode_stream_control
from chimera.server.api import ApiServer
from tests.support.stream import (
    EOF_PY,
    RESIZE_PY,
    THROTTLE_PY,
    bind_local_stream_argv,
    run_exec_session,
    staged_argv,
)

ACCESS_SENTINEL = "chimera-r4-access-sentinel-7f3c"


@pytest.fixture
def service():
    command_service = Mock()
    command_service.execute = AsyncMock(return_value={"server": {"running": True}})
    command_service.authorize = Mock()
    command_service.state_engine = Mock()
    command_service.state_engine.validate_stream_target = AsyncMock()
    return command_service


async def _start_unix_server(tmp_path, service) -> tuple[ApiServer, ChimeraClient]:
    socket_path = tmp_path / "server.sock"
    server = ApiServer(socket_path, service, "chimera-admin")
    bind_local_stream_argv(server)
    await server.start()
    return server, ChimeraClient(socket_path=str(socket_path), timeout=5)


@pytest.mark.asyncio
async def test_unix_staged_output_input_and_exit_status(service, tmp_path):
    """FIRST is visible before completion; a piped answer is echoed; exit 7 is reported."""
    server, client = await _start_unix_server(tmp_path, service)
    try:
        output, complete, first_at = await run_exec_session(client, staged_argv(7), answer=b"pong")
        text = output.decode("utf-8", errors="replace")
        assert "FIRST" in text
        assert "SECOND" in text
        assert "ECHO:pong" in text
        assert "complete" not in text
        assert '"exit_status"' not in text
        assert complete is not None
        assert complete.get("type") == "complete"
        assert complete.get("exit_status") == 7
        assert first_at is not None
        assert first_at < 1.0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unix_input_eof_keeps_subsequent_output(service, tmp_path):
    """stdin EOF is not process completion; later output still arrives."""
    server, client = await _start_unix_server(tmp_path, service)
    try:
        output, complete, _ = await run_exec_session(
            client, [sys.executable, "-c", EOF_PY], send_eof=True, tty=False
        )
        text = output.decode("utf-8", errors="replace")
        assert "BEFORE" in text
        assert "AFTER_EOF" in text
        assert complete is not None
        assert complete.get("type") == "complete"
        assert complete.get("exit_status") == 0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unix_resize_reaches_pty(service, tmp_path):
    """A resize control message updates the child PTY window size."""
    server, client = await _start_unix_server(tmp_path, service)
    try:
        output, complete, _ = await run_exec_session(
            client,
            [sys.executable, "-c", RESIZE_PY],
            answer=b"go",
            resize=(31, 92),
        )
        text = output.decode("utf-8", errors="replace")
        assert "SIZE:31x92" in text
        assert complete is not None
        assert complete.get("exit_status") == 0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unix_disconnect_reaps_owned_child(service, tmp_path):
    """Closing the client does not leave the session child running."""
    server, client = await _start_unix_server(tmp_path, service)
    holder: dict[str, asyncio.subprocess.Process] = {}
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        process = await real_exec(*args, **kwargs)
        if args and args[0] != "systemctl":
            holder["process"] = process
        return process

    try:
        with patch("chimera.server.api.asyncio.create_subprocess_exec", side_effect=spy):
            async with client.stream_connect(
                "/api/v1/stream/exec", {"name": "demo"}, timeout=5
            ) as websocket:
                await websocket.send(
                    encode_stream_control(
                        {
                            "type": "start",
                            "command": [sys.executable, "-c", "import time; time.sleep(30)"],
                        }
                    )
                )
                await asyncio.sleep(0.2)
                assert holder["process"].returncode is None
        await asyncio.sleep(0.8)
        assert holder["process"].returncode is not None
    finally:
        await server.stop()


def test_stream_argv_propagates_validated_term_only_on_tty(service, tmp_path):
    """TTY exec/shell pass one --setenv=TERM=... item; pipe exec does not."""
    server = ApiServer(tmp_path / "server.sock", service, "chimera-admin")
    command = ["printf", "%s", "$HOME", "--dashed"]
    tty = server._stream_argv("demo", command, use_tty=True, term="xterm-256color")
    assert "--pty" in tty
    assert "--setenv=TERM=xterm-256color" in tty
    assert tty[tty.index("--") + 1 :] == command
    assert tty.index("--setenv=TERM=xterm-256color") < tty.index("--")
    piped = server._stream_argv("demo", command, use_tty=False, term="xterm-256color")
    assert "--pipe" in piped
    assert "--pty" not in piped
    assert all(not part.startswith("--setenv=") for part in piped)
    assert piped[piped.index("--") + 1 :] == command
    assert server._stream_argv("demo", None, term="tmux-256color") == [
        "machinectl",
        "--setenv=TERM=tmux-256color",
        "shell",
        "demo",
    ]
    assert server._stream_argv("demo", None, use_tty=False, term="xterm-256color") == [
        "machinectl",
        "shell",
        "demo",
    ]
    injected = server._stream_argv("demo", command, term="xterm\n--evil")
    assert all(not part.startswith("--setenv=") for part in injected)
    assert injected[injected.index("--") + 1 :] == command


@pytest.mark.asyncio
async def test_forced_output_failure_is_not_complete_success(service, tmp_path):
    """A stalled or failed sender cannot report complete with exit 0."""
    from aiohttp import web as aiohttp_web

    async def boom(self, data, *args, **kwargs):
        raise ConnectionError("forced delivery failure")

    server, client = await _start_unix_server(tmp_path, service)
    try:
        with patch.object(aiohttp_web.WebSocketResponse, "send_bytes", boom):
            _output, complete, _ = await run_exec_session(
                client, [sys.executable, "-c", THROTTLE_PY], tty=False, timeout=8
            )
        assert complete is not None
        assert complete.get("type") == "error"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exec_sentinel_is_absent_from_access_and_audit_logs(service, tmp_path, caplog):
    """Exec arguments do not appear in sanitized access logs or identity audit lines."""
    server, client = await _start_unix_server(tmp_path, service)
    http_logger = logging.getLogger("chimera.server.http")
    http_logger.setLevel(logging.INFO)
    http_logger.propagate = True
    logging.getLogger("aiohttp.access").setLevel(logging.INFO)
    logging.getLogger("aiohttp.access").propagate = True
    try:
        with caplog.at_level(logging.DEBUG):
            await run_exec_session(client, ["/bin/true", ACCESS_SENTINEL])
            await run_exec_session(client, [f"/no/such/{ACCESS_SENTINEL}", ACCESS_SENTINEL])
        access_and_audit = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name.startswith(("chimera.server", "aiohttp"))
        )
        assert ACCESS_SENTINEL not in access_and_audit
        assert quote(ACCESS_SENTINEL, safe="") not in access_and_audit
        assert "Authorized stream_exec" in access_and_audit
        assert "transport=unix" in access_and_audit
    finally:
        await server.stop()
