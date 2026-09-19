"""
Provider registry for managing resource providers.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import logging
from typing import Any

from chimera.providers.base import BaseProvider
from chimera.providers.cloudinit import CloudInitProvider
from chimera.providers.container import ContainerProvider
from chimera.providers.image import ImageProvider
from chimera.providers.profile import ProfileProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry for managing providers."""

    def __init__(self) -> None:
        """Initialize provider registry."""
        self._providers: dict[str, BaseProvider[Any]] = {}
        self._provider_classes: dict[str, type[BaseProvider[Any]]] = {
            "image": ImageProvider,
            "container": ContainerProvider,
            "cloudinit": CloudInitProvider,
            "profile": ProfileProvider,
        }

    async def initialize(self, config: Any) -> None:
        """Initialize all providers with two-pass injection."""
        # Phase 1: Instantiate all providers
        for name, provider_class in self._provider_classes.items():
            try:
                self._providers[name] = provider_class()
            except Exception as e:
                logger.error(f"Failed to instantiate provider {name}: {e}")
                raise

        # Phase 2: Initialize and inject registry
        for name, provider in self._providers.items():
            try:
                await provider.initialize(config, self)
                logger.debug(f"Initialized provider: {name}")
            except Exception as e:
                logger.error(f"Failed to initialize provider {name}: {e}")
                raise

    def get_provider(self, name: str) -> BaseProvider[Any] | None:
        """Get a provider by name."""
        return self._providers.get(name)

    def list_providers(self) -> list[str]:
        """List available provider names."""
        return list(self._providers.keys())
