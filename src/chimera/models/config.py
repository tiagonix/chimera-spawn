"""Configuration models."""

from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field, validator


class AgentConfig(BaseModel):
    """Agent configuration."""
    socket_path: str = Field(default="./state/chimera-agent.sock")
    reconciliation_interval: int = Field(default=30, ge=5)
    log_level: str = Field(default="INFO")
    config_dir: str = Field(default="./configs")
    state_dir: str = Field(default="./state")
    
    @validator("log_level")
    def validate_log_level(cls, v):
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Invalid log level: {v}")
        return v.upper()


class ProxyConfig(BaseModel):
    """Proxy configuration."""
    http_proxy: Optional[str] = None
    https_proxy: Optional[str] = None
    no_proxy: str = Field(default="localhost,127.0.0.1")


class SystemdConfig(BaseModel):
    """Systemd paths configuration."""
    machines_dir: str = Field(default="/var/lib/machines")
    nspawn_dir: str = Field(default="/etc/systemd/nspawn")
    system_dir: str = Field(default="/etc/systemd/system")


class ChimeraConfig(BaseModel):
    """Main configuration model."""
    agent: AgentConfig = Field(default_factory=AgentConfig)
    proxy: Optional[ProxyConfig] = None
    systemd: SystemdConfig = Field(default_factory=SystemdConfig)
    
    class Config:
        """Pydantic config."""
        extra = "ignore"
