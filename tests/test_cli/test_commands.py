"""Tests for CLI command implementations."""

import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from chimera.cli.commands import exec_in_container, shell_in_container


class TestStreamingCommands:
    """Tests for the interactive stream-based commands."""

    @patch("chimera.cli.commands._proxy_terminal", new_callable=AsyncMock)
    def test_exec_in_container_uses_stream_request(self, mock_proxy):
        """Verify exec_in_container calls stream_request and proxies terminal."""
        mock_client = MagicMock()
        mock_ctx = AsyncMock()
        mock_client.stream_connect.return_value = mock_ctx
        mock_ws = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_ws

        command = ["ls", "-la", "/root"]
        
        # This calls the sync wrapper which internally uses asyncio.run()
        # By NOT mocking asyncio.run, we allow the real event loop to execute
        # and await our AsyncMocks naturally.
        exec_in_container(
            client=mock_client,
            name="test-container",
            command=command,
        )
        
        # Assertions run after the loop has closed
        mock_client.stream_connect.assert_called_once()
        args, kwargs = mock_client.stream_connect.call_args
        assert args[0] == "/api/v1/stream/exec"
        assert args[1]["name"] == "test-container"
        assert "ls" in args[1]["command"]
        
        mock_proxy.assert_called_once_with(mock_ws)

    @patch("chimera.cli.commands._proxy_terminal", new_callable=AsyncMock)
    def test_shell_in_container_uses_stream_request(self, mock_proxy):
        """Verify shell_in_container calls stream_request and proxies terminal."""
        mock_client = MagicMock()
        mock_ctx = AsyncMock()
        mock_client.stream_connect.return_value = mock_ctx
        mock_ws = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_ws

        # Execute naturally
        shell_in_container(
            client=mock_client,
            name="test-container",
        )

        # Assertions run after the loop has closed
        mock_client.stream_connect.assert_called_once()
        args, kwargs = mock_client.stream_connect.call_args
        assert args[0] == "/api/v1/stream/shell"
        assert args[1]["name"] == "test-container"
        
        mock_proxy.assert_called_once_with(mock_ws)
