"""Transport-independent command service for the Chimera server.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from chimera.server.config import ConfigManager
from chimera.server.engine import StateEngine
from chimera.errors import ChimeraError
from chimera.models.container import (
    BindMountSpec,
    ContainerSpec,
    PortForwardSpec,
    ResourceControlSpec,
    TmpfsMountSpec,
)
from chimera.pydantic_compat import model_validate
from chimera.runtime import RuntimePaths

READ_COMMANDS = {"status", "list", "validate", "doctor"}
PRIVILEGED_COMMANDS = {
    "create",
    "launch",
    "spawn",
    "stop",
    "start",
    "restart",
    "delete",
    "remove",
    "exec",
    "reconcile",
    "reload",
    "image_pull",
    "import_records",
    "stream_exec",
    "stream_shell",
    "stream_logs",
    "stream_preflight",
}


@dataclass(frozen=True)
class CallerIdentity:
    """Transport-neutral caller identity established by ApiServer."""

    transport: Literal["unix", "tls"] = "unix"
    uid: int | None = None
    gid: int | None = None
    pid: int | None = None
    gids: tuple[int, ...] = ()
    cert_sha256: str | None = None
    cert_subject: str | None = None

    @property
    def is_root(self) -> bool:
        """Whether a Unix peer is the root user."""
        return self.transport == "unix" and self.uid == 0

    @property
    def is_remote_admin(self) -> bool:
        """Whether a verified client certificate is present."""
        return self.transport == "tls" and bool(self.cert_sha256)

    def audit_identity(self) -> str:
        """Return log fields without certificates, keys, or command payloads."""
        if self.transport == "tls":
            return f"transport=tls cert_sha256={self.cert_sha256} subject={self.cert_subject}"
        return f"transport=unix uid={self.uid} pid={self.pid}"


PeerCredentials = CallerIdentity


class CommandService:
    """Apply validated CLI/API commands to the state engine."""

    def __init__(
        self,
        state_engine: StateEngine,
        config_manager: ConfigManager,
        runtime_paths: RuntimePaths,
        admin_group_gid: int | None,
    ):
        self.state_engine = state_engine
        self.config_manager = config_manager
        self.runtime_paths = runtime_paths
        self.admin_group_gid = admin_group_gid

    async def execute(
        self,
        command: str | None,
        args: dict[str, Any] | None,
        peer: PeerCredentials,
    ) -> dict[str, Any]:
        """Authorize, validate, and execute one non-streaming command."""
        if not isinstance(command, str) or not command:
            raise ChimeraError(
                code="invalid_argument",
                message="A command is required.",
                status=400,
            )
        if command not in READ_COMMANDS | PRIVILEGED_COMMANDS:
            raise ChimeraError(
                code="invalid_argument",
                message=f"Unknown command '{command}'.",
                suggestion="Run 'chimeractl --help' to see supported commands.",
                status=400,
            )
        self.authorize(command, peer)
        values = args or {}

        if command == "status":
            return await self._status(values)
        if command == "list":
            return await self._list(values)
        if command in {"create", "launch", "spawn"}:
            return await self._create(values, start=command in {"launch", "spawn"})
        if command == "start":
            return await self._start(values)
        if command == "stop":
            return await self._stop(values)
        if command == "restart":
            return await self._restart(values)
        if command in {"delete", "remove"}:
            return await self._delete(values)
        if command == "exec":
            return await self._exec(values)
        if command == "stream_preflight":
            return await self._stream_preflight(values)
        if command == "reconcile":
            await self.state_engine.reconcile()
            return {"reconciled": True}
        if command == "reload":
            await self.state_engine.reload_configuration()
            return {"reloaded": True}
        if command == "image_pull":
            name = self._required_string(values, "name", "Image name")
            await self.state_engine.pull_image(name)
            return {"image": name, "pulled": True}
        if command == "validate":
            return await self.state_engine.validate_configuration()
        if command == "import_records":
            return await self._import_records(values)
        if command == "doctor":
            return await self._doctor()
        raise AssertionError(f"Unhandled command: {command}")

    def authorize(self, command: str, peer: PeerCredentials) -> None:
        """Enforce root or root-equivalent administrator access."""
        if peer.transport == "tls":
            if not peer.is_remote_admin:
                raise ChimeraError(
                    code="permission_denied",
                    message="This action requires a Chimera administrator.",
                    detail="The TLS client certificate was not verified.",
                    suggestion=(
                        "Present a client certificate signed by the configured Chimera "
                        "administrative CA."
                    ),
                    status=403,
                )
            return
        if command in READ_COMMANDS:
            return
        if peer.uid is None:
            raise ChimeraError(
                code="permission_denied",
                message="This action requires a Chimera administrator.",
                detail="Unix peer credentials could not be established.",
                suggestion="Connect through the local server socket as root or chimera-admin.",
                status=403,
            )
        is_group_admin = self.admin_group_gid is not None and self.admin_group_gid in peer.gids
        if peer.is_root or is_group_admin:
            return
        raise ChimeraError(
            code="permission_denied",
            message="This action requires a Chimera administrator.",
            detail="The connected user is not root or a member of chimera-admin.",
            suggestion=(
                "Ask a system administrator to add you to chimera-admin. "
                "That group is root-equivalent for container administration."
            ),
            status=403,
        )

    async def _status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return server-wide status or one container's status."""
        container = args.get("container")
        if container:
            return {
                "containers": {container: await self.state_engine.get_container_status(container)}
            }
        return {
            "server": {
                "running": True,
                "last_reconciliation": (
                    self.state_engine.last_reconciliation.isoformat()
                    if self.state_engine.last_reconciliation
                    else None
                ),
            },
            "containers": await self.state_engine.get_all_container_statuses(),
        }

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return one or all static/durable resource collections."""
        resource_type = args.get("type", "all")
        if resource_type not in {"all", "images", "containers", "profiles"}:
            raise ChimeraError(
                code="invalid_argument",
                message=f"Unknown resource type '{resource_type}'.",
                suggestion="Use images, containers, profiles, or all.",
                status=400,
            )
        result: dict[str, Any] = {}
        if resource_type in {"all", "images"}:
            result["images"] = {
                name: {
                    "name": name,
                    "type": spec.type,
                    "source": spec.source,
                    "verify": spec.verify,
                }
                for name, spec in self.config_manager.images.items()
            }
        if resource_type in {"all", "containers"}:
            result["containers"] = await self.state_engine.get_all_container_statuses()
        if resource_type in {"all", "profiles"}:
            result["profiles"] = {
                name: {
                    "name": name,
                    "description": spec.description or "",
                    "has_nspawn_config": bool(spec.nspawn_config_content),
                    "has_systemd_override": bool(spec.systemd_override_content),
                }
                for name, spec in self.config_manager.profiles.items()
            }
        return result

    async def _create(self, args: dict[str, Any], start: bool) -> dict[str, Any]:
        """Create or launch a new durable container record."""
        image = self._required_string(args, "image", "Image")
        name = self._required_string(args, "name", "Container name")
        profile = args.get("profile", "standard")
        cloud_init = args.get("cloud_init")
        if not isinstance(profile, str) or not profile:
            raise ChimeraError(
                code="invalid_argument",
                message="Profile must be a non-empty string.",
                status=400,
            )
        if cloud_init is not None and not isinstance(cloud_init, str):
            raise ChimeraError(
                code="invalid_argument",
                message="Cloud-init template must be a string.",
                status=400,
            )
        try:
            bind_mounts = [
                model_validate(BindMountSpec, item) for item in args.get("bind_mounts", [])
            ]
            tmpfs_mounts = [
                model_validate(TmpfsMountSpec, item) for item in args.get("tmpfs_mounts", [])
            ]
            port_forwards = [
                model_validate(PortForwardSpec, item) for item in args.get("port_forwards", [])
            ]
            raw_controls = args.get("resource_controls")
            resource_controls = (
                model_validate(ResourceControlSpec, raw_controls)
                if raw_controls is not None
                else None
            )
        except (ValidationError, TypeError) as error:
            raise ChimeraError(
                code="invalid_argument",
                message="The per-container runtime configuration is invalid.",
                detail=str(error),
                status=400,
            ) from error
        record = await self.state_engine.create_container(
            image=image,
            name=name,
            profile=profile,
            cloud_init_template=cloud_init,
            bind_mounts=bind_mounts,
            tmpfs_mounts=tmpfs_mounts,
            port_forwards=port_forwards,
            resource_controls=resource_controls,
            start=start,
        )
        return {
            "name": record.name,
            "image": record.spec.image,
            "desired_state": record.desired_state,
        }

    async def _start(self, args: dict[str, Any]) -> dict[str, Any]:
        """Start one existing managed container."""
        name = self._required_string(args, "name", "Container name")
        record = await self.state_engine.start_container(name)
        return {"name": record.name, "desired_state": record.desired_state}

    async def _stop(self, args: dict[str, Any]) -> dict[str, Any]:
        """Stop one existing managed container."""
        name = self._required_string(args, "name", "Container name")
        record = await self.state_engine.stop_container(name)
        return {"name": record.name, "desired_state": record.desired_state}

    async def _restart(self, args: dict[str, Any]) -> dict[str, Any]:
        """Restart one existing managed container."""
        name = self._required_string(args, "name", "Container name")
        record = await self.state_engine.restart_container(name)
        return {"name": record.name, "desired_state": record.desired_state}

    async def _delete(self, args: dict[str, Any]) -> dict[str, Any]:
        """Delete one existing managed container."""
        name = self._required_string(args, "name", "Container name")
        deleted = await self.state_engine.remove_container(name)
        return {"name": name, "deleted": deleted, "already_absent": not deleted}

    async def _exec(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run a batch command in a running managed container."""
        name = self._required_string(args, "name", "Container name")
        command = args.get("command")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise ChimeraError(
                code="invalid_argument",
                message="Command must be a list of arguments.",
                status=400,
            )
        return await self.state_engine.execute_in_container(name, command)

    async def _stream_preflight(self, args: dict[str, Any]) -> dict[str, Any]:
        """Preserve structured lifecycle errors before the WebSocket handshake."""
        name = self._required_string(args, "name", "Container name")
        operation = self._required_string(args, "operation", "Stream operation")
        if operation not in {"exec", "shell", "logs", "supervisor_logs"}:
            raise ChimeraError(
                code="invalid_argument",
                message="Stream operation must be exec, shell, logs, or supervisor_logs.",
                status=400,
            )
        if operation in {"logs", "supervisor_logs"}:
            await self.state_engine.validate_log_target(
                name, supervisor=operation == "supervisor_logs"
            )
        else:
            await self.state_engine.validate_stream_target(name)
        return {"name": name, "operation": operation, "ready": True}

    async def _import_records(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate and import client-parsed legacy node records."""
        source_records = args.get("records")
        if not isinstance(source_records, list):
            raise ChimeraError(
                code="invalid_argument",
                message="Import records must be a list.",
                status=400,
            )
        try:
            records = [model_validate(ContainerSpec, value) for value in source_records]
        except ValidationError as error:
            raise ChimeraError(
                code="invalid_configuration",
                message="The import file contains an invalid container record.",
                detail=str(error),
                suggestion="Correct the legacy node YAML and retry the import.",
                status=422,
            ) from error
        dry_run = args.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise ChimeraError(
                code="invalid_argument",
                message="dry_run must be a boolean.",
                status=400,
            )
        return await self.state_engine.import_records(records, dry_run=dry_run)

    async def _doctor(self) -> dict[str, Any]:
        """Return compact, machine-readable host readiness checks."""
        validation_error: str | None = None
        try:
            await self.state_engine.validate_configuration()
        except ChimeraError as error:
            validation_error = error.detail or error.message
        unmanaged_resources: dict[str, list[str]] = {
            "machines": [],
            "storage_entries": [],
            "nspawn_configs": [],
            "systemd_overrides": [],
        }
        machine_observation_error: str | None = None
        try:
            unmanaged_resources = await self.state_engine.get_unmanaged_host_resources()
        except ChimeraError as error:
            machine_observation_error = error.detail or error.message
        config = self.config_manager.config
        if config is None:
            raise ChimeraError(
                code="service_unavailable",
                message="The server configuration has not been initialized.",
                status=503,
            )
        return {
            "healthy": (
                validation_error is None
                and machine_observation_error is None
                and shutil.which("machinectl") is not None
            ),
            "checks": {
                "machinectl": shutil.which("machinectl") is not None,
                "catalog": {"valid": validation_error is None, "error": validation_error},
                "unmanaged_resources": unmanaged_resources,
                "unmanaged_machines": unmanaged_resources.get("machines", []),
                "machine_observation_error": machine_observation_error,
                "machines_directory": os.path.isdir(config.systemd.machines_dir),
            },
        }

    @staticmethod
    def _required_string(args: dict[str, Any], key: str, label: str) -> str:
        """Require a non-empty string argument."""
        value = args.get(key)
        if not isinstance(value, str) or not value:
            raise ChimeraError(
                code="invalid_argument",
                message=f"{label} is required.",
                status=400,
            )
        return value
