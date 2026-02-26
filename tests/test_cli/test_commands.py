"""Tests for CLI command implementations."""

import pytest
import sys
import shlex
from unittest.mock import MagicMock, patch, call

from chimera.cli.commands import exec_in_container


class TestExecCommand:
    """Tests for the exec_in_container command handler."""

    @patch("subprocess.run")
    def test_exec_in_container_runs_subprocess(self, mock_run):
        """Verify exec_in_container calls subprocess.run correctly."""
        # Configure the mock to simulate success (returncode 0)
        mock_run.return_value = MagicMock(returncode=0)

        command = ["ls", "-la", "/root"]
        
        # Call the function with command arguments
        exec_in_container(
            name="test-container",
            command=command,
        )

        # Verify subprocess.run was called with the correct arguments,
        # including the shell wrapper and shlex-joined command.
        expected_cmd_str = shlex.join(command)
        mock_run.assert_called_once_with(
            ["machinectl", "shell", "test-container", "/bin/bash", "-c", expected_cmd_str],
            check=False
        )

    @patch("subprocess.run")
    def test_exec_in_container_raises_on_failure(self, mock_run):
        """Verify exec_in_container raises SystemExit on failure."""
        # Mock a failed return code
        mock_run.return_value = MagicMock(returncode=127)

        with pytest.raises(SystemExit) as exc_info:
            exec_in_container(
                name="test-container",
                command=["bad-command"],
            )
        
        assert exc_info.value.code == 127
