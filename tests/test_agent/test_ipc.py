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
    engine.restart_container = AsyncMock()
    
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
        
        # Mock the engine to handle enrichment if called, or assume implementation calls it
        mock_state_engine._enrich_container_spec = Mock()

        response = await server._handle_validate({})
        
        assert response["valid"] is False
        assert "Invalid container bad-cont" in response["error"]

    async def test_privileged_command_denied_non_root(self, mock_state_engine, mock_config_manager):
        """Test that privileged commands are denied for non-root users."""
        server = IPCServer(Path("/tmp/sock"), mock_state_engine, mock_config_manager)
        
        # Privileged command
        request = {"command": "spawn", "args": {"name": "test"}}
        
        # Test with None UID (unknown user)
        response = await server._process_request(request, client_uid=None)
        assert response["success"] is False
        assert "Permission denied" in response["error"]
        
        # Test with non-root UID (e.g., 1000)
        response = await server._process_request(request, client_uid=1000)
        assert response["success"] is False
        assert "Permission denied" in response["error"]

    async def test_privileged_command_allowed_root(self, mock_state_engine, mock_config_manager):
        """Test that privileged commands are allowed for root user."""
        server = IPCServer(Path("/tmp/sock"), mock_state_engine, mock_config_manager)
        
        # Privileged command
        request = {"command": "spawn", "args": {"name": "test"}}
        
        # Mock the handler to succeed immediately
        server._handle_spawn = AsyncMock(return_value={"created": True})
        
        # Test with UID 0 (root)
        response = await server._process_request(request, client_uid=0)
        
        assert response["success"] is True
        assert response["data"]["created"] is True
        server._handle_spawn.assert_called_once()

    async def test_non_privileged_command_allowed_any(self, mock_state_engine, mock_config_manager):
        """Test that non-privileged commands are allowed for any user."""
        server = IPCServer(Path("/tmp/sock"), mock_state_engine, mock_config_manager)
        
        # Non-privileged command
        request = {"command": "status", "args": {}}
        
        # Mock the handler
        server._handle_status = AsyncMock(return_value={"status": "ok"})
        
        # Test with non-root UID
        response = await server._process_request(request, client_uid=1000)
        
        assert response["success"] is True
        assert response["data"]["status"] == "ok"

    async def test_handle_restart_success(self, mock_state_engine, mock_config_manager):
        """Test the restart IPC handler calls the state engine."""
        server = IPCServer(Path("/tmp/sock"), mock_state_engine, mock_config_manager)

        # Call the handler for restart
        result = await server._handle_restart({"name": "test-container"})

        # Verify the state engine's restart method was called
        mock_state_engine.restart_container.assert_awaited_once_with("test-container")
        assert result == {"container": "test-container", "restarted": True}
