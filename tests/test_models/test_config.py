"""Tests for configuration models."""

import pytest
from pydantic import ValidationError

from chimera.models.config import ChimeraConfig, AgentConfig, ProxyConfig, SystemdConfig


class TestAgentConfig:
    """Test AgentConfig model."""
    
    def test_default_values(self):
        """Test default agent configuration values."""
        config = AgentConfig()
        
        assert config.socket_path == "./state/chimera-agent.sock"
        assert config.reconciliation_interval == 30
        assert config.log_level == "INFO"
        assert config.config_dir == "./configs"
        assert config.state_dir == "./state"
        
    def test_custom_values(self):
        """Test custom agent configuration values."""
        config = AgentConfig(
            socket_path="/tmp/agent.sock",
            reconciliation_interval=60,
            log_level="DEBUG"
        )
        
        assert config.socket_path == "/tmp/agent.sock"
        assert config.reconciliation_interval == 60
        assert config.log_level == "DEBUG"
        
    def test_log_level_validation(self):
        """Test log level validation."""
        # Valid log levels
        for level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            config = AgentConfig(log_level=level)
            assert config.log_level == level.upper()
            
        # Case insensitive
        config = AgentConfig(log_level="debug")
        assert config.log_level == "DEBUG"
        
        # Invalid log level
        with pytest.raises(ValidationError) as exc_info:
            AgentConfig(log_level="INVALID")
            
        assert "log_level" in str(exc_info.value)
        
    def test_interval_validation(self):
        """Test reconciliation interval validation."""
        # Minimum value
        with pytest.raises(ValidationError) as exc_info:
            AgentConfig(reconciliation_interval=4)  # Less than 5
            
        assert "reconciliation_interval" in str(exc_info.value)


class TestProxyConfig:
    """Test ProxyConfig model."""
    
    def test_no_proxy(self):
        """Test proxy configuration without proxy."""
        config = ProxyConfig()
        
        assert config.http_proxy is None
        assert config.https_proxy is None
        assert config.no_proxy == "localhost,127.0.0.1"
        
    def test_with_proxy(self):
        """Test proxy configuration with proxy."""
        config = ProxyConfig(
            http_proxy="http://proxy.example.com:3128",
            https_proxy="http://proxy.example.com:3128",
            no_proxy="localhost,127.0.0.1,.example.com"
        )
        
        assert config.http_proxy == "http://proxy.example.com:3128"
        assert config.https_proxy == "http://proxy.example.com:3128"
        assert config.no_proxy == "localhost,127.0.0.1,.example.com"


class TestChimeraConfig:
    """Test ChimeraConfig model."""
    
    def test_default_config(self):
        """Test default configuration."""
        config = ChimeraConfig()
        
        assert isinstance(config.agent, AgentConfig)
        assert config.proxy is None
        assert isinstance(config.systemd, SystemdConfig)
        
        # Check defaults
        assert config.agent.socket_path == "./state/chimera-agent.sock"
        assert config.systemd.machines_dir == "/var/lib/machines"
        
    def test_full_config(self):
        """Test full configuration with all sections."""
        config = ChimeraConfig(
            agent=AgentConfig(log_level="DEBUG"),
            proxy=ProxyConfig(http_proxy="http://proxy:3128"),
            systemd=SystemdConfig(machines_dir="/custom/machines")
        )
        
        assert config.agent.log_level == "DEBUG"
        assert config.proxy.http_proxy == "http://proxy:3128"
        assert config.systemd.machines_dir == "/custom/machines"
        
    def test_extra_fields_ignored(self):
        """Test that extra fields are ignored."""
        # Should not raise error
        config = ChimeraConfig(
            agent=AgentConfig(),
            extra_field="ignored"
        )
        
        assert hasattr(config, "agent")
        assert not hasattr(config, "extra_field")
