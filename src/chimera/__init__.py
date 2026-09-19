"""
Chimera Spawn - Modern systemd-nspawn container orchestration.

A sophisticated container management platform using systemd-nspawn for superior
isolation and systemd integration, providing LXD-like usability.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from importlib.metadata import PackageNotFoundError, version

from chimera.models.config import ChimeraConfig
from chimera.models.container import ContainerRecord, ContainerSpec
from chimera.models.image import ImageSpec
from chimera.models.profile import ProfileSpec


def package_version() -> str:
    """Return the installed distribution version when metadata is available."""
    try:
        return version("chimera-spawn")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = package_version()
__author__ = "Thiago Camargo"

__all__ = [
    "ChimeraConfig",
    "ContainerRecord",
    "ContainerSpec",
    "ImageSpec",
    "ProfileSpec",
    "package_version",
]
