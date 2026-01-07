"""
Chimera Spawn - Modern systemd-nspawn container orchestration.

A sophisticated container management platform using systemd-nspawn for superior
isolation and systemd integration, providing LXD-like usability.
"""

__version__ = "1.0.0"
__author__ = "Chimera Development Team"

# Re-export key components for easier access
from chimera.models.config import ChimeraConfig
from chimera.models.container import ContainerSpec
from chimera.models.image import ImageSpec
from chimera.models.profile import ProfileSpec

__all__ = [
    "ChimeraConfig",
    "ContainerSpec", 
    "ImageSpec",
    "ProfileSpec",
]
