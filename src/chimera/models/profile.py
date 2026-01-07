"""Profile specification models."""

from pydantic import BaseModel, Field


class ProfileSpec(BaseModel):
    """nspawn profile specification."""
    name: str = Field(..., description="Profile name")
    nspawn_config_content: str = Field(..., description="Content for .nspawn file")
    systemd_override_content: str = Field(..., description="Systemd service override content")
    
    class Config:
        """Pydantic config."""
        extra = "ignore"
