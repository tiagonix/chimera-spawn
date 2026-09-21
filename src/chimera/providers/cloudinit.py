"""
Cloud-init provider for container initialization.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chimera.errors import ChimeraError
from chimera.models.container import ContainerSpec
from chimera.providers.base import BaseProvider, ProviderStatus
from chimera.utils.fs import classify_materialization_path, write_contained_text
from chimera.utils.rendering import creation_render_payload

if TYPE_CHECKING:
    from chimera.providers.registry import ProviderRegistry


logger = logging.getLogger(__name__)


class CloudInitProvider(BaseProvider[ContainerSpec]):
    """Provider for managing cloud-init configurations."""

    def __init__(self) -> None:
        """Initialize cloud-init provider."""
        self.machines_dir: Path | None = None
        self.proxy_config: Any | None = None

    async def initialize(self, config: Any, registry: "ProviderRegistry") -> None:
        """Initialize provider with configuration and registry."""
        self.machines_dir = Path(config.systemd.machines_dir)
        self.proxy_config = config.proxy

    async def status(self, spec: ContainerSpec) -> ProviderStatus:
        """Check cloud-init status."""
        if not spec.cloud_init:
            return ProviderStatus.ABSENT

        container_path = self._require_machines_dir() / spec.name
        seed_dir = container_path / "var/lib/cloud/seed/nocloud"
        exists = await asyncio.to_thread(seed_dir.exists)
        return ProviderStatus.PRESENT if exists else ProviderStatus.ABSENT

    async def present(self, spec: ContainerSpec) -> None:
        """Ensure cloud-init is configured."""
        await self.prepare(spec)

    async def absent(self, spec: ContainerSpec) -> None:
        """Remove cloud-init configuration."""
        container_path = self._require_machines_dir() / spec.name
        cloud_dir = container_path / "var/lib/cloud"
        if await asyncio.to_thread(cloud_dir.exists):
            await asyncio.to_thread(shutil.rmtree, cloud_dir)
            logger.debug("Removed cloud-init directory for %s", spec.name)

    async def validate_spec(self, spec: ContainerSpec) -> bool:
        """Validate cloud-init specification."""
        return True

    async def prepare(self, spec: ContainerSpec) -> None:
        """Write cloud-init seed files inside a verified container root."""
        if not spec.cloud_init:
            logger.debug("No cloud-init config for container %s", spec.name)
            return

        container_path = self._require_machines_dir() / spec.name
        kind = await asyncio.to_thread(classify_materialization_path, container_path)
        if kind != "directory":
            raise ChimeraError(
                code="provisioning_failed",
                message=f"Container '{spec.name}' is not a writable root filesystem for cloud-init.",
                suggestion="Cloud-init seeding requires a stopped directory materialization.",
                status=422,
            )
        payload = creation_render_payload(
            container_name=spec.name,
            image=spec._effective_image,
            cloud_init=spec.cloud_init,
            proxy=self.proxy_config,
        )
        files = payload["files"]
        if not isinstance(files, dict):
            raise ChimeraError(
                code="provisioning_failed",
                message="Cloud-init rendering produced an invalid file map.",
                status=502,
            )
        for relative, content in files.items():
            if not isinstance(relative, str) or not isinstance(content, str):
                continue
            try:
                await asyncio.to_thread(write_contained_text, container_path, relative, content)
            except ChimeraError:
                raise
            except OSError as error:
                raise ChimeraError(
                    code="provisioning_failed",
                    message=f"Could not write cloud-init file '{relative}' for '{spec.name}'.",
                    detail=str(error),
                    status=502,
                ) from error
            logger.debug("Wrote contained cloud-init file %s for %s", relative, spec.name)

    def _require_machines_dir(self) -> Path:
        """Return the configured container directory after provider initialization."""
        if self.machines_dir is None:
            raise RuntimeError("Cloud-init provider is not initialized")
        return self.machines_dir
