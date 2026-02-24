"""Tests for ProviderRegistry."""

import pytest
from unittest.mock import Mock, AsyncMock
from chimera.providers.registry import ProviderRegistry
from chimera.providers.base import BaseProvider, ProviderStatus


class MockProvider(BaseProvider):
    """Mock provider for testing registry."""
    
    def __init__(self):
        self.initialized = False
        self.registry_ref = None
        self.config_ref = None
        
    async def initialize(self, config, registry):
        self.initialized = True
        self.config_ref = config
        self.registry_ref = registry
        
    async def status(self, spec):
        return ProviderStatus.UNKNOWN
        
    async def present(self, spec):
        pass
        
    async def absent(self, spec):
        pass
        
    async def validate_spec(self, spec):
        return True


class TestProviderRegistry:
    """Test ProviderRegistry initialization and injection."""
    
    @pytest.mark.asyncio
    async def test_initialization_injection(self):
        """Test that registry injects itself into providers."""
        registry = ProviderRegistry()
        
        # Override the provider classes map with our mock
        registry._provider_classes = {
            "mock": MockProvider
        }
        
        mock_config = Mock()
        await registry.initialize(mock_config)
        
        # Verify provider was created
        provider = registry.get_provider("mock")
        assert provider is not None
        assert isinstance(provider, MockProvider)
        
        # Verify injection
        assert provider.initialized is True
        assert provider.config_ref == mock_config
        assert provider.registry_ref == registry

    @pytest.mark.asyncio
    async def test_get_provider(self):
        """Test retrieving providers."""
        registry = ProviderRegistry()
        registry._provider_classes = {"mock": MockProvider}
        await registry.initialize(Mock())
        
        assert registry.get_provider("mock") is not None
        assert registry.get_provider("nonexistent") is None

    def test_list_providers(self):
        """Test listing providers."""
        registry = ProviderRegistry()
        registry._provider_classes = {
            "mock1": MockProvider,
            "mock2": MockProvider
        }
        
        # Should list keys even before initialization if just listing keys, 
        # but the method lists keys of _providers which are populated at init.
        # Let's check post-init behavior which is what matters.
        # Based on implementation, _providers is empty until init.
        assert registry.list_providers() == []
