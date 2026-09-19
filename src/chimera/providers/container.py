"""
Container provider for managing systemd-nspawn containers.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import asyncio
import logging
import os
import shlex
import shutil
import stat
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

from chimera.errors import ChimeraError
from chimera.models.container import ContainerSpec
from chimera.providers.base import BaseProvider, ProviderStatus
from chimera.utils.fs import classify_materialization_path, identity_for
from chimera.utils.rendering import (
    image_nspawn_parameters,
    render_nspawn_config,
    render_systemd_override,
)
from chimera.utils.systemd import SystemdDBus, run_command

if TYPE_CHECKING:
    from chimera.providers.cloudinit import CloudInitProvider
    from chimera.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class ContainerProvider(BaseProvider[ContainerSpec]):
    """Provider for managing systemd-nspawn containers."""

    def __init__(self) -> None:
        """Initialize container provider."""
        self.machines_dir: Path | None = None
        self.nspawn_dir: Path | None = None
        self.system_dir: Path | None = None
        self.systemd_dbus = SystemdDBus()
        self.proxy_config = None
        self._cloudinit_provider: CloudInitProvider | None = None

    async def initialize(self, config: Any, registry: "ProviderRegistry") -> None:
        """Initialize provider with configuration and registry."""
        self.machines_dir = Path(config.systemd.machines_dir)
        self.nspawn_dir = Path(config.systemd.nspawn_dir)
        self.system_dir = Path(config.systemd.system_dir)
        self.proxy_config = config.proxy

        # Inject dependency explicitly
        self._cloudinit_provider = cast(
            "CloudInitProvider | None", registry.get_provider("cloudinit")
        )

        await self.systemd_dbus.connect()

    @property
    def cloudinit_provider(self) -> Optional["CloudInitProvider"]:
        """Get cloud-init provider."""
        return self._cloudinit_provider

    async def status(self, spec: ContainerSpec) -> ProviderStatus:
        """Return proven materialization state without guessing from failures."""
        status, _identity = await self.inspect_materialization(spec)
        return status

    async def inspect_materialization(
        self, spec: ContainerSpec
    ) -> tuple[ProviderStatus, str | None]:
        """Return presence and the identity of a valid materialization only."""
        try:
            machines_dir = self._require_machines_dir()
            container_dir = machines_dir / spec.name
            container_raw = machines_dir / f"{spec.name}.raw"
            dir_kind = await asyncio.to_thread(classify_materialization_path, container_dir)
            raw_kind = await asyncio.to_thread(classify_materialization_path, container_raw)
            if dir_kind == "error" or raw_kind == "error":
                return ProviderStatus.ERROR, None
            if dir_kind == "invalid" or raw_kind == "invalid":
                logger.error(
                    "Container %s has an unexpected file or symlink at its storage path", spec.name
                )
                return ProviderStatus.ERROR, None
            if dir_kind == "directory":
                ident = await asyncio.to_thread(identity_for, container_dir, "dir")
                return ProviderStatus.PRESENT, ident
            if raw_kind == "file":
                ident = await asyncio.to_thread(identity_for, container_raw, "raw")
                return ProviderStatus.PRESENT, ident
            names = await self._list_image_inventory()
            if names is None:
                return ProviderStatus.ERROR, None
            if spec.name in names:
                return ProviderStatus.PRESENT, f"inventory:{spec.name}"
            return ProviderStatus.ABSENT, None
        except (OSError, ChimeraError) as error:
            logger.error("Could not determine whether container %s exists: %s", spec.name, error)
            return ProviderStatus.ERROR, None

    async def present(self, spec: ContainerSpec) -> None:
        """Materialize the container filesystem or raw image without provisioning."""
        current_status, _identity = await self.inspect_materialization(spec)

        if current_status == ProviderStatus.PRESENT:
            logger.debug("Container %s already present", spec.name)
            return
        if current_status != ProviderStatus.ABSENT:
            raise self._observation_failure(spec.name)

        logger.info("Creating container %s", spec.name)
        try:
            await run_command(["machinectl", "clone", spec.image, spec.name], timeout=600)
            logger.debug("Cloned image %s to container %s", spec.image, spec.name)
        except subprocess.CalledProcessError as error:
            logger.error("Failed to clone image: %s. Stderr: %s", error, error.stderr)
            raise

    async def provision_rootfs(self, spec: ContainerSpec) -> None:
        """Apply creation-time custom-files and cloud-init to a stopped container."""
        if await self.is_running(spec):
            raise ChimeraError(
                code="provisioning_failed",
                message=f"Cannot apply creation-time provisioning while '{spec.name}' is running.",
                suggestion="Stop the container or recreate it; Chimera will not rewrite a live root filesystem.",
                status=409,
            )
        await self._provision_rootfs(spec)

    async def apply_host_config(self, spec: ContainerSpec) -> None:
        """Write reconcilable nspawn and systemd host configuration for a stopped container."""
        if await self.is_running(spec):
            raise ChimeraError(
                code="provisioning_failed",
                message=f"Cannot rewrite host configuration while '{spec.name}' is running.",
                suggestion="Stop or restart the container to apply pending profile configuration.",
                status=409,
            )
        await self._ensure_configs(spec)

    async def _ensure_configs(self, spec: ContainerSpec) -> None:
        """Ensure configuration files are in place."""
        # Create .nspawn configuration file
        if spec._profile_spec and (
            spec._profile_spec.nspawn_config_content
            or spec.bind_mounts
            or spec.tmpfs_mounts
            or spec.port_forwards
        ):
            await self._create_nspawn_config(spec)

        # Create systemd service override
        if spec._profile_spec and (
            spec._profile_spec.systemd_override_content or spec.resource_controls is not None
        ):
            await self._create_systemd_override(spec)

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
        elif current_status == ProviderStatus.ERROR:
            raise self._observation_failure(spec.name)
        else:
            logger.debug("Owned container %s is already absent", spec.name)

        # Callers reach this method only through an existing store record.
        # The artifacts are therefore Chimera-owned even when a previous
        # materialization attempt did not create a machine image.
        await self._cleanup_owned_artifacts(spec.name)

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

        if spec._image_spec.type == "raw" and spec.cloud_init:
            raise ChimeraError(
                code="unsupported_provisioning",
                message=f"Raw image '{spec.image}' does not support cloud-init provisioning.",
                suggestion="Choose a root-filesystem tar image or launch the raw image without --cloud-init.",
                status=422,
            )

        return True

    async def preflight_create(self, spec: ContainerSpec) -> None:
        """Reject a new name that is already owned by unmanaged host resources."""
        collisions = await self.find_host_artifacts(spec)
        if collisions:
            raise ChimeraError(
                code="conflict",
                message=f"Container name '{spec.name}' is already used by an unmanaged host resource.",
                detail=", ".join(collisions),
                suggestion=(
                    "Choose another name. Use 'chimeractl config import-nodes' only when "
                    "intentionally migrating a legacy Chimera container."
                ),
                status=409,
            )

    async def find_host_artifacts(self, spec: ContainerSpec) -> list[str]:
        """List same-named host resources without modifying or adopting them."""
        status = await self.status(spec)
        if status == ProviderStatus.ERROR:
            raise self._observation_failure(spec.name)

        machines_dir, nspawn_dir, system_dir = self._require_paths()
        collisions: list[str] = []
        if status == ProviderStatus.PRESENT:
            collisions.append(f"machine/image named '{spec.name}'")

        try:
            online = await self.list_online_machine_names()
        except ChimeraError:
            raise
        except Exception as error:
            raise ChimeraError(
                code="host_observation_failed",
                message="Could not list online machines before claiming a container name.",
                detail=str(error),
                suggestion="Check machinectl and the Chimera server journal, then retry.",
                status=503,
            ) from error
        if spec.name in online:
            collisions.append(f"online machine named '{spec.name}'")

        artifact_paths = {
            "container directory": machines_dir / spec.name,
            "raw container image": machines_dir / f"{spec.name}.raw",
            "nspawn configuration": nspawn_dir / f"{spec.name}.nspawn",
            "systemd override": system_dir / f"systemd-nspawn@{spec.name}.service.d",
        }
        for label, path in artifact_paths.items():
            if await asyncio.to_thread(os.path.lexists, path):
                collisions.append(label)
        return collisions

    async def list_online_machine_names(self) -> set[str]:
        """Return names registered in the online machine namespace."""
        try:
            machines = await self.systemd_dbus.list_machines()
        except Exception as error:
            raise ChimeraError(
                code="host_observation_failed",
                message="Could not list online machines.",
                detail=str(error),
                suggestion="Check machinectl and the Chimera server journal, then retry.",
                status=503,
            ) from error
        names: set[str] = set()
        for machine in machines:
            name = machine.get("name")
            if isinstance(name, str) and name:
                names.add(name)
        return names

    async def list_unmanaged_host_resources(
        self, managed_names: set[str], catalog_image_names: set[str] | None = None
    ) -> dict[str, list[str]]:
        """Inventory same-domain host artifacts that Chimera does not own."""
        catalog_image_names = catalog_image_names or set()
        machines_dir, nspawn_dir, system_dir = self._require_paths()
        machines = await self.list_unmanaged_machine_names(managed_names)
        storage_entries, storage_error = await asyncio.to_thread(
            self._list_unmanaged_names, machines_dir, managed_names, suffixes=("", ".raw")
        )
        nspawn_configs, nspawn_error = await asyncio.to_thread(
            self._list_unmanaged_names, nspawn_dir, managed_names, suffixes=(".nspawn",)
        )
        systemd_overrides, override_error = await asyncio.to_thread(
            self._list_unmanaged_override_names, system_dir, managed_names
        )
        catalog_hits: list[str] = []
        unmanaged_storage: list[str] = []
        for entry in storage_entries:
            name = entry[:-4] if entry.endswith(".raw") else entry
            if name in catalog_image_names:
                catalog_hits.append(entry)
            else:
                unmanaged_storage.append(entry)
        result: dict[str, list[str]] = {
            "machines": [name for name in machines if name not in catalog_image_names],
            "storage_entries": unmanaged_storage,
            "nspawn_configs": nspawn_configs,
            "systemd_overrides": systemd_overrides,
            "catalog_images": catalog_hits,
        }
        errors = [item for item in (storage_error, nspawn_error, override_error) if item]
        if errors:
            result["errors"] = errors
        if len(unmanaged_storage) >= 256 or len(nspawn_configs) >= 256:
            result["truncated"] = ["inventory"]
        return result

    async def list_unmanaged_machine_names(self, managed_names: set[str]) -> list[str]:
        """Report observed machine names outside the durable Chimera registry."""
        try:
            machines = await self.systemd_dbus.list_machines()
        except Exception as error:
            raise ChimeraError(
                code="host_observation_failed",
                message="Could not list host machines for diagnostics.",
                detail=str(error),
                suggestion="Check machinectl and the Chimera server journal, then retry.",
                status=503,
            ) from error
        return sorted(
            machine["name"]
            for machine in machines
            if isinstance(machine.get("name"), str) and machine["name"] not in managed_names
        )

    async def is_running(self, spec: ContainerSpec) -> bool:
        """Check running state, refusing to equate observation failure with stopped."""
        try:
            service_name = f"systemd-nspawn@{spec.name}.service"
            state = await self.systemd_dbus.get_unit_state(service_name)
        except Exception as error:
            raise ChimeraError(
                code="host_observation_failed",
                message=f"Could not determine whether container '{spec.name}' is running.",
                detail=str(error),
                suggestion="Check systemctl and the Chimera server journal, then retry.",
                status=503,
            ) from error
        if state == "active":
            return True
        if state in {"inactive", "failed", "not-found"}:
            return False
        raise ChimeraError(
            code="host_observation_failed",
            message=f"Container '{spec.name}' has an indeterminate systemd state.",
            detail=state,
            suggestion="Check systemctl and the Chimera server journal, then retry.",
            status=503,
        )

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
            if not await self._wait_for_ready(spec.name):
                raise ChimeraError(
                    code="container_not_ready",
                    message=f"Container '{spec.name}' did not become ready after starting.",
                    suggestion=(
                        f"Inspect it with 'chimeractl info {spec.name}' and "
                        f"'journalctl -u systemd-nspawn@{spec.name} -e'."
                    ),
                    status=502,
                )

        except Exception as e:
            logger.error(f"Failed to start container: {e}")
            raise

    async def restart(self, spec: ContainerSpec) -> None:
        """Restart a materialized container through its systemd unit."""
        logger.info(f"Restarting container {spec.name}")
        service_name = f"systemd-nspawn@{spec.name}.service"
        try:
            await self.systemd_dbus.restart_unit(service_name)
            if not await self._wait_for_ready(spec.name):
                raise ChimeraError(
                    code="container_not_ready",
                    message=f"Container '{spec.name}' did not become ready after restarting.",
                    suggestion=f"Inspect 'journalctl -u systemd-nspawn@{spec.name} -e'.",
                    status=502,
                )
        except Exception as error:
            logger.error(f"Failed to restart container: {error}")
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
            if not await self._wait_for_unit_state(service_name, {"inactive"}):
                raise ChimeraError(
                    code="container_not_stopped",
                    message=f"Container '{spec.name}' did not stop cleanly.",
                    suggestion=f"Inspect 'journalctl -u {service_name} -e' before retrying.",
                    status=502,
                )
        except Exception as e:
            logger.error(f"Failed to stop container: {e}")
            raise

    async def execute(self, spec: ContainerSpec, command: list[str]) -> dict[str, Any]:
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
                "stdout": e.stdout if hasattr(e, "stdout") else "",
                "stderr": e.stderr if hasattr(e, "stderr") else "",
            }

    async def _apply_custom_files(self, container_name: str, custom_files: list[Any]) -> None:
        """Apply requested leaf operations without following container symlinks."""
        machines_dir = self._require_machines_dir()
        container_root = machines_dir / container_name
        for file_spec in custom_files:
            try:
                await asyncio.to_thread(self._apply_custom_file, container_root, file_spec)
            except ChimeraError:
                raise
            except OSError as error:
                raise ChimeraError(
                    code="provisioning_failed",
                    message=f"Could not apply custom file '{file_spec.path}' to '{container_name}'.",
                    detail=str(error),
                    suggestion="Inspect the container filesystem and correct the image catalog entry.",
                    status=502,
                ) from error

    async def _provision_rootfs(self, spec: ContainerSpec) -> None:
        """Apply creation-time root-filesystem provisioning for a tar image."""
        image = spec._image_spec
        if image is None:
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Image '{spec.image}' is not in the Chimera catalog.",
                status=422,
            )
        if image.type == "raw":
            if image.custom_files or spec.cloud_init:
                raise ChimeraError(
                    code="unsupported_provisioning",
                    message=f"Raw image '{image.name}' cannot be modified by this release.",
                    suggestion="Use a root-filesystem tar image without raw-image provisioning.",
                    status=422,
                )
            return
        if image.custom_files:
            try:
                await self._apply_custom_files(spec.name, image.custom_files)
            except ChimeraError:
                raise
            except OSError as error:
                raise ChimeraError(
                    code="provisioning_failed",
                    message=f"Could not apply custom file provisioning to '{spec.name}'.",
                    detail=str(error),
                    suggestion="Inspect the container filesystem and correct the image catalog entry.",
                    status=502,
                ) from error
        if spec.cloud_init:
            if self.cloudinit_provider is None:
                raise ChimeraError(
                    code="service_unavailable",
                    message="Cloud-init provisioning was requested but its provider is unavailable.",
                    suggestion="Check the Chimera server journal and restart the service.",
                    status=503,
                )
            await self.cloudinit_provider.prepare(spec)

    async def _create_nspawn_config(self, spec: ContainerSpec) -> None:
        """Create .nspawn configuration file."""
        _, nspawn_dir, _ = self._require_paths()
        profile = spec._profile_spec
        if profile is None:
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Profile '{spec.profile}' is not in the Chimera catalog.",
                status=422,
            )
        nspawn_file = nspawn_dir / f"{spec.name}.nspawn"

        await asyncio.to_thread(lambda: nspawn_dir.mkdir(parents=True, exist_ok=True))

        content = render_nspawn_config(
            profile,
            spec.name,
            self.proxy_config,
            image_nspawn_parameters(spec._image_spec),
            spec.bind_mounts,
            spec.tmpfs_mounts,
            spec.port_forwards,
        )
        await asyncio.to_thread(nspawn_file.write_text, content)
        logger.debug("Created nspawn config: %s", nspawn_file)
        await self.systemd_dbus.reload_daemon()

    async def _create_systemd_override(self, spec: ContainerSpec) -> None:
        """Create systemd service override."""
        _, _, system_dir = self._require_paths()
        profile = spec._profile_spec
        if profile is None:
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Profile '{spec.profile}' is not in the Chimera catalog.",
                status=422,
            )
        override_dir = system_dir / f"systemd-nspawn@{spec.name}.service.d"
        override_file = override_dir / "override.conf"

        await asyncio.to_thread(lambda: override_dir.mkdir(parents=True, exist_ok=True))

        content = render_systemd_override(profile, spec.name, spec.resource_controls)
        await asyncio.to_thread(override_file.write_text, content)
        logger.debug("Created systemd override: %s", override_file)
        await self.systemd_dbus.reload_daemon()

    async def _enable_service(self, container_name: str) -> None:
        """Enable container service."""
        service_name = f"systemd-nspawn@{container_name}.service"
        try:
            await self.systemd_dbus.enable_unit(service_name)
            logger.debug(f"Enabled service {service_name}")
        except Exception as e:
            logger.error(f"Failed to enable service: {e}")
            raise

    async def _disable_service(self, container_name: str) -> None:
        """Disable container service."""
        service_name = f"systemd-nspawn@{container_name}.service"
        try:
            await self.systemd_dbus.disable_unit(service_name)
            logger.debug(f"Disabled service {service_name}")
        except Exception as e:
            logger.warning(f"Failed to disable service: {e}")
            raise

    async def _cleanup_owned_artifacts(self, container_name: str) -> None:
        """Remove only artifacts whose name is already owned by the store."""
        machines_dir, nspawn_dir, system_dir = self._require_paths()
        container_dir = machines_dir / container_name
        container_raw = machines_dir / f"{container_name}.raw"
        nspawn_file = nspawn_dir / f"{container_name}.nspawn"
        override_dir = system_dir / f"systemd-nspawn@{container_name}.service.d"
        for path in (container_dir, container_raw, nspawn_file, override_dir):
            await asyncio.to_thread(self._remove_owned_path, path)

    @staticmethod
    def _remove_owned_path(path: Path) -> None:
        """Remove a named owned artifact without following a symlink."""
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(path_stat.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()

    @staticmethod
    def _apply_custom_file(container_root: Path, file_spec: Any) -> None:
        """Apply one custom-file operation using dir_fd-relative no-follow walks."""
        ContainerProvider._mutate_custom_file_leaf(
            container_root,
            file_spec.path,
            ensure=file_spec.ensure,
            target=file_spec.target,
        )

    @staticmethod
    def _mutate_custom_file_leaf(
        container_root: Path,
        relative_path: str,
        *,
        ensure: str,
        target: str | None,
    ) -> None:
        """Walk from an opened container root and mutate only the final leaf."""
        parts = Path(relative_path).parts
        if not parts or parts[-1] in {"", ".", ".."}:
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Custom-file path '{relative_path}' has no leaf component.",
                status=422,
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        root_fd = os.open(container_root, flags)
        owned_fds = [root_fd]
        try:
            parent_fd = root_fd
            for part in parts[:-1]:
                try:
                    entry_stat = os.lstat(part, dir_fd=parent_fd)
                except FileNotFoundError:
                    if ensure != "link":
                        return
                    os.mkdir(part, 0o755, dir_fd=parent_fd)
                    entry_stat = os.lstat(part, dir_fd=parent_fd)
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise ChimeraError(
                        code="invalid_configuration",
                        message="A custom-file path contains an intermediate symlink.",
                        detail=relative_path,
                        suggestion="Remove the intermediate link from the image; Chimera will not follow it.",
                        status=422,
                    )
                if not stat.S_ISDIR(entry_stat.st_mode):
                    raise ChimeraError(
                        code="provisioning_failed",
                        message=f"Custom-file parent is not a directory for '{relative_path}'.",
                        suggestion="Correct the image layout or custom_files entry.",
                        status=502,
                    )
                next_fd = os.open(part, flags, dir_fd=parent_fd)
                owned_fds.append(next_fd)
                parent_fd = next_fd
            leaf = parts[-1]
            try:
                leaf_stat = os.lstat(leaf, dir_fd=parent_fd)
            except FileNotFoundError:
                leaf_stat = None
            if ensure == "absent":
                if leaf_stat is None:
                    return
                if stat.S_ISDIR(leaf_stat.st_mode):
                    os.rmdir(leaf, dir_fd=parent_fd)
                else:
                    os.unlink(leaf, dir_fd=parent_fd)
                return
            if ensure != "link" or not target:
                raise ChimeraError(
                    code="invalid_configuration",
                    message=f"Unsupported custom-file operation for '{relative_path}'.",
                    suggestion="Use ensure=absent or ensure=link with a target.",
                    status=422,
                )
            if leaf_stat is not None:
                if stat.S_ISDIR(leaf_stat.st_mode):
                    os.rmdir(leaf, dir_fd=parent_fd)
                else:
                    os.unlink(leaf, dir_fd=parent_fd)
            os.symlink(target, leaf, dir_fd=parent_fd)
        finally:
            for fd in reversed(owned_fds):
                with suppress(OSError):
                    os.close(fd)

    @staticmethod
    def _list_unmanaged_names(
        directory: Path, managed_names: set[str], *, suffixes: tuple[str, ...]
    ) -> tuple[list[str], str | None]:
        """Return bounded directory entries whose derived names are not managed."""
        if not directory.exists():
            return [], None
        if not directory.is_dir():
            return [], f"not a directory: {directory}"
        found: list[str] = []
        try:
            entries = sorted(os.listdir(directory))
        except OSError as error:
            return [], str(error)
        truncated = False
        for entry in entries:
            if entry.startswith("."):
                continue
            if len(found) >= 256:
                truncated = True
                break
            for suffix in suffixes:
                if suffix and entry.endswith(suffix):
                    name = entry[: -len(suffix)]
                    break
                if suffix == "" and "." not in entry:
                    name = entry
                    break
            else:
                if "" in suffixes:
                    name = entry
                else:
                    continue
            if name and name not in managed_names:
                found.append(entry)
        return found, ("truncated" if truncated else None)

    @staticmethod
    def _list_unmanaged_override_names(
        system_dir: Path, managed_names: set[str]
    ) -> tuple[list[str], str | None]:
        """Return systemd-nspawn override directories not owned by Chimera."""
        if not system_dir.exists():
            return [], None
        if not system_dir.is_dir():
            return [], f"not a directory: {system_dir}"
        found: list[str] = []
        try:
            entries = sorted(os.listdir(system_dir))
        except OSError as error:
            return [], str(error)
        prefix = "systemd-nspawn@"
        suffix = ".service.d"
        truncated = False
        for entry in entries:
            if not (entry.startswith(prefix) and entry.endswith(suffix)):
                continue
            if len(found) >= 256:
                truncated = True
                break
            name = entry[len(prefix) : -len(suffix)]
            if name and name not in managed_names:
                found.append(entry)
        return found, ("truncated" if truncated else None)

    async def _wait_for_ready(self, container_name: str, timeout: int = 30) -> bool:
        """Wait for container to be ready."""
        for _ in range(timeout):
            try:
                result = await run_command(
                    ["machinectl", "shell", container_name, "/bin/true"], check=False
                )

                if result.returncode == 0:
                    logger.debug(f"Container {container_name} is ready")
                    return True

            except Exception:
                pass

            await asyncio.sleep(1)

        logger.warning(f"Container {container_name} did not become ready in {timeout}s")
        return False

    async def _wait_for_unit_state(
        self, service_name: str, expected_states: set[str], timeout: float = 30
    ) -> bool:
        """Wait for an asynchronous systemd job to reach a stable target state."""
        attempts = max(1, int(timeout * 10))
        for _ in range(attempts):
            if await self.systemd_dbus.get_unit_state(service_name) in expected_states:
                return True
            await asyncio.sleep(0.1)
        logger.warning(
            "Unit %s did not reach one of %s in %ss",
            service_name,
            sorted(expected_states),
            timeout,
        )
        return False

    async def host_config_artifacts_present(self, spec: ContainerSpec) -> bool:
        """Check that owned nspawn/override files still exist for this container."""
        _, nspawn_dir, system_dir = self._require_paths()
        profile = spec._profile_spec
        if profile is None:
            return True
        if (
            profile.nspawn_config_content
            or spec.bind_mounts
            or spec.tmpfs_mounts
            or spec.port_forwards
        ):
            nspawn_file = nspawn_dir / f"{spec.name}.nspawn"
            if not await asyncio.to_thread(os.path.lexists, nspawn_file):
                return False
        if profile.systemd_override_content or spec.resource_controls is not None:
            override_file = system_dir / f"systemd-nspawn@{spec.name}.service.d" / "override.conf"
            if not await asyncio.to_thread(os.path.lexists, override_file):
                return False
        return True

    async def _list_image_inventory(self) -> set[str] | None:
        """Return machinectl image names, or None when observation failed."""
        try:
            result = await run_command(
                ["machinectl", "list-images", "--no-legend", "--no-pager"],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            return None
        except (OSError, TimeoutError):
            return None
        if result.returncode != 0:
            logger.error("machinectl list-images failed: %s", result.stderr)
            return None
        names: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts:
                names.add(parts[0])
        return names

    def _require_paths(self) -> tuple[Path, Path, Path]:
        """Return configured host paths only after complete initialization."""
        if self.machines_dir is None or self.nspawn_dir is None or self.system_dir is None:
            raise ChimeraError(
                code="service_unavailable",
                message="The container provider is not initialized.",
                suggestion="Inspect the Chimera server journal and restart the service.",
                status=503,
            )
        return self.machines_dir, self.nspawn_dir, self.system_dir

    def _require_machines_dir(self) -> Path:
        """Return storage path for a read-only materialization observation."""
        if self.machines_dir is None:
            raise ChimeraError(
                code="service_unavailable",
                message="The container provider is not initialized.",
                suggestion="Inspect the Chimera server journal and restart the service.",
                status=503,
            )
        return self.machines_dir

    @staticmethod
    def _observation_failure(name: str) -> ChimeraError:
        """Return a stable failure when host state cannot be determined safely."""
        return ChimeraError(
            code="host_observation_failed",
            message=f"Could not determine whether container '{name}' exists.",
            suggestion="Check machinectl and the Chimera server journal, then retry.",
            status=503,
        )
