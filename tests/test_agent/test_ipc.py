"""Tests for IPC server."""

import pytest
import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path

from chimera.agent.ipc import IPCServer
from chimera.providers.base import ProviderStatus


@pytest.fixture
def mock_state_engine():
    """Create mock state engine."""
    engine = Mock()
    engine.provider_registry = Mock()
    
    # Setup mock providers
    image_provider = AsyncMock()
    image_provider.validate_spec.return_value = True
    
    container_provider = AsyncMock()
    container_provider.validate_spec.return_value = True
    
    profile_provider = AsyncMock()
    profile_provider.validate_spec.return_value = True
    
    def get_provider(name):
        if name == "image":
            return image_provider
        elif name == "container":
            return container_provider
        elif name == "profile":
            return profile_provider
        return None
        
    engine.provider_registry.get_provider.side_effect = get_provider
    return engine


@pytest.fixture
def mock_config_manager():
    """Create mock config manager."""
    manager = AsyncMock()
    manager.images = {}
    manager.profiles = {}
    manager.containers = {}
    return manager


@pytest.mark.asyncio
class TestIPCServer:
    """Test IPC Server logic."""

    async def test_validate_success(self, mock_state_engine, mock_config_manager):
        """Test validation success path."""
        server = IPCServer(Path("/tmp/sock"), mock_state_engine, mock_config_manager)
        
        # Setup valid config
        mock_config_manager.images = {"img1": Mock()}
        mock_config_manager.containers = {"cont1": Mock()}
        
        # Enriched specs need to be handled by engine/provider interaction in real code,
        # but here we just check if validate_spec is called.
        
        response = await server._handle_validate({})
        
        assert response["valid"] is True
        assert response["images"] == 1
        assert response["containers"] == 1
        
        # Verify providers were called
        img_provider = mock_state_engine.provider_registry.get_provider("image")
        img_provider.validate_spec.assert_called_once()
        
        cont_provider = mock_state_engine.provider_registry.get_provider("container")
        cont_provider.validate_spec.assert_called_once()

    async def test_validate_failure(self, mock_state_engine, mock_config_manager):
        """Test validation failure path."""
        server = IPCServer(Path("/tmp/sock"), mock_state_engine, mock_config_manager)
        
        # Setup config
        mock_config_manager.containers = {"bad-cont": Mock()}
        
        # Mock validation failure for container
        cont_provider = mock_state_engine.provider_registry.get_provider("container")
        cont_provider.validate_spec.return_value = False
        
        # Mock enrichment to avoid AttributeError if code tries to access properties
        # In the real implementation, _enrich_container_spec handles this
        
        # We need to ensure _enrich_container_spec is available or mocked if called
        # The IPC implementation will call config_manager.load(), then validate directly via providers
        # Note: Enrichment happens in StateEngine usually. 
        # For IPC validate, we need to ensure we don't crash on unenriched specs if providers expect them.
        # ContainerProvider.validate_spec checks _image_spec.
        # Let's see how we implement IPC validate. If we manually enrich or rely on pre-enrichment.
        
        # Mock the engine to handle enrichment if called, or assume implementation calls it
        mock_state_engine._enrich_container_spec = Mock()

        response = await server._handle_validate({})
        
        assert response["valid"] is False
        assert "Invalid container bad-cont" in response["error"]
