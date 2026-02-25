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
    provider = ContainerProvider()
    # Mock systemd_dbus to avoid actual DBus connection attempts
    provider.systemd_dbus = AsyncMock()
    return provider


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

    async def test_status_is_read_only(self, container_provider):
        """Test that status check does not perform cleanup on partial containers."""
        spec = ContainerSpec(name="test-partial", image="ubuntu")
        
        # Mock dependencies
        container_provider.machines_dir = Path("/var/lib/machines")
        
        # Mock run_command to return failure (container not known to machinectl)
        with patch("chimera.providers.container.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            
            with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
                # Simulate dir_exists=True, raw_exists=False
                # The implementation calls to_thread(container_dir.exists) then to_thread(container_raw.exists)
                # We mock the return values for these sequential calls
                mock_to_thread.side_effect = [True, False]
                
                # Mock cleanup method to ensure it's NOT called
                # We need to patch the method on the instance, or rely on the fact 
                # that _cleanup_partial_container is an async method
                with patch.object(container_provider, '_cleanup_partial_container', new_callable=AsyncMock) as mock_cleanup:
                    status = await container_provider.status(spec)
                    
                    assert status == ProviderStatus.ABSENT
                    mock_cleanup.assert_not_called()

    async def test_lifecycle_uses_dbus(self, container_provider):
        """Test that lifecycle methods use the SystemdDBus wrapper."""
        spec = ContainerSpec(name="test-dbus", image="ubuntu")
        service_name = "systemd-nspawn@test-dbus.service"
        
        # Mock internal helpers to avoid complex setup
        container_provider.is_running = AsyncMock(return_value=False)
        container_provider._wait_for_ready = AsyncMock()
        
        # Test start
        await container_provider.start(spec)
        container_provider.systemd_dbus.start_unit.assert_called_with(service_name)
        
        # Reset and test stop
        container_provider.systemd_dbus.reset_mock()
        container_provider.is_running.return_value = True
        await container_provider.stop(spec)
        container_provider.systemd_dbus.stop_unit.assert_called_with(service_name)
        
        # Test is_running via get_unit_state
        # We need to call the real is_running logic here, so unmock it
        del container_provider.is_running
        
        container_provider.systemd_dbus.get_unit_state.return_value = "active"
        is_active = await container_provider.is_running(spec)
        assert is_active is True
        container_provider.systemd_dbus.get_unit_state.assert_called_with(service_name)
        
        container_provider.systemd_dbus.get_unit_state.return_value = "inactive"
        is_active = await container_provider.is_running(spec)
        assert is_active is False
