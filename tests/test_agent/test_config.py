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

    async def test_cloud_init_deep_merge(self, config_dir):
        """Test that cloud-init templates are deep merged with overrides."""
        # Create a base template with nested data
        (config_dir / "cloud-init" / "base.yaml").write_text("""
base_template:
  meta_data:
    base_key: base_value
    nested:
      a: 1
  user_data: |
    #cloud-config
    users: []
""")

        # Create a node config that overrides the template
        (config_dir / "nodes" / "test.yaml").write_text("""
containers:
  merged_container:
    image: ubuntu
    cloud_init:
      template: base_template
      meta_data:
        override_key: override_value
        nested:
          b: 2
""")

        manager = ConfigManager(config_dir)
        await manager.load()
        
        container = manager.get_container_spec("merged_container")
        assert container is not None
        
        # Verify deep merge behavior
        meta = container.cloud_init.meta_data
        
        # Should have both base and override keys
        assert meta["base_key"] == "base_value"
        assert meta["override_key"] == "override_value"
        
        # Should have merged nested dictionary
        assert meta["nested"]["a"] == 1
        assert meta["nested"]["b"] == 2
        
        # User data should be preserved from base if not overridden
        assert "users: []" in container.cloud_init.user_data
