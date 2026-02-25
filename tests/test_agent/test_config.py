"""Tests for agent configuration management."""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from chimera.agent.config import ConfigManager


@pytest.fixture
def config_dir(tmp_path):
    """Create a temporary config directory structure."""
    (tmp_path / "images").mkdir()
    (tmp_path / "profiles").mkdir()
    (tmp_path / "nodes").mkdir()
    (tmp_path / "cloud-init").mkdir()
    
    # Create main config
    (tmp_path / "config.yaml").write_text("""
agent:
  socket_path: ./agent.sock
systemd:
  machines_dir: /tmp/machines
""")
    return tmp_path


@pytest.mark.asyncio
class TestConfigManager:
    """Test ConfigManager async operations."""

    async def test_load_config_async(self, config_dir):
        """Test that configuration loading works asynchronously."""
        manager = ConfigManager(config_dir)
        
        # Verify load works without blocking
        await manager.load()
        
        assert manager.config is not None
        assert manager.config.agent.socket_path == "./agent.sock"

    async def test_read_yaml_threading(self, config_dir):
        """Test that YAML reading is offloaded to a thread."""
        manager = ConfigManager(config_dir)
        test_file = config_dir / "test.yaml"
        test_file.write_text("key: value")
        
        # Patch asyncio.to_thread to verify it's called
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            # We need to simulate the return value of the wrapped function
            mock_to_thread.return_value = {"key": "value"}
            
            result = await manager._read_yaml(test_file)
            
            assert result == {"key": "value"}
            mock_to_thread.assert_called_once()
