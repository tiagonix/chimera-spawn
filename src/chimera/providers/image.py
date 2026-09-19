"""
Image provider for managing container images.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chimera.errors import ChimeraError
from chimera.models.image import ImageSpec
from chimera.providers.base import BaseProvider, ProviderStatus
from chimera.utils.fs import classify_materialization_path
from chimera.utils.systemd import run_command

if TYPE_CHECKING:
    from chimera.providers.registry import ProviderRegistry


logger = logging.getLogger(__name__)


class ImageProvider(BaseProvider[ImageSpec]):
    """Provider for managing systemd-nspawn images."""

    def __init__(self) -> None:
        """Initialize image provider."""
        self.machines_dir: Path | None = None

    async def initialize(self, config: Any, registry: "ProviderRegistry") -> None:
        """Initialize provider with configuration and registry."""
        self.machines_dir = Path(config.systemd.machines_dir)

    async def status(self, spec: ImageSpec) -> ProviderStatus:
        """Return a proven image state without treating observation failure as absence."""
        try:
            machines_dir = self._require_machines_dir()
            directory_image = machines_dir / spec.name
            raw_image = machines_dir / f"{spec.name}.raw"
            dir_kind = await asyncio.to_thread(classify_materialization_path, directory_image)
            raw_kind = await asyncio.to_thread(classify_materialization_path, raw_image)
            if dir_kind == "error" or raw_kind == "error":
                return ProviderStatus.ERROR
            if dir_kind == "invalid" or raw_kind == "invalid":
                return ProviderStatus.ERROR
            if dir_kind == "directory" or raw_kind == "file":
                return ProviderStatus.PRESENT

            try:
                result = await run_command(
                    ["machinectl", "list-images", "--no-legend", "--no-pager"],
                    check=False,
                )
            except FileNotFoundError:
                return ProviderStatus.ERROR
            if result.returncode != 0:
                logger.error(
                    "Could not list images while checking %s: %s", spec.name, result.stderr
                )
                return ProviderStatus.ERROR
            names = {line.split()[0] for line in result.stdout.splitlines() if line.split()}
            return ProviderStatus.PRESENT if spec.name in names else ProviderStatus.ABSENT
        except (OSError, ChimeraError) as error:
            logger.error("Could not determine whether image %s exists: %s", spec.name, error)
            return ProviderStatus.ERROR

    async def present(self, spec: ImageSpec) -> None:
        """Ensure image is present."""
        current_status = await self.status(spec)
        if current_status == ProviderStatus.PRESENT:
            logger.debug(f"Image {spec.name} already present")
            return
        if current_status == ProviderStatus.ERROR:
            raise self._observation_failure(spec.name)

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
            logger.error(f"Failed to pull image {spec.name}: {e}. Stderr: {e.stderr}")
            raise

        # Make image read-only (keep it pristine)
        await self._make_read_only(spec.name)

    async def absent(self, spec: ImageSpec) -> None:
        """Ensure image is absent."""
        current_status = await self.status(spec)

        if current_status == ProviderStatus.ABSENT:
            logger.debug(f"Image {spec.name} already absent")
            return
        if current_status == ProviderStatus.ERROR:
            raise self._observation_failure(spec.name)

        logger.info(f"Removing image {spec.name}")

        try:
            await run_command(["machinectl", "remove", spec.name])
            logger.info(f"Image {spec.name} removed successfully")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to remove image {spec.name}: {e}. Stderr: {e.stderr}")
            raise

    async def validate_spec(self, spec: ImageSpec) -> bool:
        """Validate image semantics that Pydantic field types cannot express."""
        if spec.type == "raw" and spec.custom_files:
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Raw image '{spec.name}' cannot use custom_files.",
                suggestion="Use a root-filesystem tar image or remove custom_files from this catalog entry.",
                status=422,
            )
        for custom_file in spec.custom_files:
            if custom_file.ensure == "present":
                raise ChimeraError(
                    code="invalid_configuration",
                    message="custom_files ensure=present is not supported.",
                    detail=f"Image '{spec.name}' requests {custom_file.path}.",
                    suggestion="Use ensure=absent or ensure=link; content creation is not defined yet.",
                    status=422,
                )
            if custom_file.ensure == "link" and not custom_file.target:
                raise ChimeraError(
                    code="invalid_configuration",
                    message="custom_files ensure=link requires a target.",
                    detail=f"Image '{spec.name}' requests {custom_file.path}.",
                    suggestion="Set a non-empty target or remove this custom_files entry.",
                    status=422,
                )
        return True

    async def _make_read_only(self, image_name: str) -> None:
        """Make image read-only."""
        try:
            # Check if already read-only
            result = await run_command(
                ["machinectl", "show-image", image_name], capture_output=True
            )

            if "ReadOnly=yes" in result.stdout:
                logger.debug(f"Image {image_name} already read-only")
                return

            # Make read-only
            await run_command(["machinectl", "read-only", image_name, "true"])
            logger.debug(f"Made image {image_name} read-only")

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to make image {image_name} read-only: {e}. Stderr: {e.stderr}")
            raise

    def _require_machines_dir(self) -> Path:
        """Return configured storage only after provider initialization."""
        if self.machines_dir is None:
            raise ChimeraError(
                code="service_unavailable",
                message="The image provider is not initialized.",
                suggestion="Inspect the Chimera server journal and restart the service.",
                status=503,
            )
        return self.machines_dir

    @staticmethod
    def _observation_failure(name: str) -> ChimeraError:
        """Expose an unavailable machinectl observation without changing host state."""
        return ChimeraError(
            code="host_observation_failed",
            message=f"Could not determine whether image '{name}' exists.",
            suggestion="Check machinectl and the Chimera server journal, then retry.",
            status=503,
        )
