"""Cloud-init provider for container initialization."""

import logging
from pathlib import Path
from typing import Optional, Dict, Any
import json
import io

from ruamel.yaml import YAML

from chimera.providers.base import BaseProvider, ProviderStatus
from chimera.models.container import ContainerSpec, CloudInitSpec
from chimera.utils.templates import render_template


logger = logging.getLogger(__name__)


class CloudInitProvider(BaseProvider):
    """Provider for managing cloud-init configurations."""
    
    def __init__(self):
        """Initialize cloud-init provider."""
        self.machines_dir: Optional[Path] = None
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.proxy_config = None
        
    async def initialize(self, config):
        """Initialize provider with configuration."""
        self.machines_dir = Path(config.systemd.machines_dir)
        self.proxy_config = config.proxy
        
    async def status(self, spec: ContainerSpec) -> ProviderStatus:
        """Check cloud-init status."""
        if not spec.cloud_init:
            return ProviderStatus.ABSENT
            
        container_path = self.machines_dir / spec.name
        seed_dir = container_path / "var/lib/cloud/seed/nocloud"
        
        if seed_dir.exists():
            return ProviderStatus.PRESENT
        else:
            return ProviderStatus.ABSENT
            
    async def present(self, spec: ContainerSpec) -> None:
        """Ensure cloud-init is configured."""
        # This is called from container provider
        await self.prepare(spec)
        
    async def absent(self, spec: ContainerSpec) -> None:
        """Remove cloud-init configuration."""
        container_path = self.machines_dir / spec.name
        cloud_dir = container_path / "var/lib/cloud"
        
        if cloud_dir.exists():
            import shutil
            shutil.rmtree(cloud_dir)
            logger.debug(f"Removed cloud-init directory for {spec.name}")
            
    async def validate_spec(self, spec: ContainerSpec) -> bool:
        """Validate cloud-init specification."""
        if not spec.cloud_init:
            return True
            
        # Basic validation is handled by Pydantic
        return True
        
    async def prepare(self, spec: ContainerSpec):
        """Prepare cloud-init configuration for container."""
        if not spec.cloud_init:
            logger.debug(f"No cloud-init config for container {spec.name}")
            return
            
        container_path = self.machines_dir / spec.name
        cloud_init = spec.cloud_init
        
        # Create directory structure
        seed_dir = container_path / "var/lib/cloud/seed/nocloud"
        seed_dir.mkdir(parents=True, exist_ok=True)
        
        # Process meta-data
        meta_data = await self._prepare_meta_data(spec.name, cloud_init)
        if meta_data:
            meta_file = seed_dir / "meta-data"
            # Use StringIO to dump YAML
            stream = io.StringIO()
            self.yaml.dump(meta_data, stream)
            meta_file.write_text(stream.getvalue())
            logger.debug(f"Created meta-data for {spec.name}")
            
        # Process user-data
        user_data = await self._prepare_user_data(cloud_init)
        if user_data:
            user_file = seed_dir / "user-data"
            user_file.write_text(user_data)
            logger.debug(f"Created user-data for {spec.name}")
            
        # Process network-config
        network_config = cloud_init.network_config
        if network_config:
            network_file = seed_dir / "network-config"
            network_file.write_text(network_config)
            logger.debug(f"Created network-config for {spec.name}")
        else:
            # Disable network configuration
            disable_file = container_path / "etc/cloud/cloud.cfg.d/99-disable-network-config.cfg"
            disable_file.parent.mkdir(parents=True, exist_ok=True)
            disable_file.write_text("network: {config: disabled}\n")
            logger.debug(f"Disabled network config for {spec.name}")
            
    async def _prepare_meta_data(self, container_name: str, cloud_init: CloudInitSpec) -> Dict[str, Any]:
        """Prepare meta-data content."""
        meta_data = cloud_init.meta_data.copy() if cloud_init.meta_data else {}
        
        # Always set local-hostname
        meta_data["local-hostname"] = container_name
        
        # Add instance-id if not present
        if "instance-id" not in meta_data:
            meta_data["instance-id"] = f"iid-{container_name}"
            
        return meta_data
        
    async def _prepare_user_data(self, cloud_init: CloudInitSpec) -> Optional[str]:
        """Prepare user-data content."""
        if not cloud_init.user_data:
            return None
            
        # Render template with proxy settings if available
        context = {
            "proxy": self.proxy_config,
        }
        
        return render_template(cloud_init.user_data, **context)
