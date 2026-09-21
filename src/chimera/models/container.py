"""
Container specification models.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, PrivateAttr

from chimera.pydantic_compat import (
    AllowExtraModel,
    ForbidExtraModel,
    IgnoreExtraModel,
    PYDANTIC_V2,
    model_copy,
    model_dump,
    validated_field,
)

if PYDANTIC_V2:
    from pydantic import model_validator
else:
    from pydantic import root_validator

MACHINE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
ProvisioningState = Literal["unknown", "pending", "complete"]
_SAFE_RUNTIME_VALUE = re.compile(r"^[^\x00-\x1f\x7f-\x9f]+$")


def _safe_runtime_string(value: str, *, label: str) -> str:
    """Reject empty values and control characters in host configuration."""
    if not value or not _SAFE_RUNTIME_VALUE.fullmatch(value):
        raise ValueError(f"{label} must be non-empty and contain no control characters")
    return value


class BindMountSpec(ForbidExtraModel):
    """One host path exposed inside the container."""

    source: str
    destination: str
    read_only: bool = False
    options: str | None = None

    @validated_field("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        value = _safe_runtime_string(value, label="bind source")
        if not value.startswith("/"):
            raise ValueError("bind source must be an absolute host path")
        return value

    @validated_field("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        value = _safe_runtime_string(value, label="bind destination")
        if not value.startswith("/"):
            raise ValueError("bind destination must be an absolute guest path")
        return value

    @validated_field("options")
    @classmethod
    def validate_options(cls, value: str | None) -> str | None:
        return None if value is None else _safe_runtime_string(value, label="bind options")


class TmpfsMountSpec(ForbidExtraModel):
    """One native systemd-nspawn temporary filesystem."""

    destination: str
    options: str | None = None

    @validated_field("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        value = _safe_runtime_string(value, label="tmpfs destination")
        if not value.startswith("/"):
            raise ValueError("tmpfs destination must be an absolute guest path")
        return value

    @validated_field("options")
    @classmethod
    def validate_options(cls, value: str | None) -> str | None:
        return None if value is None else _safe_runtime_string(value, label="tmpfs options")


class PortForwardSpec(ForbidExtraModel):
    """One native systemd-nspawn host-to-container port forward."""

    protocol: Literal["tcp", "udp"] = "tcp"
    host_port: int = Field(..., ge=1, le=65535)
    container_port: int = Field(..., ge=1, le=65535)


class ResourceControlSpec(ForbidExtraModel):
    """Optional per-container systemd resource-control overrides."""

    memory_high: str | None = None
    memory_max: str | None = None
    memory_swap_max: str | None = None
    tasks_max: int | None = Field(None, gt=0)
    cpu_quota_percent: int | None = Field(None, gt=0)
    cpu_weight: int | None = Field(None, ge=1, le=10000)
    io_weight: int | None = Field(None, ge=1, le=10000)

    @validated_field("memory_high", "memory_max", "memory_swap_max")
    @classmethod
    def validate_memory_value(cls, value: str | None) -> str | None:
        return None if value is None else _safe_runtime_string(value, label="memory value")


class CloudInitSpec(AllowExtraModel):
    """Cloud-init specification."""

    meta_data: dict[str, Any] = Field(default_factory=dict)
    user_data: str | None = None
    network_config: str | None = None
    template: str | None = Field(None, description="Template name to use")


class ContainerSpec(IgnoreExtraModel):
    """Container specification."""

    name: str = Field(..., description="Container name")
    ensure: Literal["present", "absent"] = Field(default="present")
    state: Literal["running", "stopped"] = Field(default="running")
    image: str = Field(..., description="Canonical SimpleStreams product identity")
    image_source: str = Field(..., description="Configured SimpleStreams source name")
    image_artifact: Literal["rootfs", "disk"] = Field(
        default="rootfs",
        description="Local materialization kind: directory rootfs or raw disk",
    )
    profile: str = Field(default="standard", description="Profile name")
    cloud_init: CloudInitSpec | None = None
    autostart: bool = Field(default=True)
    bind_mounts: list[BindMountSpec] = Field(default_factory=list)
    tmpfs_mounts: list[TmpfsMountSpec] = Field(default_factory=list)
    port_forwards: list[PortForwardSpec] = Field(default_factory=list)
    resource_controls: ResourceControlSpec | None = None

    # Internal fields for resolved catalog objects; never persisted as identity.
    _effective_image: Any | None = PrivateAttr(default=None)
    _profile_spec: Any | None = PrivateAttr(default=None)

    @validated_field("name")
    @classmethod
    def validate_machine_name(cls, value: str) -> str:
        """Reject names unsafe for files, machinectl, and systemd unit instances."""
        if not MACHINE_NAME_PATTERN.fullmatch(value) or ".." in value:
            raise ValueError(
                "must use 1-63 letters, digits, dots, underscores, or hyphens; "
                "it cannot contain '..'"
            )
        from chimera.images.identity import require_unreserved_public_name

        return require_unreserved_public_name(value, kind="container")


class ContainerRecord(ForbidExtraModel):
    """Durable lifecycle intent for one CLI-managed container.

    provisioning_state is the single authority for creation-time initialization:
    unknown (explicitly adopted materialization), pending (a new materialization
    not initialized), or complete (initialization persisted for a specific
    materialization_id).
    """

    schema_version: Literal[1] = 1
    spec: ContainerSpec
    desired_state: Literal["running", "stopped"] = "stopped"
    deleting: bool = False
    last_error: str | None = None
    provisioning_state: ProvisioningState = "unknown"
    provisioning_fingerprint: str | None = None
    host_config_fingerprint: str | None = None
    materialization_id: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    if PYDANTIC_V2:

        @model_validator(mode="after")
        def validate_complete_materialization(self) -> ContainerRecord:
            """Require completed provisioning to identify its materialization."""
            if self.provisioning_state == "complete" and self.materialization_id is None:
                raise ValueError("complete provisioning requires a materialization_id")
            return self

    else:

        @root_validator(skip_on_failure=True)
        def validate_complete_materialization(
            cls: type[ContainerRecord], values: dict[str, Any]
        ) -> dict[str, Any]:
            """Require completed provisioning to identify its materialization."""
            if (
                values.get("provisioning_state") == "complete"
                and values.get("materialization_id") is None
            ):
                raise ValueError("complete provisioning requires a materialization_id")
            return values

    @property
    def name(self) -> str:
        """Return the managed container name."""
        return self.spec.name

    def runtime_spec(self) -> ContainerSpec:
        """Return an isolated spec copy for a provider operation."""
        return model_copy(self.spec, deep=True)

    def creation_identity(self) -> dict[str, Any]:
        """Return the operator-requested create/launch identity.

        Catalog expansion, generated hostnames, and proxy-rendered output are
        not part of this identity. Named cloud-init templates remain by name so
        identical retries compare equal even after the template body is merged.
        """
        return {
            "name": self.spec.name,
            "image": self.spec.image,
            "image_source": self.spec.image_source,
            "image_artifact": self.spec.image_artifact,
            "profile": self.spec.profile,
            "cloud_init": (
                model_dump(self.spec.cloud_init, exclude_none=True)
                if self.spec.cloud_init
                else None
            ),
            "bind_mounts": [model_dump(item) for item in self.spec.bind_mounts],
            "tmpfs_mounts": [model_dump(item) for item in self.spec.tmpfs_mounts],
            "port_forwards": [model_dump(item) for item in self.spec.port_forwards],
            "resource_controls": (
                model_dump(self.spec.resource_controls, exclude_none=True)
                if self.spec.resource_controls
                else None
            ),
        }


def stable_fingerprint(payload: Any) -> str:
    """Return a deterministic digest over JSON-safe provisioning inputs only."""
    _assert_canonical(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _assert_canonical(payload: Any) -> None:
    """Reject objects that would require default=str or non-deterministic encoding."""
    if payload is None or isinstance(payload, bool | int | str):
        return
    if isinstance(payload, float):
        if payload != payload or payload in {float("inf"), float("-inf")}:
            raise TypeError("fingerprint floats must be finite")
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if not isinstance(key, str):
                raise TypeError("fingerprint mapping keys must be strings")
            _assert_canonical(value)
        return
    if isinstance(payload, list):
        for item in payload:
            _assert_canonical(item)
        return
    raise TypeError(f"non-canonical fingerprint value: {type(payload)!r}")
