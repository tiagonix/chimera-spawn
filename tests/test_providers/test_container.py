"""Tests for container provider."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
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
