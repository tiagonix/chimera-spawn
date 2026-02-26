"""Tests for CLI command implementations."""

import pytest
from unittest.mock import MagicMock, patch
from chimera.cli.commands import exec_in_container, shell_in_container


class TestStreamingCommands:
    """Tests for the interactive stream-based commands."""

    @patch("chimera.cli.commands._proxy_terminal")
    def test_exec_in_container_uses_stream_request(self, mock_proxy):
        """Verify exec_in_container calls stream_request and proxies terminal."""
        mock_client = MagicMock()
        mock_sock = MagicMock()
        mock_client.stream_request.return_value = mock_sock

        command = ["ls", "-la", "/root"]
        exec_in_container(
            client=mock_client,
            name="test-container",
            command=command,
        )

        mock_client.stream_request.assert_called_once_with(
            "stream_exec", {"name": "test-container", "command": command}
        )
        mock_proxy.assert_called_once_with(mock_sock)

    @patch("chimera.cli.commands._proxy_terminal")
    def test_shell_in_container_uses_stream_request(self, mock_proxy):
        """Verify shell_in_container calls stream_request and proxies terminal."""
        mock_client = MagicMock()
        mock_sock = MagicMock()
        mock_client.stream_request.return_value = mock_sock

        shell_in_container(
            client=mock_client,
            name="test-container",
        )

        mock_client.stream_request.assert_called_once_with(
            "stream_shell", {"name": "test-container"}
        )
        mock_proxy.assert_called_once_with(mock_sock)
