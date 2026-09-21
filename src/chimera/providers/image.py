"""
Image provider for managing container images.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chimera.errors import ChimeraError
from chimera.images.identity import EffectiveImage, native_debian_architecture
from chimera.images.resolver import download_verified_artifact, resolve_image_artifact
from chimera.images.simplestreams import SIMPLESTREAMS_SQUASHFS_FTYPE
from chimera.providers.base import BaseProvider, ProviderStatus
from chimera.utils.fs import classify_materialization_path, normalize_nspawn_machine_id
from chimera.utils.systemd import run_command

if TYPE_CHECKING:
    from chimera.providers.registry import ProviderRegistry


logger = logging.getLogger(__name__)
IMPORT_TIMEOUT = 600


class ImageProvider(BaseProvider[Any]):
    """Provider for managing systemd-nspawn images."""

    def __init__(self) -> None:
        """Initialize image provider."""
        self.machines_dir: Path | None = None

    async def initialize(self, config: Any, registry: "ProviderRegistry") -> None:
        """Initialize provider with configuration and registry."""
        self.machines_dir = Path(config.systemd.machines_dir)

    async def status(self, spec: EffectiveImage) -> ProviderStatus:
        """Return a proven image state without treating observation failure as absence."""
        name = spec.local_image_name
        try:
            machines_dir = self._require_machines_dir()
            directory_image = machines_dir / name
            raw_image = machines_dir / f"{name}.raw"
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
                logger.error("Could not list images while checking %s: %s", name, result.stderr)
                return ProviderStatus.ERROR
            names = {line.split()[0] for line in result.stdout.splitlines() if line.split()}
            return ProviderStatus.PRESENT if name in names else ProviderStatus.ABSENT
        except (OSError, ChimeraError) as error:
            logger.error("Could not determine whether image %s exists: %s", name, error)
            return ProviderStatus.ERROR

    async def present(self, spec: EffectiveImage) -> None:
        """Ensure a completed read-only image cache is present."""
        current_status = await self.status(spec)
        if current_status == ProviderStatus.ERROR:
            raise self._observation_failure(spec.local_image_name)
        if current_status == ProviderStatus.PRESENT:
            await self._require_completed_cache(spec.local_image_name)
            logger.debug("Image %s already present", spec.local_image_name)
            return

        try:
            await self._present_simplestreams_image(spec)
            await self._require_completed_cache(spec.local_image_name)
        except BaseException:
            await self._rollback_new_cache(spec)
            raise

    async def absent(self, spec: EffectiveImage) -> None:
        """Ensure image is absent."""
        current_status = await self.status(spec)

        if current_status == ProviderStatus.ABSENT:
            logger.debug("Image %s already absent", spec.local_image_name)
            return
        if current_status == ProviderStatus.ERROR:
            raise self._observation_failure(spec.local_image_name)

        logger.info("Removing image %s", spec.local_image_name)

        try:
            await run_command(["machinectl", "remove", spec.local_image_name])
            logger.info("Image %s removed successfully", spec.local_image_name)
        except subprocess.CalledProcessError as e:
            logger.error(
                "Failed to remove image %s: %s. Stderr: %s",
                spec.local_image_name,
                e,
                e.stderr,
            )
            raise

    async def validate_spec(self, spec: EffectiveImage) -> bool:
        """Validate image semantics that Pydantic field types cannot express."""
        for custom_file in spec.custom_files:
            if custom_file.ensure == "present":
                raise ChimeraError(
                    code="invalid_configuration",
                    message="custom_files ensure=present is not supported.",
                    detail=f"Image '{spec.canonical_image_id}' requests {custom_file.path}.",
                    suggestion="Use ensure=absent or ensure=link; content creation is not defined yet.",
                    status=422,
                )
            if custom_file.ensure == "link" and not custom_file.target:
                raise ChimeraError(
                    code="invalid_configuration",
                    message="custom_files ensure=link requires a target.",
                    detail=f"Image '{spec.canonical_image_id}' requests {custom_file.path}.",
                    suggestion="Set a non-empty target or remove this custom_files entry.",
                    status=422,
                )
        return True

    async def inspect_local(self, spec: EffectiveImage) -> dict[str, Any]:
        """Observe local machinectl image presence and read-only state when safe."""
        status = await self.status(spec)
        payload: dict[str, Any] = {
            "name": spec.local_image_name,
            "status": status.value,
            "read_only": None,
        }
        if status != ProviderStatus.PRESENT:
            return payload
        try:
            result = await run_command(
                ["machinectl", "show-image", spec.local_image_name], capture_output=True
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            return payload
        if "ReadOnly=yes" in result.stdout:
            payload["read_only"] = True
        elif "ReadOnly=no" in result.stdout:
            payload["read_only"] = False
        return payload

    async def _present_simplestreams_image(self, spec: EffectiveImage) -> None:
        """Resolve, verify, and import a SimpleStreams artifact as a read-only local image."""
        source = spec.source_spec
        logger.info(
            "Resolving image %s/%s from source %s",
            spec.canonical_image_id,
            spec.artifact_kind,
            spec.source_name,
        )
        artifact = await resolve_image_artifact(
            source,
            spec.canonical_image_id,
            architecture=native_debian_architecture(),
            artifact_kind=spec.artifact_kind,
        )
        temp_root = _download_temp_root()
        tmpdir = await asyncio.to_thread(
            tempfile.mkdtemp, prefix="chimera-image-", dir=str(temp_root)
        )
        destination = Path(tmpdir) / _artifact_filename(artifact.ftype, spec.artifact_kind)
        mountpoint = Path(tmpdir) / "mnt"
        try:
            await download_verified_artifact(artifact, destination, source_base=source.url)
            try:
                await self._import_verified_artifact(
                    destination,
                    spec.local_image_name,
                    artifact.ftype,
                    spec.artifact_kind,
                    mountpoint,
                )
                logger.info(
                    "Image %s imported from source %s",
                    spec.local_image_name,
                    spec.source_name,
                )
            except subprocess.CalledProcessError as error:
                logger.error(
                    "Failed to import image %s: %s. Stderr: %s",
                    spec.local_image_name,
                    error,
                    error.stderr,
                )
                raise
        finally:
            await asyncio.to_thread(shutil.rmtree, tmpdir, True)

    async def _make_read_only(self, image_name: str) -> None:
        """Make image read-only."""
        try:
            result = await run_command(
                ["machinectl", "show-image", image_name], capture_output=True
            )

            if "ReadOnly=yes" in result.stdout:
                logger.debug("Image %s already read-only", image_name)
                return

            await run_command(["machinectl", "read-only", image_name, "true"])
            logger.debug("Made image %s read-only", image_name)

        except subprocess.CalledProcessError as e:
            logger.error(
                "Failed to make image %s read-only: %s. Stderr: %s",
                image_name,
                e,
                e.stderr,
            )
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

    async def _import_verified_artifact(
        self,
        destination: Path,
        name: str,
        ftype: str,
        artifact_kind: str,
        mountpoint: Path,
    ) -> None:
        """Import a verified tar, squashfs, or disk through native systemd tooling."""
        if artifact_kind == "disk":
            await self._import_disk(destination, name)
            return
        if ftype == SIMPLESTREAMS_SQUASHFS_FTYPE:
            await self._import_squashfs(destination, name, mountpoint)
            return
        await self._import_rootfs_tar(destination, name)

    async def _import_rootfs_tar(self, destination: Path, name: str) -> None:
        """Import a verified rootfs tar as a read-only machine image."""
        await run_command(local_tar_import_command(str(destination), name), timeout=IMPORT_TIMEOUT)

    async def _import_disk(self, destination: Path, name: str) -> None:
        """Import a verified raw/QCOW2 disk as a read-only machine image."""
        await run_command(local_raw_import_command(str(destination), name), timeout=IMPORT_TIMEOUT)

    async def _import_squashfs(self, destination: Path, name: str, mountpoint: Path) -> None:
        """Mount a verified squashfs, import it, and unmount the source."""
        await asyncio.to_thread(mountpoint.mkdir, parents=True, exist_ok=True)
        mounted = False
        failure: BaseException | None = None
        try:
            await run_command(
                ["mount", "-t", "squashfs", "-o", "loop,ro", str(destination), str(mountpoint)]
            )
            mounted = True
            await run_command(
                local_fs_import_command(str(mountpoint), name), timeout=IMPORT_TIMEOUT
            )
            machines_dir = self._require_machines_dir()
            await asyncio.to_thread(normalize_nspawn_machine_id, machines_dir / name)
            await self._make_read_only(name)
        except BaseException as error:
            failure = error
            raise
        finally:
            if mounted:
                try:
                    await run_command(["umount", str(mountpoint)])
                except Exception as umount_error:
                    if failure is None:
                        raise
                    logger.error(
                        "Could not unmount squashfs import source for %s after import failure: %s",
                        name,
                        umount_error,
                    )

    async def _require_completed_cache(self, name: str) -> None:
        """Accept an existing cache only when systemd reports it read-only."""
        try:
            result = await run_command(["machinectl", "show-image", name], capture_output=True)
        except (FileNotFoundError, subprocess.CalledProcessError, OSError) as error:
            raise ChimeraError(
                code="host_observation_failed",
                message=f"Could not determine whether image '{name}' is a completed read-only cache.",
                detail=str(error),
                suggestion="Check machinectl and the Chimera server journal, then retry.",
                status=503,
            ) from error
        if "ReadOnly=yes" in result.stdout:
            return
        if "ReadOnly=no" in result.stdout:
            raise ChimeraError(
                code="image_cache_incomplete",
                message=f"Image cache '{name}' exists but is not a completed read-only image.",
                suggestion=(
                    "Remove that incomplete cache with machinectl, then pull the image again. "
                    "Chimera will not rewrite an existing writable cache."
                ),
                status=409,
            )
        raise ChimeraError(
            code="host_observation_failed",
            message=f"Could not determine whether image '{name}' is read-only.",
            detail=result.stdout.strip() or None,
            suggestion="Check machinectl show-image and the Chimera server journal, then retry.",
            status=503,
        )

    async def _rollback_new_cache(self, spec: EffectiveImage) -> None:
        """Remove a cache created by this failed import without masking the primary error."""
        name = spec.local_image_name
        try:
            current = await self.status(spec)
        except Exception as error:
            logger.error("Could not inspect image cache %s after import failure: %s", name, error)
            current = ProviderStatus.PRESENT
        if current == ProviderStatus.ABSENT:
            return
        try:
            await run_command(["machinectl", "remove", name])
            logger.warning("Removed incomplete image cache %s after import failure", name)
        except Exception as error:
            logger.error("Could not remove incomplete image cache %s: %s", name, error)

    @staticmethod
    def _observation_failure(name: str) -> ChimeraError:
        """Expose an unavailable machinectl observation without changing host state."""
        return ChimeraError(
            code="host_observation_failed",
            message=f"Could not determine whether image '{name}' exists.",
            suggestion="Check machinectl and the Chimera server journal, then retry.",
            status=503,
        )


def local_tar_import_command(path: str, name: str) -> list[str]:
    """Import a verified local tar through native systemd tooling."""
    if shutil.which("importctl") is not None:
        return ["importctl", "--class=machine", "--read-only", "import-tar", path, name]
    return ["machinectl", "--read-only", "import-tar", path, name]


def local_fs_import_command(path: str, name: str) -> list[str]:
    """Import a verified directory tree writable so Chimera can normalize nspawn files."""
    if shutil.which("importctl") is not None:
        return ["importctl", "--class=machine", "import-fs", path, name]
    return ["machinectl", "import-fs", path, name]


def local_raw_import_command(path: str, name: str) -> list[str]:
    """Import a verified local raw/QCOW2 disk through native systemd tooling."""
    if shutil.which("importctl") is not None:
        return ["importctl", "--class=machine", "--read-only", "import-raw", path, name]
    return ["machinectl", "--read-only", "import-raw", path, name]


def _artifact_filename(ftype: str, artifact_kind: str) -> str:
    """Name the downloaded artifact from its SimpleStreams ftype and kind."""
    if artifact_kind == "disk":
        if ftype.endswith(".img") or ftype.endswith(".qcow2") or ftype.endswith(".raw"):
            return f"disk.{ftype.rsplit('.', 1)[-1]}"
        return "disk.img"
    if ftype == SIMPLESTREAMS_SQUASHFS_FTYPE:
        return "rootfs.squashfs"
    return "root.tar.xz"


def _usable_temp_dir(path: Path) -> bool:
    """Accept TMPDIR or /var/tmp only when the path is an existing usable directory."""
    try:
        return path.is_dir() and os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False


def _download_temp_root() -> Path:
    """Prefer disk-backed temporary storage for multi-hundred-megabyte tarballs."""
    configured = os.environ.get("TMPDIR")
    if configured:
        candidate = Path(configured)
        if _usable_temp_dir(candidate):
            return candidate
    var_tmp = Path("/var/tmp")
    if _usable_temp_dir(var_tmp):
        return var_tmp
    return Path(tempfile.gettempdir())
