"""Container provider for managing systemd-nspawn containers."""

import asyncio
import logging
import subprocess
import shlex
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, TYPE_CHECKING

from chimera.providers.base import BaseProvider, ProviderStatus
from chimera.models.container import ContainerSpec
from chimera.utils.systemd import run_command, SystemdDBus
from chimera.utils.templates import render_template

if TYPE_CHECKING:
    from chimera.providers.registry import ProviderRegistry
    from chimera.providers.cloudinit import CloudInitProvider

logger = logging.getLogger(__name__)


class ContainerProvider(BaseProvider):
    """Provider for managing systemd-nspawn containers."""
    
    def __init__(self):
        """Initialize container provider."""
        self.machines_dir: Optional[Path] = None
        self.nspawn_dir: Optional[Path] = None
        self.system_dir: Optional[Path] = None
        self.systemd_dbus = SystemdDBus()
        self.proxy_config = None
        self._cloudinit_provider: Optional["CloudInitProvider"] = None
        
    async def initialize(self, config, registry: "ProviderRegistry") -> None:
        """Initialize provider with configuration and registry."""
        self.machines_dir = Path(config.systemd.machines_dir)
        self.nspawn_dir = Path(config.systemd.nspawn_dir)
        self.system_dir = Path(config.systemd.system_dir)
        self.proxy_config = config.proxy
        
        # Inject dependency explicitly
        self._cloudinit_provider = registry.get_provider("cloudinit")
        
        await self.systemd_dbus.connect()
        
    @property
    def cloudinit_provider(self) -> Optional["CloudInitProvider"]:
        """Get cloud-init provider."""
        return self._cloudinit_provider

    async def status(self, spec: ContainerSpec) -> ProviderStatus:
        """Check if container exists."""
        try:
            result = await run_command(
                ["machinectl", "show", spec.name],
                check=False
            )
            
            if result.returncode == 0:
                return ProviderStatus.PRESENT
            else:
                # Check if files exist (for both directory and raw images)
                container_dir = self.machines_dir / spec.name
                container_raw = self.machines_dir / f"{spec.name}.raw"
                
                # Check existence asynchronously (stat call)
                dir_exists = await asyncio.to_thread(container_dir.exists)
                raw_exists = await asyncio.to_thread(container_raw.exists)
                
                if dir_exists or raw_exists:
                    # Container files exist but machinectl doesn't see it
                    # For raw images, this might be normal if it's never been started
                    if raw_exists and spec._image_spec and spec._image_spec.type == "raw":
                        # Raw image exists, consider it present
                        return ProviderStatus.PRESENT
                    else:
                        # Directory-based container with issues
                        logger.warning(f"Container {spec.name} has partial creation")
                        # NOTE: Destructive cleanup removed from read-only status check
                        # Cleanup is deferred to state-mutating methods (present/absent)
                        
                return ProviderStatus.ABSENT
                
        except Exception as e:
            logger.error(f"Error checking container {spec.name}: {e}")
            return ProviderStatus.ERROR
            
    async def present(self, spec: ContainerSpec) -> None:
        """Ensure container is present."""
        current_status = await self.status(spec)
        
        if current_status == ProviderStatus.PRESENT:
            logger.debug(f"Container {spec.name} already present")
            # Skip cloning but ensure configs are in place
            await self._ensure_configs(spec)
            return
            
        logger.info(f"Creating container {spec.name}")

        # Ensure a clean slate before attempting a clone operation
        # This handles the case of partial/broken containers detected by status
        await self._cleanup_partial_container(spec.name)
        
        # Check if this is a raw image
        is_raw_image = spec._image_spec and spec._image_spec.type == "raw"
        
        # Clone image to create container
        try:
            # Extended timeout for cloning operations
            await run_command([
                "machinectl", "clone", spec.image, spec.name
            ], timeout=600)
            logger.debug(f"Cloned image {spec.image} to container {spec.name}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to clone image: {e}. Stderr: {e.stderr}")
            raise
            
        # Only apply modifications for tar-based images
        if not is_raw_image:
            # Apply custom file modifications from image spec
            if spec._image_spec and spec._image_spec.custom_files:
                await self._apply_custom_files(spec.name, spec._image_spec.custom_files)
                
            # Prepare cloud-init if specified
            if spec.cloud_init:
                if self.cloudinit_provider:
                    await self.cloudinit_provider.prepare(spec)
                else:
                    logger.warning("CloudInitProvider not found in registry")
        else:
            logger.info(f"Container {spec.name} uses raw image - skipping custom_files and cloud-init")
            
        await self._ensure_configs(spec)
        
    async def _ensure_configs(self, spec: ContainerSpec) -> None:
        """Ensure configuration files are in place."""
        # Create .nspawn configuration file
        if spec._profile_spec and spec._profile_spec.nspawn_config_content:
            await self._create_nspawn_config(spec)
            
        # Create systemd service override
        if spec._profile_spec and spec._profile_spec.systemd_override_content:
            await self._create_systemd_override(spec)
            
        # Enable service if autostart
        if spec.autostart:
            await self._enable_service(spec.name)
            
        # Start if desired state is running
        if spec.state == "running":
            await self.start(spec)
            
    async def absent(self, spec: ContainerSpec) -> None:
        """Ensure container is absent."""
        current_status = await self.status(spec)
        
        if current_status == ProviderStatus.PRESENT:
            logger.info(f"Removing container {spec.name}")
            
            # Stop service first
            await self.stop(spec)
            
            # Disable service
            await self._disable_service(spec.name)
            
            # Remove container
            try:
                # Extended timeout for removal (can be slow for large containers or hung processes)
                await run_command(["machinectl", "remove", spec.name], timeout=120)
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to remove container: {e}. Stderr: {e.stderr}")
                raise
        else:
            logger.debug(f"Container {spec.name} already absent (checking for residuals)")

        # Clean up any residuals, configuration files, or partial states
        # This makes the method idempotent and handles the case where status returned ABSENT
        # but the directory or config files still exist (partial state)
        await self._cleanup_partial_container(spec.name)
            
    async def validate_spec(self, spec: ContainerSpec) -> bool:
        """Validate container specification."""
        # Check that referenced image exists
        if spec._image_spec is None:
            logger.error(f"Image {spec.image} not found in configuration")
            return False
            
        # Check that referenced profile exists
        if spec._profile_spec is None:
            logger.error(f"Profile {spec.profile} not found in configuration")
            return False
            
        return True
        
    async def is_running(self, spec: ContainerSpec) -> bool:
        """Check if container is running."""
        try:
            service_name = f"systemd-nspawn@{spec.name}.service"
            state = await self.systemd_dbus.get_unit_state(service_name)
            return state == "active"
            
        except Exception as e:
            logger.error(f"Error checking container state: {e}")
            return False
            
    async def start(self, spec: ContainerSpec) -> None:
        """Start the container."""
        if await self.is_running(spec):
            logger.debug(f"Container {spec.name} already running")
            return
            
        logger.info(f"Starting container {spec.name}")
        
        service_name = f"systemd-nspawn@{spec.name}.service"
        try:
            await self.systemd_dbus.start_unit(service_name)
            
            # Wait for container to be ready
            await self._wait_for_ready(spec.name)
            
        except Exception as e:
            logger.error(f"Failed to start container: {e}")
            raise
            
    async def stop(self, spec: ContainerSpec) -> None:
        """Stop the container."""
        if not await self.is_running(spec):
            logger.debug(f"Container {spec.name} already stopped")
            return
            
        logger.info(f"Stopping container {spec.name}")
        
        service_name = f"systemd-nspawn@{spec.name}.service"
        try:
            await self.systemd_dbus.stop_unit(service_name)
        except Exception as e:
            logger.error(f"Failed to stop container: {e}")
            raise
            
    async def execute(self, spec: ContainerSpec, command: list[str]) -> Dict[str, Any]:
        """Execute command in container."""
        # Use shlex.join to safely quote arguments for shell execution
        # avoiding injection vulnerabilities
        safe_command = shlex.join(command)
        cmd = ["machinectl", "shell", spec.name, "/bin/bash", "-c", safe_command]
        
        try:
            result = await run_command(cmd, capture_output=True, check=True)
            
            return {
                "exit_code": 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except subprocess.CalledProcessError as e:
            return {
                "exit_code": e.returncode,
                "stdout": e.stdout if hasattr(e, 'stdout') else "",
                "stderr": e.stderr if hasattr(e, 'stderr') else "",
            }
            
    async def _apply_custom_files(self, container_name: str, custom_files: list) -> None:
        """Apply custom file modifications to the cloned container."""
        container_path = self.machines_dir / container_name
        
        for file_spec in custom_files:
            file_path = container_path / file_spec.path
            
            try:
                if file_spec.ensure == "absent":
                    if await asyncio.to_thread(file_path.exists):
                        if await asyncio.to_thread(file_path.is_dir):
                            await asyncio.to_thread(file_path.rmdir)
                        else:
                            await asyncio.to_thread(file_path.unlink)
                        logger.debug(f"Removed {file_path} from container {container_name}")
                        
                elif file_spec.ensure == "link":
                    if file_spec.target:
                        # Remove existing file/link if present
                        if await asyncio.to_thread(file_path.exists) or await asyncio.to_thread(file_path.is_symlink):
                            await asyncio.to_thread(file_path.unlink)
                            
                        # Create parent directory if needed
                        # parent.mkdir is blocking, use to_thread
                        await asyncio.to_thread(lambda: file_path.parent.mkdir(parents=True, exist_ok=True))
                        
                        # Create symbolic link
                        await asyncio.to_thread(file_path.symlink_to, file_spec.target)
                        logger.debug(f"Created symlink {file_path} -> {file_spec.target} in container {container_name}")
                        
            except Exception as e:
                logger.error(f"Error modifying {file_path} in container {container_name}: {e}")
                
    async def _create_nspawn_config(self, spec: ContainerSpec) -> None:
        """Create .nspawn configuration file."""
        nspawn_file = self.nspawn_dir / f"{spec.name}.nspawn"
        
        # Ensure directory exists
        await asyncio.to_thread(lambda: self.nspawn_dir.mkdir(parents=True, exist_ok=True))
        
        # Render template with any variables
        content = render_template(
            spec._profile_spec.nspawn_config_content,
            container_name=spec.name,
            proxy=self.proxy_config,
        )
        
        # Write config file
        await asyncio.to_thread(nspawn_file.write_text, content)
        logger.debug(f"Created nspawn config: {nspawn_file}")
        
        # Reload systemd
        await self.systemd_dbus.reload_daemon()
        
    async def _create_systemd_override(self, spec: ContainerSpec) -> None:
        """Create systemd service override."""
        override_dir = self.system_dir / f"systemd-nspawn@{spec.name}.service.d"
        override_file = override_dir / "override.conf"
        
        # Create directory
        await asyncio.to_thread(lambda: override_dir.mkdir(parents=True, exist_ok=True))
        
        # Render template
        content = render_template(
            spec._profile_spec.systemd_override_content,
            container_name=spec.name,
        )
        
        # Write override file
        await asyncio.to_thread(override_file.write_text, content)
        logger.debug(f"Created systemd override: {override_file}")
        
        # Reload systemd
        await self.systemd_dbus.reload_daemon()
        
    async def _enable_service(self, container_name: str) -> None:
        """Enable container service."""
        service_name = f"systemd-nspawn@{container_name}.service"
        try:
            await self.systemd_dbus.enable_unit(service_name)
            logger.debug(f"Enabled service {service_name}")
        except Exception as e:
            logger.error(f"Failed to enable service: {e}")
            
    async def _disable_service(self, container_name: str) -> None:
        """Disable container service."""
        service_name = f"systemd-nspawn@{container_name}.service"
        try:
            await self.systemd_dbus.disable_unit(service_name)
            logger.debug(f"Disabled service {service_name}")
        except Exception as e:
            logger.warning(f"Failed to disable service: {e}")
            
    async def _cleanup_config_files(self, container_name: str) -> None:
        """Clean up configuration files."""
        # Remove .nspawn file
        nspawn_file = self.nspawn_dir / f"{container_name}.nspawn"
        if await asyncio.to_thread(nspawn_file.exists):
            await asyncio.to_thread(nspawn_file.unlink)
            
        # Remove systemd override directory
        override_dir = self.system_dir / f"systemd-nspawn@{container_name}.service.d"
        if await asyncio.to_thread(override_dir.exists):
            await asyncio.to_thread(shutil.rmtree, override_dir)
            
    async def _cleanup_partial_container(self, container_name: str) -> None:
        """Clean up partially created container."""
        # Note: This is now called defensively in present/absent and handles logging internally.
        # We might see "Cleaning up partial container" even if nothing is there if we're not careful,
        # but the checks inside are guarded by exists().
        
        # Clean up directory
        container_dir = self.machines_dir / container_name
        if await asyncio.to_thread(container_dir.exists):
            logger.info(f"Cleaning up container directory {container_name}")
            await asyncio.to_thread(shutil.rmtree, container_dir)
            logger.debug(f"Removed partial container directory: {container_dir}")
            
        # Clean up raw file
        container_raw = self.machines_dir / f"{container_name}.raw"
        if await asyncio.to_thread(container_raw.exists):
            logger.info(f"Cleaning up container raw file {container_name}")
            await asyncio.to_thread(container_raw.unlink)
            logger.debug(f"Removed partial container raw file: {container_raw}")
            
        # Clean up any config files
        await self._cleanup_config_files(container_name)
            
    async def _wait_for_ready(self, container_name: str, timeout: int = 30) -> None:
        """Wait for container to be ready."""
        for i in range(timeout):
            try:
                result = await run_command([
                    "machinectl", "shell", container_name, "/bin/true"
                ], check=False)
                
                if result.returncode == 0:
                    logger.debug(f"Container {container_name} is ready")
                    return
                    
            except Exception:
                pass
                
            await asyncio.sleep(1)
            
        logger.warning(f"Container {container_name} did not become ready in {timeout}s")
