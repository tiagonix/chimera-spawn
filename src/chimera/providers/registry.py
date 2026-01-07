"""Provider registry for managing resource providers."""

import logging
from typing import Dict, Optional, Type

from chimera.providers.base import BaseProvider
from chimera.providers.image import ImageProvider
from chimera.providers.container import ContainerProvider
from chimera.providers.cloudinit import CloudInitProvider
from chimera.providers.profile import ProfileProvider


logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry for managing providers."""
    
    def __init__(self):
        """Initialize provider registry."""
        self._providers: Dict[str, BaseProvider] = {}
        self._provider_classes: Dict[str, Type[BaseProvider]] = {
            "image": ImageProvider,
            "container": ContainerProvider,
            "cloudinit": CloudInitProvider,
            "profile": ProfileProvider,
        }
        
    async def initialize(self, config):
        """Initialize all providers."""
        for name, provider_class in self._provider_classes.items():
            try:
                provider = provider_class()
                await provider.initialize(config)
                self._providers[name] = provider
                logger.debug(f"Initialized provider: {name}")
            except Exception as e:
                logger.error(f"Failed to initialize provider {name}: {e}")
                
    def get_provider(self, name: str) -> Optional[BaseProvider]:
        """Get a provider by name."""
        return self._providers.get(name)
        
    def list_providers(self) -> list[str]:
        """List available provider names."""
        return list(self._providers.keys())


# Global provider registry
_registry: Optional[ProviderRegistry] = None


def get_provider_registry() -> ProviderRegistry:
    """Get or create the global provider registry."""
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry
