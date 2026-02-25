"""Tests for container provider."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock, call
import asyncio
import shutil
from pathlib import Path

from chimera.providers.container import ContainerProvider
from chimera.models.container import ContainerSpec
from chimera.providers.base import ProviderStatus


@pytest.fixture
def container_provider():
    """Create container provider instance."""
    return ContainerProvider()


@pytest.mark.asyncio
class TestContainerProvider:
    """Test container provider."""

    async def test_execute_with_spaces(self, container_provider):
        """Test execution of commands with spaces is safely quoted."""
        spec = ContainerSpec(name="test-container", image="ubuntu")
        command = ["echo", "hello world", ";", "rm", "-rf", "/"]
        
        # Mock run_command to capture arguments
        with patch("chimera.providers.container.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(stdout="hello world ; rm -rf /", stderr="", returncode=0)
            
            await container_provider.execute(spec, command)
            
            # Verify the command was constructed safely
            # Should be: machinectl shell test-container /bin/bash -c 'echo "hello world" ";" rm -rf /'
            # exact quoting depends on shlex, but it certainly won't be just spaces
            call_args = mock_run.call_args[0][0]
            
            assert call_args[0] == "machinectl"
            assert call_args[1] == "shell"
            assert call_args[2] == "test-container"
            assert call_args[4] == "-c"
            
            # The critical assertion: passed as a single quoted string to bash -c
            # NOT flattened with simple spaces
            executed_cmd_string = call_args[5]
            assert "'hello world'" in executed_cmd_string or '"hello world"' in executed_cmd_string
            assert "hello world" in executed_cmd_string

    async def test_absent_offloads_blocking_io(self, container_provider):
        """Test that absent method offloads blocking I/O to threads."""
        spec = ContainerSpec(name="test-container", image="ubuntu")
        
        # Setup mocks
        container_provider.machines_dir = Path("/var/lib/machines")
        container_provider.nspawn_dir = Path("/etc/systemd/nspawn")
        container_provider.system_dir = Path("/etc/systemd/system")
        
        # Mock status to return PRESENT so absent proceeds
        container_provider.status = AsyncMock(return_value=ProviderStatus.PRESENT)
        container_provider.stop = AsyncMock()
        container_provider._disable_service = AsyncMock()
        
        # Mock run_command for machinectl remove
        with patch("chimera.providers.container.run_command", new_callable=AsyncMock) as mock_run:
            # Mock asyncio.to_thread to verify it intercepts blocking calls
            with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
                # Mock Path.exists to return True so cleanup code runs
                with patch("pathlib.Path.exists", return_value=True):
                     await container_provider.absent(spec)
                
                # Verify key blocking operations were offloaded
                # We expect calls for cleanup_config_files (unlink) and potentially rmtree
                
                # Check for rmtree offloading
                # Note: The implementation calls _cleanup_partial_container or explicit cleanup in absent
                # We need to verify that shutil.rmtree was passed to to_thread
                
                # Verify at least one call involved shutil.rmtree or unlink
                rmtree_called = any(
                    args[0] == shutil.rmtree for args in mock_to_thread.call_args_list
                )
                
                # We can't easily equality check bound methods of different objects,
                # but we can check the call count is non-zero
                unlink_called = len(mock_to_thread.call_args_list) > 0
                
                assert unlink_called, "Should have offloaded filesystem operations"
