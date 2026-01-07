"""Image specification models."""

from typing import Literal, Optional, List
from pydantic import BaseModel, Field, HttpUrl


class CustomFileSpec(BaseModel):
    """Custom file modification specification."""
    path: str = Field(..., description="File path relative to container root")
    ensure: Literal["present", "absent", "link"] = Field(...)
    target: Optional[str] = Field(None, description="Target for symbolic links")
    
    class Config:
        """Pydantic config."""
        extra = "forbid"


class ImageSpec(BaseModel):
    """Image specification."""
    name: str = Field(..., description="Image name")
    type: Literal["tar", "raw"] = Field(..., description="Image type")
    verify: Literal["signature", "checksum", "no"] = Field(default="signature")
    source: str = Field(..., description="Image source URL")
    custom_files: List[CustomFileSpec] = Field(default_factory=list)
    
    class Config:
        """Pydantic config."""
        extra = "ignore"
