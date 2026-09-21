"""Durable container lifecycle and serialized reconciliation.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import ValidationError

from chimera.server.config import CatalogSnapshot, ConfigManager
from chimera.server.store import ContainerStore
from chimera.errors import ChimeraError
from chimera.models.config import ProxyConfig
from chimera.models.container import (
    BindMountSpec,
    CloudInitSpec,
    ContainerRecord,
    ContainerSpec,
    PortForwardSpec,
    ResourceControlSpec,
    TmpfsMountSpec,
    stable_fingerprint,
)
from chimera.images.identity import effective_from_source
from chimera.images.reference import (
    list_source_products,
    resolve_image_reference,
    resolve_current_artifact,
)
from chimera.models.image import ArtifactKind, DEFAULT_ARTIFACT_KIND
from chimera.providers import ProviderRegistry, ProviderStatus
from chimera.pydantic_compat import model_copy, model_dump, model_validate
from chimera.utils.rendering import (
    creation_render_payload,
    host_config_render_payload,
    image_nspawn_parameters,
)
from chimera.utils.templates import merge_dicts

logger = logging.getLogger(__name__)


class StateEngine:
    """Manage CLI-owned lifecycle intent over systemd-nspawn providers."""

    def __init__(
        self,
        config_manager: ConfigManager,
        provider_registry: ProviderRegistry,
        store: ContainerStore,
    ):
        self.config_manager = config_manager
        self.provider_registry = provider_registry
        self.store = store
        self.last_reconciliation: datetime | None = None
        self._mutation_lock = asyncio.Lock()

    async def reload_configuration(self) -> None:
        """Validate then atomically apply one complete reloadable catalog snapshot."""
        async with self._mutation_lock:
            snapshot = await self.config_manager.build_snapshot()
            await self._validate_snapshot(snapshot)
            self.config_manager.apply_snapshot(snapshot)

    async def reconcile(self) -> None:
        """Converge only durable managed records."""
        async with self._mutation_lock:
            started_at = datetime.now(UTC)
            logger.info("Starting state reconciliation")
            for record in self.store.records():
                try:
                    if record.deleting:
                        await self._delete_locked(record)
                    else:
                        await self._ensure_present_locked(record)
                        await self._apply_desired_state_locked(record)
                        await self._clear_converged_error_locked(record)
                except Exception as error:
                    logger.error("Failed to reconcile container %s: %s", record.name, error)
                    await self._record_error_locked(record, error)
            self.last_reconciliation = datetime.now(UTC)
            duration = (self.last_reconciliation - started_at).total_seconds()
            logger.info("State reconciliation completed in %.2fs", duration)

    async def create_container(
        self,
        *,
        image: str,
        name: str,
        profile: str = "standard",
        cloud_init_template: str | None = None,
        bind_mounts: list[BindMountSpec] | None = None,
        tmpfs_mounts: list[TmpfsMountSpec] | None = None,
        port_forwards: list[PortForwardSpec] | None = None,
        resource_controls: ResourceControlSpec | None = None,
        start: bool = False,
        image_source: str | None = None,
        image_artifact: ArtifactKind = DEFAULT_ARTIFACT_KIND,
    ) -> ContainerRecord:
        """Create or resume a durable stopped/running container intent."""
        async with self._mutation_lock:
            resolution = await resolve_image_reference(
                self.config_manager,
                image,
                explicit_source=image_source,
                artifact_kind=image_artifact,
            )
            requested = ContainerSpec(
                name=name,
                image=resolution.effective.canonical_image_id,
                image_source=resolution.effective.source_name,
                image_artifact=resolution.effective.artifact_kind,
                profile=profile,
                cloud_init=(
                    CloudInitSpec(template=cloud_init_template) if cloud_init_template else None
                ),
                bind_mounts=bind_mounts or [],
                tmpfs_mounts=tmpfs_mounts or [],
                port_forwards=port_forwards or [],
                resource_controls=resource_controls,
            )
            desired_state: Literal["running", "stopped"] = "running" if start else "stopped"
            action = "launch" if start else "create"
            if self.store.contains(name):
                return await self._resume_create_locked(requested, desired_state, action)

            await self._preflight_new_container_locked(requested)
            record = ContainerRecord(
                spec=requested,
                desired_state=desired_state,
                provisioning_state="pending",
            )
            self.store.add(record)
            try:
                await self._ensure_present_locked(record)
                await self._apply_desired_state_locked(record)
                await self._clear_converged_error_locked(record)
                return self.store.get(name)
            except Exception as error:
                await self._record_error_locked(record, error)
                raise self._host_error(action, name, error) from error

    async def _resume_create_locked(
        self, spec: ContainerSpec, desired_state: str, action: str
    ) -> ContainerRecord:
        """Resume an identical lost-response create/launch without changing intent."""
        record = self.store.get(spec.name)
        if record.deleting:
            raise ChimeraError(
                code="conflict",
                message=f"Container '{spec.name}' is being deleted.",
                suggestion=f"Wait for deletion or retry 'chimeractl delete {spec.name}'.",
                status=409,
            )
        if record.creation_identity() != ContainerRecord(spec=spec).creation_identity():
            raise ChimeraError(
                code="conflict",
                message=f"Container '{spec.name}' already has a different creation specification.",
                suggestion=f"Inspect it with 'chimeractl info {spec.name}'.",
                status=409,
            )
        if record.desired_state != desired_state:
            next_action = "start" if desired_state == "running" else "stop"
            raise ChimeraError(
                code="conflict",
                message=f"Container '{spec.name}' already has {record.desired_state} intent.",
                suggestion=f"Use 'chimeractl {next_action} {spec.name}' for that transition.",
                status=409,
            )
        try:
            await self._ensure_present_locked(record)
            await self._apply_desired_state_locked(record)
            await self._clear_converged_error_locked(record)
            return self.store.get(spec.name)
        except Exception as error:
            await self._record_error_locked(record, error)
            raise self._host_error(action, spec.name, error) from error

    async def _preflight_new_container_locked(self, spec: ContainerSpec) -> None:
        """Validate a detached copy before a new request can become durable intent."""
        for mount in spec.bind_mounts:
            if not await asyncio.to_thread(os.path.exists, mount.source):
                raise ChimeraError(
                    code="invalid_argument",
                    message=f"Bind source '{mount.source}' does not exist on the Chimera server.",
                    suggestion="Create the host path on the server or correct --bind/--bind-ro.",
                    status=422,
                )
        working = model_copy(spec, deep=True)
        self._attach_catalog(working, require=True)
        self._reject_unsupported_disk_provisioning(working)
        self._enrich_cloud_init_spec(working)
        image = working._effective_image
        profile = working._profile_spec
        image_provider = self._required_provider("image")
        profile_provider = self._required_provider("profile")
        container_provider = self._required_provider("container")
        await image_provider.validate_spec(image)
        if not await profile_provider.validate_spec(profile):
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Profile '{spec.profile}' is invalid.",
                suggestion="Correct the profile catalog entry and reload the server.",
                status=422,
            )
        if not await container_provider.validate_spec(working):
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Container '{spec.name}' references an invalid image or profile.",
                suggestion="Run 'chimeractl config validate' and correct the catalog entry.",
                status=422,
            )
        await container_provider.preflight_create(working)

    async def start_container(self, name: str) -> ContainerRecord:
        """Persist running intent before ensuring and starting a container."""
        async with self._mutation_lock:
            record = self.store.get(name)
            if record.deleting:
                raise ChimeraError(
                    code="conflict",
                    message=f"Container '{name}' is being deleted.",
                    suggestion="Wait for deletion to finish or retry 'chimeractl delete' after resolving its error.",
                    status=409,
                )
            record.desired_state = "running"
            record.updated_at = self.store.now()
            self.store.replace(record)
            try:
                await self._ensure_present_locked(record)
                await self._apply_desired_state_locked(record)
                await self._clear_converged_error_locked(record)
                return self.store.get(name)
            except Exception as error:
                await self._record_error_locked(record, error)
                raise self._host_error("start", name, error) from error

    async def stop_container(self, name: str) -> ContainerRecord:
        """Persist stopped intent before stopping a materialized container."""
        async with self._mutation_lock:
            record = self.store.get(name)
            if record.deleting:
                raise ChimeraError(
                    code="conflict",
                    message=f"Container '{name}' is being deleted.",
                    status=409,
                )
            record.desired_state = "stopped"
            record.updated_at = self.store.now()
            self.store.replace(record)
            try:
                await self._apply_desired_state_locked(record)
                await self._clear_converged_error_locked(record)
                return self.store.get(name)
            except Exception as error:
                await self._record_error_locked(record, error)
                raise self._host_error("stop", name, error) from error

    async def restart_container(self, name: str) -> ContainerRecord:
        """Restart for real, applying pending host configuration only after preflight."""
        async with self._mutation_lock:
            record = self.store.get(name)
            if record.deleting:
                raise ChimeraError(
                    code="conflict",
                    message=f"Container '{name}' is being deleted.",
                    status=409,
                )
            spec, container_provider = self._lifecycle_context(record)
            status, _identity = await self._inspect(container_provider, spec)
            running = False
            if status == ProviderStatus.ERROR:
                raise self._observation_error(name)
            if status == ProviderStatus.PRESENT:
                running = await container_provider.is_running(spec)

            if record.provisioning_state == "pending" and running:
                raise ChimeraError(
                    code="provisioning_incomplete",
                    message=(
                        f"Container '{name}' is running but creation-time provisioning is still "
                        "pending. Chimera will not stop it to initialize the root filesystem."
                    ),
                    suggestion="Inspect 'chimeractl info' and recreate the container if initialization is required.",
                    status=409,
                )

            host_pending = False
            resolved = None
            host_fp = None
            try:
                resolved = self._resolve_for_provisioning(spec)
                host_fp = self._host_config_fingerprint(resolved)
                artifacts_ok = True
                if status == ProviderStatus.PRESENT:
                    artifacts_ok = await self._host_artifacts_ok(container_provider, resolved)
                # A matching fingerprint is not proof that .nspawn/override files still exist.
                host_pending = (
                    not self._host_fingerprint_matches(record.host_config_fingerprint, resolved)
                    or not artifacts_ok
                )
            except ChimeraError:
                resolved = None
                host_fp = None

            if record.provisioning_state == "unknown" and running and host_pending:
                # Host-config restart is allowed; unknown history still forbids rootfs writes.
                pass

            record.desired_state = "running"
            record.updated_at = self.store.now()
            self.store.replace(record)
            try:
                if not running:
                    await self._ensure_present_locked(record, operator_restart=True)
                    await self._apply_desired_state_locked(record)
                    await self._clear_converged_error_locked(record)
                    return self.store.get(name)

                if host_pending and resolved is not None:
                    await container_provider.stop(spec)
                    await container_provider.apply_host_config(resolved)
                    current = self.store.get(name)
                    current.host_config_fingerprint = host_fp
                    current.updated_at = self.store.now()
                    self.store.replace(current)
                    if not await self._host_artifacts_ok(container_provider, resolved):
                        raise ChimeraError(
                            code="provisioning_failed",
                            message=f"Host configuration artifacts for '{name}' are missing after apply.",
                            suggestion="Inspect nspawn and systemd override paths, then retry restart.",
                            status=502,
                        )

                current = self.store.get(name)
                if current.provisioning_state == "pending":
                    raise ChimeraError(
                        code="provisioning_incomplete",
                        message=f"Container '{name}' is not fully provisioned and will not be started.",
                        suggestion="Retry create/launch after inspecting 'chimeractl info'.",
                        status=409,
                    )
                await container_provider._enable_service(spec.name)
                if host_pending and resolved is not None:
                    await container_provider.start(spec)
                else:
                    await container_provider.restart(spec)
                await self._clear_converged_error_locked(record)
                return self.store.get(name)
            except Exception as error:
                await self._record_error_locked(record, error)
                raise self._host_error("restart", name, error) from error

    async def remove_container(self, name: str) -> bool:
        """Mark a record deleting, retry cleanup, or report an absent record safely."""
        async with self._mutation_lock:
            if not self.store.contains(name):
                return False
            record = self.store.get(name)
            if not record.deleting:
                record.deleting = True
                record.updated_at = self.store.now()
                self.store.replace(record)
            try:
                await self._delete_locked(record)
                return True
            except Exception as error:
                await self._record_error_locked(record, error)
                raise self._host_error("delete", name, error) from error

    async def pull_image(
        self,
        name: str,
        image_source: str | None = None,
        image_artifact: ArtifactKind = DEFAULT_ARTIFACT_KIND,
    ) -> dict[str, Any]:
        """Pull one resolved SimpleStreams image into the local cache."""
        async with self._mutation_lock:
            resolution = await resolve_image_reference(
                self.config_manager,
                name,
                explicit_source=image_source,
                artifact_kind=image_artifact,
            )
            local_name = resolution.effective.local_image_name
            image_provider = self._required_provider("image")
            try:
                await image_provider.validate_spec(resolution.effective)
                await image_provider.present(resolution.effective)
            except Exception as error:
                raise self._host_error("pull image", name, error) from error
            return {
                "image": resolution.effective.canonical_image_id,
                "image_source": resolution.effective.source_name,
                "artifact_kind": resolution.effective.artifact_kind,
                "requested": name,
                "local_image_name": local_name,
                "pulled": True,
            }

    async def import_records(
        self, specs: list[ContainerSpec], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Validate and atomically import explicitly requested legacy ownership."""
        async with self._mutation_lock:
            incoming: list[ContainerRecord] = []
            already_imported: list[str] = []
            adopting: list[str] = []
            pending: list[str] = []
            for spec in specs:
                self._validate_legacy_import_spec(spec)
                await self._preflight_import_semantics_locked(spec)
                container_provider = self._required_provider("container")
                artifacts = await container_provider.find_host_artifacts(spec)
                provisioning_state: Literal["unknown", "pending"] = (
                    "unknown" if artifacts else "pending"
                )
                record = ContainerRecord(
                    spec=model_copy(spec, deep=True),
                    desired_state=spec.state,
                    provisioning_state=provisioning_state,
                )
                if self.store.contains(record.name):
                    current = self.store.get(record.name)
                    if (
                        current.creation_identity() == record.creation_identity()
                        and current.desired_state == record.desired_state
                        and not current.deleting
                    ):
                        already_imported.append(record.name)
                        continue
                    raise ChimeraError(
                        code="conflict",
                        message=f"Container '{record.name}' is already managed differently.",
                        suggestion=f"Inspect it with 'chimeractl info {record.name}'.",
                        status=409,
                    )
                if artifacts:
                    adopting.append(record.name)
                else:
                    pending.append(record.name)
                incoming.append(record)
            if not dry_run and incoming:
                self.store.import_records(incoming)
            return {
                "validated": len(specs),
                "imported": 0 if dry_run else len(incoming),
                "already_imported": already_imported,
                "adopting": adopting,
                "pending_materialization": pending,
                "dry_run": dry_run,
            }

    def _validate_legacy_import_spec(self, spec: ContainerSpec) -> None:
        """Allow only legacy state/autostart combinations with a direct mapping."""
        if spec.ensure == "absent":
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Legacy container '{spec.name}' has ensure=absent and cannot be imported.",
                suggestion="Remove it from the migration input; migration never performs implicit deletion.",
                status=422,
            )
        if (spec.state == "running") != spec.autostart:
            raise ChimeraError(
                code="invalid_configuration",
                message=(
                    f"Legacy container '{spec.name}' has incompatible state/autostart values."
                ),
                detail=f"state={spec.state}, autostart={spec.autostart}",
                suggestion=(
                    "Import only running+autostart=true or stopped+autostart=false records, "
                    "then set the desired lifecycle state explicitly."
                ),
                status=422,
            )

    async def _preflight_import_semantics_locked(self, spec: ContainerSpec) -> None:
        """Apply create-equivalent catalog semantics but permit explicit adoption."""
        working = model_copy(spec, deep=True)
        self._attach_catalog(working, require=True)
        self._reject_unsupported_disk_provisioning(working)
        self._enrich_cloud_init_spec(working)
        image_provider = self._required_provider("image")
        profile_provider = self._required_provider("profile")
        container_provider = self._required_provider("container")
        await image_provider.validate_spec(working._effective_image)
        if not await profile_provider.validate_spec(working._profile_spec):
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Profile '{spec.profile}' is invalid.",
                status=422,
            )
        if not await container_provider.validate_spec(working):
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Container '{spec.name}' references an invalid image or profile.",
                status=422,
            )

    async def get_container_status(self, name: str) -> dict[str, Any]:
        """Return desired and observed state for one managed container."""
        record = self.store.get(name)
        return await self._status_for_record(record)

    async def get_all_container_statuses(self) -> dict[str, dict[str, Any]]:
        """Return desired and observed state for all managed containers."""
        statuses: dict[str, dict[str, Any]] = {}
        for record in self.store.records():
            statuses[record.name] = await self._status_for_record(record)
        return statuses

    async def get_unmanaged_host_resources(self) -> dict[str, list[str]]:
        """Diagnose unmanaged host artifacts without claiming or changing them."""
        container_provider = self._required_provider("container")
        managed_names = {record.name for record in self.store.records()}
        resources = await container_provider.list_unmanaged_host_resources(managed_names)
        if not isinstance(resources, dict):
            raise ChimeraError(
                code="host_observation_failed",
                message="The container provider returned invalid diagnostic data.",
                status=503,
            )
        normalized: dict[str, list[str]] = {}
        for key, value in resources.items():
            if not isinstance(key, str) or not isinstance(value, list):
                continue
            names = [item for item in value if isinstance(item, str)]
            normalized[key] = names
        normalized.setdefault("image_caches", [])
        return normalized

    async def get_unmanaged_machine_names(self) -> list[str]:
        """Diagnose listed host machines without claiming or changing them."""
        resources = await self.get_unmanaged_host_resources()
        return list(resources.get("machines", []))

    async def execute_in_container(self, name: str, command: list[str]) -> dict[str, Any]:
        """Run a noninteractive command only in an existing running container."""
        _record, spec, container_provider = await self.validate_stream_target(name)
        result = await container_provider.execute(spec, command)
        if not isinstance(result, dict):
            raise ChimeraError(
                code="host_operation_failed",
                message=f"Container '{name}' returned an invalid command result.",
                status=502,
            )
        return result

    async def validate_stream_target(self, name: str) -> tuple[ContainerRecord, ContainerSpec, Any]:
        """Validate a managed, present, running target for terminal streaming."""
        record = self.store.get(name)
        spec, container_provider = self._lifecycle_context(record)
        status, _identity = await self._inspect(container_provider, spec)
        if status == ProviderStatus.ERROR:
            raise self._observation_error(name)
        if status == ProviderStatus.ABSENT:
            raise ChimeraError(
                code="container_absent",
                message=f"Container '{name}' is absent from the host.",
                suggestion=f"Start it with: chimeractl start {name}",
                status=409,
            )
        if not await container_provider.is_running(spec):
            raise ChimeraError(
                code="container_stopped",
                message=f"Container '{name}' exists but is stopped.",
                suggestion=f"Start it with: chimeractl start {name}",
                status=409,
            )
        return record, spec, container_provider

    async def validate_log_target(
        self, name: str, *, supervisor: bool
    ) -> tuple[ContainerRecord, ContainerSpec, Any]:
        """Validate managed log access, requiring a running machine for guest logs."""
        record = self.store.get(name)
        spec, container_provider = self._lifecycle_context(record)
        if supervisor:
            return record, spec, container_provider
        return await self.validate_stream_target(name)

    async def validate_configuration(self) -> dict[str, Any]:
        """Validate a candidate catalog without modifying the active snapshot."""
        async with self._mutation_lock:
            snapshot = await self.config_manager.build_snapshot()
            return await self._validate_snapshot(snapshot)

    async def _validate_snapshot(self, snapshot: CatalogSnapshot) -> dict[str, Any]:
        """Perform semantic catalog validation without applying its values."""
        image_provider = self._required_provider("image")
        profile_provider = self._required_provider("profile")
        errors: list[str] = []
        product_policies = 0
        for source in snapshot.image_sources.values():
            for product, policy in source.products.items():
                product_policies += 1
                try:
                    await image_provider.validate_spec(
                        effective_from_source(source, product, policy, "rootfs")
                    )
                except ChimeraError as error:
                    errors.append(
                        f"invalid product policy '{source.name}/{product}': {error.message}"
                    )
        for name, profile in snapshot.profiles.items():
            if not await profile_provider.validate_spec(profile):
                errors.append(f"invalid profile '{name}'")
        for name, template in snapshot.cloud_init_templates.items():
            try:
                model_validate(CloudInitSpec, template)
            except (ValidationError, TypeError, ValueError) as error:
                errors.append(f"invalid cloud-init template '{name}': {error}")
        if errors:
            raise ChimeraError(
                code="invalid_configuration",
                message="Chimera catalog validation failed: " + "; ".join(errors),
                detail="; ".join(errors),
                suggestion="Correct the named catalog entry, then retry validation.",
                status=422,
            )
        return {
            "valid": True,
            "image_sources": len(snapshot.image_sources),
            "product_policies": product_policies,
            "profiles": len(snapshot.profiles),
            "cloud_init_templates": len(snapshot.cloud_init_templates),
        }

    async def describe_image(
        self,
        reference: str,
        image_source: str | None = None,
        image_artifact: ArtifactKind = DEFAULT_ARTIFACT_KIND,
    ) -> dict[str, Any]:
        """Resolve an image reference and report configured, source, and local state."""
        resolution = await resolve_image_reference(
            self.config_manager,
            reference,
            explicit_source=image_source,
            artifact_kind=image_artifact,
        )
        effective = resolution.effective
        image_provider = self._required_provider("image")
        local = await image_provider.inspect_local(effective)
        artifact = await resolve_current_artifact(self.config_manager, resolution)
        return {
            "requested": reference,
            "source": effective.source_name,
            "canonical_image_id": effective.canonical_image_id,
            "artifact_kind": effective.artifact_kind,
            "artifacts": list(resolution.artifact_kinds),
            "metadata_verify": effective.source_spec.metadata_verify,
            "keyring": effective.source_spec.keyring,
            "aliases": list(resolution.aliases),
            "release": resolution.release,
            "version": resolution.version,
            "variant": resolution.variant,
            "architecture": resolution.architecture,
            "custom_files": [
                {"path": item.path, "ensure": item.ensure, "target": item.target}
                for item in effective.custom_files
            ],
            "nspawn_parameters": list(effective.nspawn_parameters),
            "local": local,
            "remote": {
                "serial": artifact.serial,
                "url": artifact.url,
                "sha256": artifact.sha256,
                "size": artifact.size,
                "product": artifact.product,
                "architecture": artifact.architecture,
                "aliases": list(artifact.aliases),
                "release": artifact.release,
                "version": artifact.version,
                "variant": artifact.variant,
                "ftype": artifact.ftype,
                "artifact_kind": artifact.artifact_kind,
            },
        }

    async def list_source_images(self, source_name: str) -> list[dict[str, Any]]:
        """List discoverable products for one configured source."""
        return await list_source_products(self.config_manager, source_name)

    def _lifecycle_context(self, record: ContainerRecord) -> tuple[ContainerSpec, Any]:
        """Build a detached spec for stop/delete/inspect without expanding templates."""
        spec = record.runtime_spec()
        self._attach_catalog(spec, require=False)
        return spec, self._required_provider("container")

    def _attach_catalog(self, spec: ContainerSpec, *, require: bool) -> None:
        """Resolve image/profile references onto a detached spec copy without network access."""
        spec._profile_spec = self.config_manager.get_profile_spec(spec.profile)
        source = self.config_manager.get_image_source_spec(spec.image_source)
        if source is None:
            spec._effective_image = None
        else:
            policy = self.config_manager.get_product_policy(spec.image_source, spec.image)
            spec._effective_image = effective_from_source(
                source, spec.image, policy, spec.image_artifact
            )
        if require and spec._effective_image is None:
            raise ChimeraError(
                code="not_found",
                message=f"Image '{spec.image}' is not available from source '{spec.image_source}'.",
                suggestion="Run 'chimeractl image source list' or correct image_source on the container.",
                status=404,
            )
        if require and spec._profile_spec is None:
            raise ChimeraError(
                code="not_found",
                message=f"Profile '{spec.profile}' is not in the Chimera catalog.",
                suggestion="Run 'chimeractl profile list' to see available profiles.",
                status=404,
            )

    def _resolve_for_provisioning(self, spec: ContainerSpec) -> ContainerSpec:
        """Return a detached copy with catalogs and cloud-init templates expanded."""
        working = model_copy(spec, deep=True)
        self._attach_catalog(working, require=True)
        self._reject_unsupported_disk_provisioning(working)
        self._enrich_cloud_init_spec(working)
        return working

    def _enrich_cloud_init_spec(self, spec: ContainerSpec) -> None:
        """Merge a named cloud-init template with per-container overrides on a copy."""
        if not spec.cloud_init or not spec.cloud_init.template:
            return
        template_name = spec.cloud_init.template
        template_data = self.config_manager.cloud_init_templates.get(template_name)
        if template_data is None:
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Cloud-init template '{template_name}' does not exist.",
                suggestion="Correct the container record or add the template to the catalog.",
                status=422,
            )
        merged_data = merge_dicts(template_data, model_dump(spec.cloud_init, exclude_none=True))
        merged_data.pop("template", None)
        spec.cloud_init = CloudInitSpec(**merged_data)

    async def _ensure_present_locked(
        self, record: ContainerRecord, *, operator_restart: bool = False
    ) -> None:
        """Materialize and provision according to unknown/pending/complete semantics."""
        spec, container_provider = self._lifecycle_context(record)
        status, identity = await self._inspect(container_provider, spec)
        if status == ProviderStatus.ERROR:
            raise self._observation_error(record.name)
        running = False
        if status == ProviderStatus.PRESENT:
            running = await container_provider.is_running(spec)

        current = self.store.get(record.name)
        if current.provisioning_state == "unknown":
            if status == ProviderStatus.PRESENT:
                await self._apply_host_config_if_safe(
                    current, spec, container_provider, running, operator_restart
                )
                return
            current.provisioning_state = "pending"
            current.materialization_id = None
            current.provisioning_fingerprint = None
            current.host_config_fingerprint = None
            current.updated_at = self.store.now()
            self.store.replace(current)
            current = self.store.get(record.name)
            status = ProviderStatus.ABSENT
            identity = None
            running = False

        if current.provisioning_state == "complete":
            if status == ProviderStatus.ABSENT:
                current.provisioning_state = "pending"
                current.materialization_id = None
                current.provisioning_fingerprint = None
                current.host_config_fingerprint = None
                current.updated_at = self.store.now()
                self.store.replace(current)
                current = self.store.get(record.name)
                running = False
            elif identity is None or identity != current.materialization_id:
                raise ChimeraError(
                    code="conflict",
                    message=(
                        f"Container '{record.name}' has a different materialization than the "
                        "one Chimera initialized."
                    ),
                    suggestion="Delete the unmanaged replacement or recreate the managed container.",
                    status=409,
                )
            else:
                await self._converge_complete_materialization(
                    current, spec, container_provider, running, operator_restart
                )
                return

        if running:
            current.last_error = (
                "Creation-time provisioning is not recorded for this running container. "
                "Chimera will not rewrite its root filesystem in place."
            )
            current.updated_at = self.store.now()
            self.store.replace(current)
            return

        resolved = self._resolve_for_provisioning(spec)
        if not await container_provider.validate_spec(resolved):
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Container '{record.name}' references an invalid image or profile.",
                suggestion="Run 'chimeractl config validate' and correct the catalog entry.",
                status=422,
            )
        if status == ProviderStatus.ABSENT:
            await self._ensure_image_for_clone(resolved)
            await container_provider.present(resolved)
            status, identity = await self._inspect(container_provider, spec)
            if status != ProviderStatus.PRESENT or identity is None:
                raise ChimeraError(
                    code="host_operation_failed",
                    message=f"Container '{record.name}' was not present after materialization.",
                    suggestion="Inspect machinectl and the server journal, then retry.",
                    status=502,
                )

        await container_provider.provision_rootfs(resolved)
        creation_fp = self._creation_fingerprint(resolved)
        current = self.store.get(record.name)
        current.provisioning_state = "complete"
        current.materialization_id = identity
        current.provisioning_fingerprint = creation_fp
        current.updated_at = self.store.now()
        self.store.replace(current)

        await container_provider.apply_host_config(resolved)
        if not await self._host_artifacts_ok(container_provider, resolved):
            raise ChimeraError(
                code="provisioning_failed",
                message=f"Host configuration artifacts for '{record.name}' are missing after apply.",
                status=502,
            )
        current = self.store.get(record.name)
        current.host_config_fingerprint = self._host_config_fingerprint(resolved)
        current.updated_at = self.store.now()
        self.store.replace(current)

    async def _converge_complete_materialization(
        self,
        record: ContainerRecord,
        spec: ContainerSpec,
        container_provider: Any,
        running: bool,
        operator_restart: bool,
    ) -> None:
        """Do not replay initialization; report drift and repair host files when safe."""
        try:
            resolved = self._resolve_for_provisioning(spec)
        except ChimeraError:
            await self._apply_host_config_if_safe(
                record, spec, container_provider, running, operator_restart
            )
            return
        current = self.store.get(record.name)
        if current.provisioning_fingerprint is not None and not self._creation_fingerprint_matches(
            current.provisioning_fingerprint, resolved
        ):
            current.last_error = (
                f"Creation-time provisioning for '{record.name}' differs from the recorded "
                "definition. Recreate the container to apply catalog or cloud-init changes."
            )
            current.updated_at = self.store.now()
            self.store.replace(current)
            # Creation drift is visible but does not authorize rootfs writes, and it
            # must not skip unrelated repair of missing or outdated host files.
        await self._apply_host_config_if_safe(
            current, spec, container_provider, running, operator_restart, resolved=resolved
        )

    async def _apply_host_config_if_safe(
        self,
        record: ContainerRecord,
        spec: ContainerSpec,
        container_provider: Any,
        running: bool,
        operator_restart: bool,
        resolved: ContainerSpec | None = None,
    ) -> None:
        """Apply pending or missing host configuration only while stopped or restarting."""
        try:
            resolved = resolved or self._resolve_for_provisioning(spec)
        except ChimeraError:
            return
        host_fp = self._host_config_fingerprint(resolved)
        artifacts_ok = await self._host_artifacts_ok(container_provider, resolved)
        current = self.store.get(record.name)
        if (
            self._host_fingerprint_matches(current.host_config_fingerprint, resolved)
            and artifacts_ok
        ):
            return
        if running and not operator_restart:
            return
        if running and operator_restart:
            await container_provider.stop(spec)
        await container_provider.apply_host_config(resolved)
        if not await self._host_artifacts_ok(container_provider, resolved):
            raise ChimeraError(
                code="provisioning_failed",
                message=f"Host configuration artifacts for '{record.name}' are missing after apply.",
                status=502,
            )
        current = self.store.get(record.name)
        current.host_config_fingerprint = host_fp
        current.updated_at = self.store.now()
        self.store.replace(current)

    async def _ensure_image_for_clone(self, spec: ContainerSpec) -> None:
        """Require the same completed read-only cache contract as image pull."""
        image_provider = self._required_provider("image")
        await image_provider.validate_spec(spec._effective_image)
        await image_provider.present(spec._effective_image)

    async def _apply_desired_state_locked(self, record: ContainerRecord) -> None:
        """Apply the durable running/stopped target to the host system."""
        spec, container_provider = self._lifecycle_context(record)
        status, _identity = await self._inspect(container_provider, spec)
        if status == ProviderStatus.ABSENT:
            if record.desired_state == "running":
                await self._ensure_present_locked(record)
                spec, container_provider = self._lifecycle_context(record)
            else:
                return
        elif status == ProviderStatus.ERROR:
            raise self._observation_error(record.name)

        current = self.store.get(record.name)
        if record.desired_state == "running":
            if current.provisioning_state == "pending":
                running = await container_provider.is_running(spec)
                if running:
                    return
                raise ChimeraError(
                    code="provisioning_incomplete",
                    message=f"Container '{record.name}' is not fully provisioned and will not be started.",
                    suggestion="Inspect 'chimeractl info' and retry create/launch after the error is resolved.",
                    status=409,
                )
            if current.provisioning_state == "unknown":
                await container_provider._enable_service(spec.name)
                await container_provider.start(spec)
                return
            await container_provider._enable_service(spec.name)
            await container_provider.start(spec)
        else:
            await container_provider.stop(spec)
            await container_provider._disable_service(spec.name)

    async def _delete_locked(self, record: ContainerRecord) -> None:
        """Remove a deleting record's resources, then remove its durable intent."""
        spec, container_provider = self._lifecycle_context(record)
        await container_provider.absent(spec)
        self.store.remove(record.name)

    async def _status_for_record(self, record: ContainerRecord) -> dict[str, Any]:
        """Combine durable intent with live machinectl/systemd state."""
        spec, container_provider = self._lifecycle_context(record)
        host_fp: str | None = None
        render_ok = True
        resolved: ContainerSpec | None = None
        try:
            resolved = self._resolve_for_provisioning(spec)
            host_fp = self._host_config_fingerprint(resolved)
        except ChimeraError:
            render_ok = False
            resolved = None
        status, _identity = await self._inspect(container_provider, spec)
        provisioning_drift: bool | None = None
        if (
            record.provisioning_state == "complete"
            and record.provisioning_fingerprint
            and render_ok
            and resolved is not None
        ):
            provisioning_drift = not self._creation_fingerprint_matches(
                record.provisioning_fingerprint, resolved
            )
        elif not render_ok and record.provisioning_state == "complete":
            provisioning_drift = None
        else:
            provisioning_drift = False if record.provisioning_state != "complete" else None

        if status == ProviderStatus.ERROR:
            return self._status_payload(
                record,
                exists=None,
                running=None,
                observed_state="error",
                provisioning_drift=provisioning_drift,
                host_config_pending=None,
                host_config_missing=None,
                next_action="Check machinectl, systemd, and the Chimera server journal, then retry.",
            )
        exists = status == ProviderStatus.PRESENT
        try:
            running = exists and await container_provider.is_running(spec)
        except ChimeraError:
            running = None
        observed_state = (
            "running"
            if running
            else "stopped" if exists and running is False else "absent" if not exists else "unknown"
        )
        host_pending: bool | None
        host_missing: bool | None
        if not render_ok or host_fp is None:
            host_pending = None
            host_missing = None
        else:
            assert resolved is not None
            artifacts_ok = True
            if exists:
                artifacts_ok = await self._host_artifacts_ok(container_provider, spec)
            host_missing = exists and not artifacts_ok
            host_pending = bool(
                not self._host_fingerprint_matches(record.host_config_fingerprint, resolved)
                or host_missing
            )
        return self._status_payload(
            record,
            exists=exists,
            running=running,
            observed_state=observed_state,
            provisioning_drift=provisioning_drift,
            host_config_pending=host_pending,
            host_config_missing=host_missing,
            next_action=self._next_action(
                record, observed_state, provisioning_drift, host_pending, running
            ),
        )

    def _status_payload(
        self,
        record: ContainerRecord,
        *,
        exists: bool | None,
        running: bool | None,
        observed_state: str,
        provisioning_drift: bool | None,
        host_config_pending: bool | None,
        host_config_missing: bool | None,
        next_action: str,
    ) -> dict[str, Any]:
        """Stable structured fields for human and JSON info output."""
        return {
            "name": record.name,
            "exists": exists,
            "running": running,
            "observed_state": observed_state,
            "desired_state": record.desired_state,
            "deleting": record.deleting,
            "image": record.spec.image,
            "image_source": record.spec.image_source,
            "image_artifact": record.spec.image_artifact,
            "profile": record.spec.profile,
            "bind_mounts": [model_dump(item) for item in record.spec.bind_mounts],
            "tmpfs_mounts": [model_dump(item) for item in record.spec.tmpfs_mounts],
            "port_forwards": [model_dump(item) for item in record.spec.port_forwards],
            "resource_controls": (
                model_dump(record.spec.resource_controls, exclude_none=True)
                if record.spec.resource_controls
                else None
            ),
            "last_error": record.last_error,
            "provisioning_state": record.provisioning_state,
            "provisioning_drift": provisioning_drift,
            "materialization_bound": record.provisioning_state == "complete",
            "host_config_pending": host_config_pending,
            "host_config_missing": host_config_missing,
            "next_action": next_action,
            "updated_at": record.updated_at.isoformat(),
        }

    @staticmethod
    def _next_action(
        record: ContainerRecord,
        observed_state: str,
        provisioning_drift: bool | None,
        host_config_pending: bool | None,
        running: bool | None,
    ) -> str:
        """Suggest the operator's next safe command without recommending doctor recursively."""
        if record.deleting:
            return f"Retry 'chimeractl delete {record.name}' after inspecting the last error."
        if record.last_error and provisioning_drift:
            return f"Recreate '{record.name}' to apply creation-time catalog changes."
        if record.provisioning_state == "pending" and observed_state != "running":
            return f"Retry 'chimeractl start {record.name}' after inspecting the last error."
        if record.provisioning_state == "unknown" and running:
            return "Provisioning history is unknown; lifecycle commands use the existing materialization."
        if host_config_pending and running:
            return f"Restart '{record.name}' to apply pending host configuration."
        if record.desired_state == "running" and observed_state != "running":
            return f"Start with: chimeractl start {record.name}"
        if record.desired_state == "stopped" and observed_state == "running":
            return f"Stop with: chimeractl stop {record.name}"
        if record.last_error:
            return "Inspect the last error, then retry the failed operation."
        return "No action required."

    async def _clear_converged_error_locked(self, record: ContainerRecord) -> None:
        """Clear last_error only after requested provisioning has fully succeeded."""
        current = self.store.get(record.name)
        if current.provisioning_state == "pending":
            return
        if current.provisioning_state == "complete":
            spec, container_provider = self._lifecycle_context(current)
            try:
                resolved = self._resolve_for_provisioning(spec)
            except ChimeraError:
                return
            if not self._creation_fingerprint_matches(current.provisioning_fingerprint, resolved):
                return
            if not self._host_fingerprint_matches(current.host_config_fingerprint, resolved):
                return
            if not await self._host_artifacts_ok(container_provider, resolved):
                return
        await self._clear_error_locked(current)

    def _creation_fingerprint(self, spec: ContainerSpec) -> str:
        """Fingerprint the rendered creation-time output that will actually be applied."""
        return stable_fingerprint(self._creation_payload(spec))

    def _host_config_fingerprint(self, spec: ContainerSpec) -> str:
        """Fingerprint the rendered nspawn/override files that will actually be applied."""
        return stable_fingerprint(self._host_payload(spec))

    def _creation_fingerprint_matches(self, stored: str | None, spec: ContainerSpec) -> bool:
        """Compare a stored creation fingerprint with the current rendered output."""
        return stored == self._creation_fingerprint(spec)

    def _host_fingerprint_matches(self, stored: str | None, spec: ContainerSpec) -> bool:
        """Compare a stored host fingerprint with the current rendered output."""
        return stored == self._host_config_fingerprint(spec)

    def _creation_payload(self, spec: ContainerSpec) -> dict[str, Any]:
        """Return the applied creation-time contract used for fingerprints."""
        return creation_render_payload(
            container_name=spec.name,
            image=spec._effective_image,
            cloud_init=spec.cloud_init,
            proxy=self._proxy(),
        )

    def _host_payload(self, spec: ContainerSpec) -> dict[str, Any]:
        """Return the applied host-config contract used for fingerprints."""
        return host_config_render_payload(
            container_name=spec.name,
            profile=spec._profile_spec,
            proxy=self._proxy(),
            extra_parameters=image_nspawn_parameters(spec._effective_image),
            bind_mounts=spec.bind_mounts,
            tmpfs_mounts=spec.tmpfs_mounts,
            port_forwards=spec.port_forwards,
            resource_controls=spec.resource_controls,
        )

    def _reject_unsupported_disk_provisioning(self, spec: ContainerSpec) -> None:
        """Reject disk materialization that would require root-filesystem mutation."""
        image = spec._effective_image
        if image is None or image.artifact_kind != "disk":
            return
        if spec.cloud_init is not None:
            raise ChimeraError(
                code="unsupported_provisioning",
                message="Disk artifacts do not support cloud-init provisioning.",
                suggestion="Launch with --artifact rootfs, or omit --cloud-init.",
                status=422,
            )
        if image.custom_files:
            raise ChimeraError(
                code="unsupported_provisioning",
                message=(
                    f"Disk artifact for '{image.canonical_image_id}' cannot apply "
                    "product-policy custom_files."
                ),
                suggestion="Use --artifact rootfs, or remove custom_files from that product policy.",
                status=422,
            )

    def _proxy(self) -> ProxyConfig | None:
        """Return the active proxy snapshot used by rendering."""
        config = getattr(self.config_manager, "config", None)
        proxy = getattr(config, "proxy", None) if config is not None else None
        if isinstance(proxy, ProxyConfig):
            return proxy
        return None

    def _required_provider(self, name: str) -> Any:
        """Return an initialized provider or raise a service failure."""
        provider = self.provider_registry.get_provider(name)
        if provider is None:
            raise ChimeraError(
                code="service_unavailable",
                message=f"The '{name}' provider is unavailable.",
                suggestion="Check the Chimera server journal and restart the service.",
                status=503,
            )
        return provider

    async def _inspect(
        self, container_provider: Any, spec: ContainerSpec
    ) -> tuple[Any, str | None]:
        """Return materialization status and identity from the container provider."""
        inspected = await container_provider.inspect_materialization(spec)
        status, identity = inspected
        if identity is not None and not isinstance(identity, str):
            return ProviderStatus.ERROR, None
        return status, identity

    async def _host_artifacts_ok(self, container_provider: Any, spec: ContainerSpec) -> bool:
        """True when required .nspawn/override files still exist."""
        return bool(await container_provider.host_config_artifacts_present(spec))

    async def _clear_error_locked(self, record: ContainerRecord) -> None:
        """Clear a prior operation error after a successful convergence."""
        current = self.store.get(record.name)
        if current.last_error:
            current.last_error = None
            current.updated_at = self.store.now()
            self.store.replace(current)

    async def _record_error_locked(self, record: ContainerRecord, error: Exception) -> None:
        """Persist the useful cause for operator diagnosis and retry."""
        try:
            current = self.store.get(record.name)
        except ChimeraError:
            return
        current.last_error = str(error)
        current.updated_at = self.store.now()
        self.store.replace(current)

    @staticmethod
    def _observation_error(name: str) -> ChimeraError:
        """Stable failure when a container observation cannot be trusted."""
        return ChimeraError(
            code="host_observation_failed",
            message=f"Could not determine whether container '{name}' exists.",
            suggestion="Check machinectl and the Chimera server journal, then retry.",
            status=503,
        )

    @staticmethod
    def _host_error(action: str, name: str, error: Exception) -> ChimeraError:
        """Wrap provider failures in stable, actionable CLI/API semantics."""
        if isinstance(error, ChimeraError):
            return error
        return ChimeraError(
            code="host_operation_failed",
            message=f"Could not {action} container '{name}'.",
            detail=str(error),
            suggestion=f"Run 'chimeractl info {name}' or inspect the Chimera server journal.",
            status=502,
        )
