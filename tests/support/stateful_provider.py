"""Stateful container provider for lifecycle contract tests."""

from __future__ import annotations

from typing import Any

from chimera.errors import ChimeraError
from chimera.providers.base import ProviderStatus


class StatefulContainerProvider:
    """A fake whose existence, provisioning, host files, and running state are independent."""

    def __init__(self) -> None:
        self.materialized: dict[str, str] = {}
        self.generation = 0
        self.provisioned_identity: dict[str, str] = {}
        self.host_files: set[str] = set()
        self.running: set[str] = set()
        self.fail_at: str | None = None
        self.inspect_error = False
        self.inventory_error = False
        self.online_machines: set[str] = set()
        self.restart_counts: dict[str, int] = {}
        self.start_counts: dict[str, int] = {}
        self.present_count = 0
        self.provision_count = 0
        self.apply_count = 0
        self.image_present_calls = 0

    def _maybe_fail(self, step: str) -> None:
        if self.fail_at == step:
            raise RuntimeError(f"{step} failed")

    async def validate_spec(self, spec: Any) -> bool:
        return spec._image_spec is not None and spec._profile_spec is not None

    async def preflight_create(self, spec: Any) -> None:
        if self.inventory_error:
            raise ChimeraError(
                "host_observation_failed",
                "Could not list online machines before claiming a container name.",
            )
        if spec.name in self.online_machines:
            raise ChimeraError(
                "conflict",
                f"Container name '{spec.name}' is already used by an unmanaged host resource.",
            )
        if spec.name in self.materialized:
            raise ChimeraError(
                "conflict",
                f"Container name '{spec.name}' is already used by an unmanaged host resource.",
            )

    async def find_host_artifacts(self, spec: Any) -> list[str]:
        if spec.name in self.materialized:
            return ["container directory"]
        return []

    async def inspect_materialization(self, spec: Any) -> tuple[ProviderStatus, str | None]:
        if self.inspect_error:
            return ProviderStatus.ERROR, None
        identity = self.materialized.get(spec.name)
        if identity is None:
            return ProviderStatus.ABSENT, None
        return ProviderStatus.PRESENT, identity

    async def status(self, spec: Any) -> ProviderStatus:
        status, _identity = await self.inspect_materialization(spec)
        return status

    async def present(self, spec: Any) -> None:
        self._maybe_fail("present")
        self.present_count += 1
        if spec.name in self.materialized:
            return
        self.generation += 1
        self.materialized[spec.name] = f"dir:1:{self.generation}"

    async def provision_rootfs(self, spec: Any) -> None:
        self._maybe_fail("provision_rootfs")
        if spec.name in self.running:
            raise ChimeraError(
                "provisioning_failed",
                f"Cannot apply creation-time provisioning while '{spec.name}' is running.",
            )
        identity = self.materialized[spec.name]
        self.provisioned_identity[spec.name] = identity
        self.provision_count += 1

    async def apply_host_config(self, spec: Any) -> None:
        self._maybe_fail("apply_host_config")
        if spec.name in self.running:
            raise ChimeraError(
                "provisioning_failed",
                f"Cannot rewrite host configuration while '{spec.name}' is running.",
            )
        self.host_files.add(spec.name)
        self.apply_count += 1

    async def host_config_artifacts_present(self, spec: Any) -> bool:
        return spec.name in self.host_files

    async def is_running(self, spec: Any) -> bool:
        return spec.name in self.running

    async def start(self, spec: Any) -> None:
        self._maybe_fail("start")
        self.start_counts[spec.name] = self.start_counts.get(spec.name, 0) + 1
        self.running.add(spec.name)

    async def stop(self, spec: Any) -> None:
        self._maybe_fail("stop")
        self.running.discard(spec.name)

    async def restart(self, spec: Any) -> None:
        self._maybe_fail("restart")
        self.restart_counts[spec.name] = self.restart_counts.get(spec.name, 0) + 1
        self.running.add(spec.name)

    async def absent(self, spec: Any) -> None:
        self._maybe_fail("absent")
        self.materialized.pop(spec.name, None)
        self.provisioned_identity.pop(spec.name, None)
        self.host_files.discard(spec.name)
        self.running.discard(spec.name)

    async def _enable_service(self, name: str) -> None:
        return None

    async def _disable_service(self, name: str) -> None:
        return None

    async def list_unmanaged_host_resources(
        self, managed_names: set[str], catalog_image_names: set[str] | None = None
    ) -> dict[str, list[str]]:
        return {
            "machines": [],
            "storage_entries": [],
            "nspawn_configs": [],
            "systemd_overrides": [],
            "catalog_images": [],
        }

    def disappear(self, name: str) -> None:
        """Simulate the materialized filesystem vanishing without store changes."""
        self.materialized.pop(name, None)
        self.host_files.discard(name)
        self.running.discard(name)
