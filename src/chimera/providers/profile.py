"""
Profile provider for managing nspawn profiles.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import logging
from typing import TYPE_CHECKING, Any

from chimera.models.profile import ProfileSpec
from chimera.providers.base import BaseProvider, ProviderStatus

if TYPE_CHECKING:
    from chimera.providers.registry import ProviderRegistry


logger = logging.getLogger(__name__)


class ProfileProvider(BaseProvider[ProfileSpec]):
    """Provider for managing nspawn profiles."""

    def __init__(self) -> None:
        """Initialize profile provider."""
        self.profiles: dict[str, ProfileSpec] = {}

    async def initialize(self, config: Any, registry: "ProviderRegistry") -> None:
        """Initialize provider with configuration and registry."""
        # Profiles are loaded by ConfigManager
        return None

    async def status(self, spec: ProfileSpec) -> ProviderStatus:
        """Check profile status."""
        # Profiles are configuration-only, always present if loaded
        return ProviderStatus.PRESENT if spec.name in self.profiles else ProviderStatus.ABSENT

    async def present(self, spec: ProfileSpec) -> None:
        """Register profile."""
        self.profiles[spec.name] = spec
        logger.debug(f"Registered profile: {spec.name}")

    async def absent(self, spec: ProfileSpec) -> None:
        """Unregister profile."""
        if spec.name in self.profiles:
            del self.profiles[spec.name]
            logger.debug(f"Unregistered profile: {spec.name}")

    async def validate_spec(self, spec: ProfileSpec) -> bool:
        """Validate profile specification."""
        # Ensure profile has required content
        if not spec.nspawn_config_content:
            logger.error(f"Profile {spec.name} missing nspawn_config_content")
            return False

        if not spec.systemd_override_content:
            logger.error(f"Profile {spec.name} missing systemd_override_content")
            return False

        return True

    def get_profile(self, name: str) -> ProfileSpec | None:
        """Get profile by name."""
        return self.profiles.get(name)
