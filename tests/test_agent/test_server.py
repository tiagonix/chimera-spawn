"""Tests for Agent HTTP/WebSocket Server."""

import pytest
import json
import asyncio
import time
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch

from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from chimera.agent.server import AgentServer


@pytest.fixture
def mock_dependencies():
    """Create mock engine and config manager."""
    state_engine = AsyncMock()
    config_manager = AsyncMock()
    
    # Setup state engine mocks
    state_engine.get_all_container_statuses.return_value = {}
    state_engine.last_reconciliation = None
    
    # Setup config manager mocks
    config_manager.images = {}
    config_manager.profiles = {}
    config_manager.containers = {}
    
    return state_engine, config_manager


@pytest.mark.asyncio
async def test_server_rest_command(mock_dependencies, tmp_path):
    """Test standard REST command handling."""
    state_engine, config_manager = mock_dependencies
    server_logic = AgentServer(tmp_path / "sock", None, 0, state_engine, config_manager)
    
    server = TestServer(server_logic.app)
    client = TestClient(server)
    await client.start_server()
    
    try:
        # Test valid command
        resp = await client.post("/api/v1/command", json={
            "command": "status",
            "args": {}
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        
        # Test invalid command
        resp = await client.post("/api/v1/command", json={
            "command": "invalid_cmd",
            "args": {}
        })
        assert resp.status == 500
        data = await resp.json()
        assert data["success"] is False
        assert "Unknown command" in data["error"]
        
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_server_websocket_stream(mock_dependencies, tmp_path):
    """Test WebSocket streaming and resize handling."""
    state_engine, config_manager = mock_dependencies
    server_logic = AgentServer(tmp_path / "sock", None, 0, state_engine, config_manager)
    
    server = TestServer(server_logic.app)
    client = TestClient(server)
    await client.start_server()
    
    def mock_os_read(*args, **kwargs):
        """Block the thread executor slightly to prevent immediate FIRST_COMPLETED termination."""
        time.sleep(0.5)
        return b""
        
    try:
        # We must patch pty.openpty and subprocess to avoid actual execution
        with patch("pty.openpty") as mock_openpty, \
             patch("asyncio.create_subprocess_exec") as mock_exec, \
             patch("os.close"), \
             patch("os.read", side_effect=mock_os_read), \
             patch("fcntl.ioctl") as mock_ioctl:
             
            # Mock PTY fds
            mock_openpty.return_value = (1, 2)
            
            # Mock process
            mock_proc = AsyncMock()
            mock_proc.terminate = Mock()  # Use sync mock for synchronous method
            mock_proc.wait.return_value = None
            mock_exec.return_value = mock_proc
            
            async with client.ws_connect("/api/v1/stream/shell?name=test") as ws:
                # Send binary data (stdin)
                await ws.send_bytes(b"ls\n")
                
                # Send resize command (text)
                resize_payload = json.dumps({"type": "resize", "cols": 100, "rows": 40})
                await ws.send_str(resize_payload)
                
                # Allow event loop to process
                await asyncio.sleep(0.1)
                
                # Verify ioctl called
                mock_ioctl.assert_called()
                
    finally:
        await client.close()
