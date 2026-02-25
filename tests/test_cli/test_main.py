"""Tests for CLI main module."""

import pytest
from unittest.mock import MagicMock, patch

import typer

from chimera.cli.main import _run_cli_command
from chimera.cli.client import IPCError


@patch("chimera.cli.main.IPCClient")
@patch("chimera.cli.main.console")
def test_run_cli_command_success(mock_console, mock_ipc_client):
    """Test the CLI command runner on a successful execution."""
    mock_handler = MagicMock()
    
    _run_cli_command(mock_handler, socket="/tmp/test.sock", arg1="value1")
    
    # Verify IPCClient was instantiated correctly
    mock_ipc_client.assert_called_once_with(socket_path="/tmp/test.sock")
    
    # Verify the handler was called with the client and arguments
    mock_handler.assert_called_once_with(mock_ipc_client.return_value, arg1="value1")
    
    # Verify no error was printed
    mock_console.print.assert_not_called()


@patch("chimera.cli.main.IPCClient")
@patch("chimera.cli.main.console")
def test_run_cli_command_ipc_error(mock_console, mock_ipc_client):
    """Test the CLI command runner when an IPCError is raised."""
    mock_handler = MagicMock(side_effect=IPCError("Agent not found"))
    
    with pytest.raises(typer.Exit) as exc_info:
        _run_cli_command(mock_handler, socket="/tmp/test.sock", arg1="value1")
        
    # Verify IPCClient was instantiated
    mock_ipc_client.assert_called_once_with(socket_path="/tmp/test.sock")
    
    # Verify the handler was called
    mock_handler.assert_called_once_with(mock_ipc_client.return_value, arg1="value1")
    
    # Verify the error message was printed to the console
    mock_console.print.assert_called_once_with("[red]Error:[/red] Agent not found")
    
    # Verify typer.Exit was called
    assert exc_info.value.exit_code == 1
