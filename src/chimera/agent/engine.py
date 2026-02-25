"""State reconciliation engine."""

import asyncio
import logging
from typing import Dict, Any, Optional, Set
from datetime import datetime

from chimera.agent.config import ConfigManager
from chimera.providers import ProviderRegistry, ProviderStatus
from chimera.models.container import ContainerSpec


logger = logging.getLogger(__name__)


class StateEngine:
    """Manages state reconciliation and drift detection."""
    
    def __init__(self, config_manager: ConfigManager, provider_registry: ProviderRegistry):
        """Initialize state engine."""
        self.config_manager = config_manager
        self.provider_registry = provider_registry
        self.last_reconciliation: Optional[datetime] = None
        self._reconciliation_lock = asyncio.Lock()
        self._container_states: Dict[str, ProviderStatus] = {}
        
    async def reconcile(self):
        """Perform full state reconciliation."""
        async with self._reconciliation_lock:
            start_time = datetime.now()
            logger.info("Starting state reconciliation")
            
            try:
                # Reconcile images first
                await self._reconcile_images()
                
                # Then reconcile containers
                await self._reconcile_containers()
                
                self.last_reconciliation = datetime.now()
                duration = (self.last_reconciliation - start_time).total_seconds()
                logger.info(f"State reconciliation completed in {duration:.2f}s")
                
            except Exception as e:
                logger.error(f"Reconciliation failed: {e}", exc_info=True)
                raise
                
    async def _reconcile_images(self):
        """Reconcile image states."""
        image_provider = self.provider_registry.get_provider("image")
        if not image_provider:
            logger.error("Image provider not found")
            return
            
        for name, spec in self.config_manager.images.items():
            try:
                # Validate spec first
                if not await image_provider.validate_spec(spec):
                    logger.warning(f"Skipping invalid image spec: {name}")
                    continue

                status = await image_provider.status(spec)
                
                if status == ProviderStatus.ABSENT:
                    logger.info(f"Image {name} is absent, pulling")
                    await image_provider.present(spec)
                elif status == ProviderStatus.PRESENT:
                    logger.debug(f"Image {name} is already present")
                else:
                    logger.warning(f"Image {name} in unknown state: {status}")
                    
            except Exception as e:
                logger.error(f"Failed to reconcile image {name}: {e}")
                
    async def _reconcile_containers(self):
        """Reconcile container states."""
        container_provider = self.provider_registry.get_provider("container")
        if not container_provider:
            logger.error("Container provider not found")
            return
            
        # Track configured containers
        configured_containers = set(self.config_manager.containers.keys())
        
        # Process each configured container
        for name, spec in self.config_manager.containers.items():
            try:
                # Enrich spec with additional data
                self._enrich_container_spec(spec)
                
                # Validate enriched spec
                if not await container_provider.validate_spec(spec):
                    logger.warning(f"Skipping invalid container spec: {name}")
                    continue
                
                status = await container_provider.status(spec)
                self._container_states[name] = status
                
                # Handle ensure state
                if spec.ensure == "present":
                    if status == ProviderStatus.ABSENT:
                        logger.info(f"Container {name} is absent, creating")
                        await container_provider.present(spec)
                    elif status == ProviderStatus.PRESENT:
                        # Check if running state matches desired
                        await self._ensure_container_state(name, spec)
                    else:
                        logger.warning(f"Container {name} in unknown state: {status}")
                        
                elif spec.ensure == "absent":
                    if status == ProviderStatus.PRESENT:
                        logger.info(f"Container {name} should be absent, removing")
                        await container_provider.absent(spec)
                        
            except Exception as e:
                logger.error(f"Failed to reconcile container {name}: {e}")
                
    def _enrich_container_spec(self, spec: ContainerSpec):
        """Enrich container spec with resolved references."""
        # Add image spec
        if spec.image:
            image_spec = self.config_manager.get_image_spec(spec.image)
            if image_spec:
                spec._image_spec = image_spec
            else:
                logger.warning(f"Image {spec.image} not found for container {spec.name}")
                
        # Add profile spec
        if spec.profile:
            profile_spec = self.config_manager.get_profile_spec(spec.profile)
            if profile_spec:
                spec._profile_spec = profile_spec
            else:
                logger.warning(f"Profile {spec.profile} not found for container {spec.name}")
                
    async def _ensure_container_state(self, name: str, spec: ContainerSpec):
        """Ensure container is in desired running state."""
        container_provider = self.provider_registry.get_provider("container")
        
        # Get current running state
        is_running = await container_provider.is_running(spec)
        
        if spec.state == "running" and not is_running:
            logger.info(f"Container {name} should be running, starting")
            await container_provider.start(spec)
        elif spec.state == "stopped" and is_running:
            logger.info(f"Container {name} should be stopped, stopping")
            await container_provider.stop(spec)
            
    async def get_container_status(self, name: str) -> Optional[Dict[str, Any]]:
        """Get detailed status for a specific container."""
        spec = self.config_manager.get_container_spec(name)
        if not spec:
            return None
            
        container_provider = self.provider_registry.get_provider("container")
        if not container_provider:
            return None
            
        self._enrich_container_spec(spec)
        
        status = await container_provider.status(spec)
        is_running = False
        if status == ProviderStatus.PRESENT:
            is_running = await container_provider.is_running(spec)
            
        return {
            "name": name,
            "exists": status == ProviderStatus.PRESENT,
            "running": is_running,
            "desired_state": spec.state,
            "ensure": spec.ensure,
            "image": spec.image,
            "profile": spec.profile,
        }
        
    async def get_all_container_statuses(self) -> Dict[str, Dict[str, Any]]:
        """Get status for all containers."""
        statuses = {}
        for name in self.config_manager.containers:
            status = await self.get_container_status(name)
            if status:
                statuses[name] = status
        return statuses
        
    async def execute_in_container(self, name: str, command: list[str]) -> Dict[str, Any]:
        """Execute command in container."""
        spec = self.config_manager.get_container_spec(name)
        if not spec:
            raise ValueError(f"Container {name} not found")
            
        container_provider = self.provider_registry.get_provider("container")
        if not container_provider:
            raise RuntimeError("Container provider not available")
            
        self._enrich_container_spec(spec)
        
        # Check container exists and is running
        status = await container_provider.status(spec)
        if status != ProviderStatus.PRESENT:
            raise RuntimeError(f"Container {name} does not exist")
            
        is_running = await container_provider.is_running(spec)
        if not is_running:
            raise RuntimeError(f"Container {name} is not running")
            
        return await container_provider.execute(spec, command)
        
    async def create_container(self, name: str):
        """Create a specific container."""
        spec = self.config_manager.get_container_spec(name)
        if not spec:
            raise ValueError(f"Container {name} not found in configuration")
            
        container_provider = self.provider_registry.get_provider("container")
        if not container_provider:
            raise RuntimeError("Container provider not available")
            
        self._enrich_container_spec(spec)
        
        # Validate spec before creation
        if not await container_provider.validate_spec(spec):
            raise ValueError(f"Invalid container configuration for {name}")

        # Ensure image exists first
        if spec._image_spec:
            image_provider = self.provider_registry.get_provider("image")
            if image_provider:
                image_status = await image_provider.status(spec._image_spec)
                if image_status != ProviderStatus.PRESENT:
                    logger.info(f"Pulling required image {spec.image}")
                    await image_provider.present(spec._image_spec)
                    
        await container_provider.present(spec)
        
    async def stop_container(self, name: str):
        """Stop a specific container."""
        spec = self.config_manager.get_container_spec(name)
        if not spec:
            raise ValueError(f"Container {name} not found")
            
        container_provider = self.provider_registry.get_provider("container")
        if not container_provider:
            raise RuntimeError("Container provider not available")
            
        self._enrich_container_spec(spec)
        await container_provider.stop(spec)
        
    async def start_container(self, name: str):
        """Start a specific container."""
        spec = self.config_manager.get_container_spec(name)
        if not spec:
            raise ValueError(f"Container {name} not found")
            
        container_provider = self.provider_registry.get_provider("container")
        if not container_provider:
            raise RuntimeError("Container provider not available")
            
        self._enrich_container_spec(spec)
        
        # Validate before starting
        if not await container_provider.validate_spec(spec):
            raise ValueError(f"Invalid container configuration for {name}")

        await container_provider.start(spec)
        
    async def remove_container(self, name: str):
        """Remove a specific container."""
        spec = self.config_manager.get_container_spec(name)
        if not spec:
            raise ValueError(f"Container {name} not found")
            
        container_provider = self.provider_registry.get_provider("container")
        if not container_provider:
            raise RuntimeError("Container provider not available")
            
        self._enrich_container_spec(spec)
        await container_provider.absent(spec)
