"""Configuration management for the agent."""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import hashlib

from ruamel.yaml import YAML
from pydantic import ValidationError
from watchfiles import awatch

from chimera.models.config import ChimeraConfig
from chimera.models.container import ContainerSpec
from chimera.models.image import ImageSpec
from chimera.models.profile import ProfileSpec


logger = logging.getLogger(__name__)


class ConfigManager:
    """Manages configuration loading and monitoring."""
    
    def __init__(self, config_dir: Path):
        """Initialize configuration manager."""
        self.config_dir = Path(config_dir)
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.config: Optional[ChimeraConfig] = None
        self.images: Dict[str, ImageSpec] = {}
        self.profiles: Dict[str, ProfileSpec] = {}
        self.cloud_init_templates: Dict[str, Dict[str, Any]] = {}
        self.containers: Dict[str, ContainerSpec] = {}
        self._config_hashes: Dict[str, str] = {}
        
    async def load(self):
        """Load all configuration files."""
        logger.info(f"Loading configuration from {self.config_dir}")
        
        # Load main configuration
        await self._load_main_config()
        
        # Load resource configurations
        await self._load_images()
        await self._load_profiles()
        await self._load_cloud_init()
        await self._load_containers()
        
        logger.info("Configuration loaded successfully")
        
    async def _load_main_config(self):
        """Load main configuration file."""
        config_file = self.config_dir / "config.yaml"
        if not config_file.exists():
            raise FileNotFoundError(f"Main config not found: {config_file}")
            
        try:
            data = await self._read_yaml(config_file)
            self.config = ChimeraConfig(**data)
            logger.debug(f"Loaded main config: {config_file}")
        except ValidationError as e:
            logger.error(f"Invalid main config: {e}")
            raise
            
    async def _load_images(self):
        """Load image configurations."""
        images_dir = self.config_dir / "images"
        if not images_dir.exists():
            logger.warning(f"Images directory not found: {images_dir}")
            return
            
        self.images.clear()
        for yaml_file in images_dir.glob("*.yaml"):
            try:
                data = await self._read_yaml(yaml_file)
                for name, spec in data.items():
                    self.images[name] = ImageSpec(name=name, **spec)
                logger.debug(f"Loaded images from {yaml_file}")
            except Exception as e:
                logger.error(f"Error loading {yaml_file}: {e}")
                
    async def _load_profiles(self):
        """Load profile configurations."""
        profiles_dir = self.config_dir / "profiles"
        if not profiles_dir.exists():
            logger.warning(f"Profiles directory not found: {profiles_dir}")
            return
            
        self.profiles.clear()
        for yaml_file in profiles_dir.glob("*.yaml"):
            try:
                data = await self._read_yaml(yaml_file)
                for name, spec in data.items():
                    self.profiles[name] = ProfileSpec(name=name, **spec)
                logger.debug(f"Loaded profiles from {yaml_file}")
            except Exception as e:
                logger.error(f"Error loading {yaml_file}: {e}")
                
    async def _load_cloud_init(self):
        """Load cloud-init templates."""
        cloud_init_dir = self.config_dir / "cloud-init"
        if not cloud_init_dir.exists():
            logger.warning(f"Cloud-init directory not found: {cloud_init_dir}")
            return
            
        self.cloud_init_templates.clear()
        for yaml_file in cloud_init_dir.glob("*.yaml"):
            try:
                data = await self._read_yaml(yaml_file)
                self.cloud_init_templates.update(data)
                logger.debug(f"Loaded cloud-init templates from {yaml_file}")
            except Exception as e:
                logger.error(f"Error loading {yaml_file}: {e}")
                
    async def _load_containers(self):
        """Load container configurations from nodes."""
        nodes_dir = self.config_dir / "nodes"
        if not nodes_dir.exists():
            logger.warning(f"Nodes directory not found: {nodes_dir}")
            return
            
        self.containers.clear()
        for yaml_file in nodes_dir.glob("*.yaml"):
            try:
                data = await self._read_yaml(yaml_file)
                if "containers" in data:
                    for name, spec in data["containers"].items():
                        # Resolve cloud-init template if specified
                        if spec.get("cloud_init") and "template" in spec["cloud_init"]:
                            template_name = spec["cloud_init"]["template"]
                            if template_name in self.cloud_init_templates:
                                template = self.cloud_init_templates[template_name]
                                # Merge template with overrides
                                cloud_init = template.copy()
                                cloud_init.update(spec["cloud_init"])
                                spec["cloud_init"] = cloud_init
                                
                        self.containers[name] = ContainerSpec(name=name, **spec)
                logger.debug(f"Loaded containers from {yaml_file}")
            except Exception as e:
                logger.error(f"Error loading {yaml_file}: {e}")
                
    async def _read_yaml(self, file_path: Path) -> Dict[str, Any]:
        """Read and parse YAML file."""
        content = file_path.read_text()
        # Store hash for change detection
        self._config_hashes[str(file_path)] = hashlib.md5(content.encode()).hexdigest()
        return self.yaml.load(content)
        
    async def watch_for_changes(self) -> bool:
        """Check if configuration files have changed."""
        changed = False
        
        # Check all YAML files
        for yaml_file in self.config_dir.rglob("*.yaml"):
            content = yaml_file.read_text()
            current_hash = hashlib.md5(content.encode()).hexdigest()
            
            if str(yaml_file) not in self._config_hashes:
                changed = True
            elif self._config_hashes[str(yaml_file)] != current_hash:
                changed = True
                
        return changed
        
    def get_container_spec(self, name: str) -> Optional[ContainerSpec]:
        """Get container specification by name."""
        return self.containers.get(name)
        
    def get_image_spec(self, name: str) -> Optional[ImageSpec]:
        """Get image specification by name."""
        return self.images.get(name)
        
    def get_profile_spec(self, name: str) -> Optional[ProfileSpec]:
        """Get profile specification by name."""
        return self.profiles.get(name)
