"""Image provider for managing container images."""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional

from chimera.providers.base import BaseProvider, ProviderStatus
from chimera.models.image import ImageSpec
from chimera.utils.systemd import run_command, SystemdDBus


logger = logging.getLogger(__name__)


class ImageProvider(BaseProvider):
    """Provider for managing systemd-nspawn images."""
    
    def __init__(self):
        """Initialize image provider."""
        self.machines_dir: Optional[Path] = None
        self.systemd_dbus: Optional[SystemdDBus] = None
        
    async def initialize(self, config):
        """Initialize provider with configuration."""
        self.machines_dir = Path(config.systemd.machines_dir)
        self.systemd_dbus = SystemdDBus()
        await self.systemd_dbus.connect()
        
    async def status(self, spec: ImageSpec) -> ProviderStatus:
        """Check if image exists."""
        try:
            # Use machinectl to check image
            result = await run_command(
                ["machinectl", "show-image", spec.name],
                check=False
            )
            
            if result.returncode == 0:
                return ProviderStatus.PRESENT
            else:
                return ProviderStatus.ABSENT
                
        except Exception as e:
            logger.error(f"Error checking image {spec.name}: {e}")
            return ProviderStatus.ERROR
            
    async def present(self, spec: ImageSpec) -> None:
        """Ensure image is present."""
        current_status = await self.status(spec)
        
        if current_status == ProviderStatus.PRESENT:
            logger.debug(f"Image {spec.name} already present")
            return
            
        logger.info(f"Pulling image {spec.name}")
        
        # Determine pull command based on type
        pull_cmd = "pull-tar" if spec.type == "tar" else "pull-raw"
        
        # Build command
        cmd = ["machinectl", pull_cmd, spec.source, spec.name]
        
        # Add verification option
        if spec.verify == "signature":
            cmd.append("--verify=signature")
        elif spec.verify == "checksum":
            cmd.append("--verify=checksum")
        elif spec.verify == "no":
            cmd.append("--verify=no")
            
        # Pull the image
        try:
            await run_command(cmd, timeout=600)
            logger.info(f"Image {spec.name} pulled successfully")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to pull image {spec.name}: {e}")
            raise
            
        # Clean up temporary files
        await self._clean_temp_files()
        
        # Make image read-only (keep it pristine)
        await self._make_read_only(spec.name)
        
    async def absent(self, spec: ImageSpec) -> None:
        """Ensure image is absent."""
        current_status = await self.status(spec)
        
        if current_status == ProviderStatus.ABSENT:
            logger.debug(f"Image {spec.name} already absent")
            return
            
        logger.info(f"Removing image {spec.name}")
        
        try:
            await run_command(["machinectl", "remove", spec.name])
            logger.info(f"Image {spec.name} removed successfully")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to remove image {spec.name}: {e}")
            raise
            
    async def validate_spec(self, spec: ImageSpec) -> bool:
        """Validate image specification."""
        # Basic validation is handled by Pydantic
        # Additional validation could be added here
        return True
        
    async def _clean_temp_files(self):
        """Clean temporary files left by machinectl."""
        try:
            # Check if there are temp files
            temp_patterns = [".raw*", ".tar*"]
            has_temp_files = False
            
            for pattern in temp_patterns:
                temp_files = list(self.machines_dir.glob(pattern))
                if temp_files:
                    has_temp_files = True
                    break
                    
            if has_temp_files:
                await run_command(["machinectl", "clean"])
                logger.debug("Cleaned temporary image files")
        except Exception as e:
            logger.warning(f"Error cleaning temp files: {e}")
            
    async def _make_read_only(self, image_name: str):
        """Make image read-only."""
        try:
            # Check if already read-only
            result = await run_command(
                ["machinectl", "show-image", image_name],
                capture_output=True
            )
            
            if "ReadOnly=yes" in result.stdout:
                logger.debug(f"Image {image_name} already read-only")
                return
                
            # Make read-only
            await run_command(["machinectl", "read-only", image_name, "true"])
            logger.debug(f"Made image {image_name} read-only")
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to make image {image_name} read-only: {e}")
            raise
