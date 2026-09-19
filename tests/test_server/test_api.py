"""Tests for the active server HTTP/WebSocket transport."""

import asyncio
import os
import socket
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from chimera.errors import ChimeraError
from chimera.server.api import ApiServer
from chimera.server.service import PeerCredentials


def _unix_admin() -> PeerCredentials:
    return PeerCredentials(transport="unix", uid=0, pid=42, gids=(0,))


def _patch_identity(server: ApiServer):
    return patch.object(server, "_caller_identity", return_value=_unix_admin())


@pytest.fixture
def service():
    """Provide a transport-independent command service mock."""
    command_service = Mock()
    command_service.execute = AsyncMock(
        return_value={"server": {"running": True}, "containers": {}}
    )
    return command_service


@pytest.mark.asyncio
async def test_server_returns_stable_success_envelope(service, tmp_path):
    """The transport delegates to the service and does not expose internals."""
    server_logic = ApiServer(tmp_path / "server.sock", service, "chimera-admin")
    client = TestClient(TestServer(server_logic.app))
    await client.start_server()
    try:
        with _patch_identity(server_logic):
            response = await client.post("/api/v1/command", json={"command": "status", "args": {}})
        assert response.status == 200
        assert await response.json() == {
            "success": True,
            "data": {"server": {"running": True}, "containers": {}},
        }
        service.execute.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_server_returns_structured_expected_error(service, tmp_path):
    """Client diagnostics can distinguish expected failures by code."""
    service.execute.side_effect = ChimeraError(
        code="permission_denied",
        message="Permission denied.",
        suggestion="Ask an administrator.",
        status=403,
    )
    server_logic = ApiServer(tmp_path / "server.sock", service, "chimera-admin")
    client = TestClient(TestServer(server_logic.app))
    await client.start_server()
    try:
        with _patch_identity(server_logic):
            response = await client.post("/api/v1/command", json={"command": "delete", "args": {}})
        payload = await response.json()
        assert response.status == 403
        assert payload["error"]["code"] == "permission_denied"
        assert payload["error"]["suggestion"] == "Ask an administrator."
    finally:
        await client.close()


def test_stale_unix_socket_is_the_only_replaceable_socket_path(service, tmp_path):
    """Safe stale recovery must preserve regular files, links, and directories."""
    socket_path = tmp_path / "server.sock"
    unix_socket = socket.socket(socket.AF_UNIX)
    unix_socket.bind(str(socket_path))
    unix_socket.close()
    server_logic = ApiServer(socket_path, service, "chimera-admin")

    server_logic._remove_stale_socket()
    assert not os.path.lexists(socket_path)

    socket_path.write_text("do not unlink", encoding="utf-8")
    with pytest.raises(ChimeraError, match="Refusing to replace"):
        server_logic._remove_stale_socket()
    assert socket_path.read_text(encoding="utf-8") == "do not unlink"

    socket_path.unlink()
    socket_path.symlink_to(tmp_path / "target")
    with pytest.raises(ChimeraError, match="Refusing to replace"):
        server_logic._remove_stale_socket()
    assert socket_path.is_symlink()

    socket_path.unlink()
    socket_path.mkdir()
    with pytest.raises(ChimeraError, match="Refusing to replace"):
        server_logic._remove_stale_socket()
    assert socket_path.is_dir()


def test_live_unix_socket_is_never_unlinked(service, tmp_path):
    """A listening server socket must not be treated as stale."""
    socket_path = tmp_path / "server.sock"
    unix_socket = socket.socket(socket.AF_UNIX)
    unix_socket.bind(str(socket_path))
    unix_socket.listen(1)
    try:
        server_logic = ApiServer(socket_path, service, "chimera-admin")
        with pytest.raises(ChimeraError, match="already listening"):
            server_logic._remove_stale_socket()
        assert os.path.lexists(socket_path)
    finally:
        unix_socket.close()


@pytest.mark.asyncio
async def test_unix_socket_http_round_trip(service, tmp_path):
    """The public Unix HTTP path serves commands through ChimeraClient."""
    socket_path = tmp_path / "subdir" / "server.sock"
    service.authorize = Mock()
    server = ApiServer(socket_path, service, "chimera-admin")
    await server.start()
    try:
        from chimera.cli.client import ChimeraClient

        result = await asyncio.to_thread(ChimeraClient(str(socket_path)).request, "status", {})
        assert result["server"]["running"] is True
    finally:
        await server.stop()
