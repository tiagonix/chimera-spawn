"""Container specification models."""

from typing import Literal, Optional, Dict, Any
from pydantic import BaseModel, Field


class CloudInitSpec(BaseModel):
    """Cloud-init specification."""
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    user_data: Optional[str] = None
    network_config: Optional[str] = None
    template: Optional[str] = Field(None, description="Template name to use")
    
    class Config:
        """Pydantic config."""
        extra = "allow"  # Allow extra fields for template merging


class ContainerSpec(BaseModel):
    """Container specification."""
    name: str = Field(..., description="Container name")
    ensure: Literal["present", "absent"] = Field(default="present")
    state: Literal["running", "stopped"] = Field(default="running")
    image: str = Field(..., description="Image name to use")
    profile: str = Field(default="isolated", description="Profile name")
    cloud_init: Optional[CloudInitSpec] = None
    autostart: bool = Field(default=True)
    
    # Internal fields for enriched data
    _image_spec: Optional[Any] = None
    _profile_spec: Optional[Any] = None
    
    class Config:
        """Pydantic config."""
        extra = "ignore"
        underscore_attrs_are_private = True
