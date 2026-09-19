"""Canonical rendering used by both fingerprinting and host/rootfs writes.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import io
import os
import stat
from typing import Any

from ruamel.yaml import YAML

from chimera.models.config import ProxyConfig
from chimera.models.container import (
    BindMountSpec,
    CloudInitSpec,
    PortForwardSpec,
    ResourceControlSpec,
    TmpfsMountSpec,
)
from chimera.models.image import ImageSpec
from chimera.models.profile import ProfileSpec
from chimera.pydantic_compat import model_dump
from chimera.utils.templates import render_template


def proxy_context(proxy: ProxyConfig | None) -> dict[str, str | None]:
    """Return JSON-safe proxy fields consumed by templates."""
    if proxy is None:
        return {"http_proxy": None, "https_proxy": None, "no_proxy": None}
    return {
        "http_proxy": proxy.http_proxy,
        "https_proxy": proxy.https_proxy,
        "no_proxy": proxy.no_proxy,
    }


def render_user_data(user_data: str | None, proxy: ProxyConfig | None) -> str | None:
    """Render cloud-init user-data with the same proxy context applied to disk."""
    if not user_data:
        return None
    return render_template(user_data, proxy=proxy)


def rendered_meta_data(container_name: str, cloud_init: CloudInitSpec) -> dict[str, Any]:
    """Return generated cloud-init meta-data including hostname defaults."""
    meta_data = dict(cloud_init.meta_data) if cloud_init.meta_data else {}
    meta_data["local-hostname"] = container_name
    meta_data.setdefault("instance-id", f"iid-{container_name}")
    return meta_data


def dump_yaml_text(payload: dict[str, Any]) -> str:
    """Serialize mapping YAML deterministically for writes and fingerprints."""
    stream = io.StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.dump(payload, stream)
    return stream.getvalue()


_PARAMETERS_PREFIX = "Parameters="
_RESOLVCONF_PREFIX = "ResolvConf="
_RESOLVED_UPLINK = "/run/systemd/resolve/resolv.conf"


def host_nspawn_resolvconf() -> str:
    """Select nspawn ResolvConf= from the host resolver layout."""
    try:
        mode = os.lstat(_RESOLVED_UPLINK).st_mode
    except OSError:
        return "replace-host"
    if not stat.S_ISREG(mode) or not os.access(_RESOLVED_UPLINK, os.R_OK):
        return "replace-host"
    return "replace-uplink"


def _exec_directive_key(stripped: str) -> str | None:
    if not stripped or stripped[0] in "#;" or "=" not in stripped:
        return None
    if stripped.startswith("[") and stripped.endswith("]"):
        return None
    return stripped.split("=", 1)[0].strip()


def ensure_nspawn_exec_assignment(content: str, assignment: str) -> str:
    """Insert an [Exec] assignment unless that key is already present."""
    if "=" not in assignment:
        return content
    key, value = assignment.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return content
    lines = content.splitlines(keepends=True) if content else []
    exec_header: int | None = None
    last_exec_line: int | None = None
    has_key = False
    in_exec = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_exec = stripped.lower() == "[exec]"
            if in_exec and exec_header is None:
                exec_header = index
            continue
        if in_exec:
            if stripped:
                last_exec_line = index
            if _exec_directive_key(stripped) == key:
                has_key = True
    if has_key:
        return content

    new_line = f"{key}={value}\n"
    if last_exec_line is not None:
        insert_at = last_exec_line + 1
        if not lines[last_exec_line].endswith("\n"):
            lines[last_exec_line] = lines[last_exec_line] + "\n"
        lines.insert(insert_at, new_line)
        return "".join(lines)
    if exec_header is not None:
        lines.insert(exec_header + 1, new_line)
        return "".join(lines)
    prefix = "[Exec]\n" + new_line
    if not content:
        return prefix
    if not content.endswith("\n"):
        return prefix + "\n" + content
    return prefix + content


def image_nspawn_parameters(image: Any | None) -> list[str]:
    """Return extra kernel command-line tokens declared on a catalog image."""
    if image is None:
        return []
    values = getattr(image, "nspawn_parameters", None)
    if not values:
        return []
    return list(values)


def compose_nspawn_exec_parameters(content: str, extra_parameters: list[str] | None = None) -> str:
    """Merge extra kernel command-line tokens into the [Exec] Parameters= value."""
    extras: list[str] = []
    seen: set[str] = set()
    for token in extra_parameters or []:
        if not token or token in seen:
            continue
        extras.append(token)
        seen.add(token)
    if not extras:
        return content

    if not content:
        return "[Exec]\n" + _PARAMETERS_PREFIX + " ".join(extras) + "\n"

    lines = content.splitlines(keepends=True)
    exec_header: int | None = None
    parameters_line: int | None = None
    last_exec_line: int | None = None
    in_exec = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_exec = stripped.lower() == "[exec]"
            if in_exec and exec_header is None:
                exec_header = index
            continue
        if in_exec:
            if stripped:
                last_exec_line = index
            if stripped.startswith(_PARAMETERS_PREFIX) and parameters_line is None:
                parameters_line = index

    if parameters_line is not None:
        line = lines[parameters_line]
        if line.endswith("\r\n"):
            newline = "\r\n"
            body = line[:-2]
        elif line.endswith("\n"):
            newline = "\n"
            body = line[:-1]
        else:
            newline = ""
            body = line
        leading_len = len(body) - len(body.lstrip(" \t"))
        leading = body[:leading_len]
        current = body.strip()[len(_PARAMETERS_PREFIX) :].split()
        merged = list(current)
        present = set(current)
        for token in extras:
            if token not in present:
                merged.append(token)
                present.add(token)
        lines[parameters_line] = f"{leading}{_PARAMETERS_PREFIX}{' '.join(merged)}{newline}"
        return "".join(lines)

    new_line = _PARAMETERS_PREFIX + " ".join(extras) + "\n"
    if last_exec_line is not None:
        insert_at = last_exec_line + 1
        if not lines[last_exec_line].endswith("\n"):
            lines[last_exec_line] = lines[last_exec_line] + "\n"
        lines.insert(insert_at, new_line)
        return "".join(lines)
    if exec_header is not None:
        lines.insert(exec_header + 1, new_line)
        return "".join(lines)
    prefix = "[Exec]\n" + new_line
    if not content.endswith("\n"):
        return prefix + "\n" + content
    return prefix + content


def _append_nspawn_runtime(
    content: str,
    bind_mounts: list[BindMountSpec] | None,
    tmpfs_mounts: list[TmpfsMountSpec] | None,
    port_forwards: list[PortForwardSpec] | None,
) -> str:
    """Append deterministic per-container nspawn assignments."""
    files: list[str] = []
    for mount in bind_mounts or []:
        key = "BindReadOnly" if mount.read_only else "Bind"
        value = f"{_escape_nspawn_path(mount.source)}:{_escape_nspawn_path(mount.destination)}"
        if mount.options:
            value += f":{mount.options}"
        files.append(f"{key}={value}")
    for tmpfs_mount in tmpfs_mounts or []:
        value = _escape_nspawn_path(tmpfs_mount.destination)
        if tmpfs_mount.options:
            value += f":{tmpfs_mount.options}"
        files.append(f"TemporaryFileSystem={value}")
    network = [
        f"Port={forward.protocol}:{forward.host_port}:{forward.container_port}"
        for forward in port_forwards or []
    ]
    additions: list[str] = []
    if files:
        additions.extend(["[Files]", *files])
    if network:
        additions.extend(["[Network]", *network])
    if not additions:
        return content
    prefix = content
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + "\n".join(additions) + "\n"


def _escape_nspawn_path(value: str) -> str:
    """Escape path separators using systemd-nspawn's bind syntax."""
    return value.replace("\\", "\\\\").replace(":", "\\:")


def render_nspawn_config(
    profile: ProfileSpec,
    container_name: str,
    proxy: ProxyConfig | None,
    extra_parameters: list[str] | None = None,
    bind_mounts: list[BindMountSpec] | None = None,
    tmpfs_mounts: list[TmpfsMountSpec] | None = None,
    port_forwards: list[PortForwardSpec] | None = None,
) -> str:
    """Render the .nspawn file that apply_host_config will write."""
    rendered = render_template(
        profile.nspawn_config_content,
        container_name=container_name,
        proxy=proxy,
    )
    rendered = ensure_nspawn_exec_assignment(
        rendered, f"{_RESOLVCONF_PREFIX}{host_nspawn_resolvconf()}"
    )
    rendered = compose_nspawn_exec_parameters(rendered, extra_parameters)
    return _append_nspawn_runtime(rendered, bind_mounts, tmpfs_mounts, port_forwards)


def render_systemd_override(
    profile: ProfileSpec,
    container_name: str,
    resource_controls: ResourceControlSpec | None = None,
) -> str:
    """Render the systemd override file that apply_host_config will write."""
    rendered = render_template(profile.systemd_override_content, container_name=container_name)
    if resource_controls is None:
        return rendered
    assignments = [
        ("MemoryHigh", resource_controls.memory_high),
        ("MemoryMax", resource_controls.memory_max),
        ("MemorySwapMax", resource_controls.memory_swap_max),
        ("TasksMax", resource_controls.tasks_max),
        (
            "CPUQuota",
            (
                f"{resource_controls.cpu_quota_percent}%"
                if resource_controls.cpu_quota_percent is not None
                else None
            ),
        ),
        ("CPUWeight", resource_controls.cpu_weight),
        ("IOWeight", resource_controls.io_weight),
    ]
    lines = [f"{key}={value}" for key, value in assignments if value is not None]
    if not lines:
        return rendered
    if rendered and not rendered.endswith("\n"):
        rendered += "\n"
    return rendered + "[Service]\n" + "\n".join(lines) + "\n"


def custom_file_contract(image: ImageSpec | None) -> list[dict[str, str | None]]:
    """Capture path, mode-equivalent ensure, and link target for creation identity."""
    if image is None:
        return []
    entries: list[dict[str, str | None]] = []
    for item in image.custom_files:
        entries.append(
            {
                "path": item.path,
                "ensure": item.ensure,
                "target": item.target,
            }
        )
    return entries


def creation_render_payload(
    *,
    container_name: str,
    image: ImageSpec | None,
    cloud_init: CloudInitSpec | None,
    proxy: ProxyConfig | None,
) -> dict[str, Any]:
    """Inputs whose rendered output is applied during creation-time provisioning."""
    files: dict[str, str] = {}
    if cloud_init is not None:
        meta = rendered_meta_data(container_name, cloud_init)
        files["var/lib/cloud/seed/nocloud/meta-data"] = dump_yaml_text(meta)
        user_data = render_user_data(cloud_init.user_data, proxy)
        if user_data is not None:
            files["var/lib/cloud/seed/nocloud/user-data"] = user_data
        if cloud_init.network_config:
            files["var/lib/cloud/seed/nocloud/network-config"] = cloud_init.network_config
        else:
            files["etc/cloud/cloud.cfg.d/99-disable-network-config.cfg"] = (
                "network: {config: disabled}\n"
            )
    # Proxy values that templates actually consume already appear in `files`.
    # Unused proxy settings are not part of the applied contract.
    return {
        "image_type": None if image is None else image.type,
        "custom_files": custom_file_contract(image),
        "files": files,
    }


def host_config_render_payload(
    *,
    container_name: str,
    profile: ProfileSpec | None,
    proxy: ProxyConfig | None,
    extra_parameters: list[str] | None = None,
    bind_mounts: list[BindMountSpec] | None = None,
    tmpfs_mounts: list[TmpfsMountSpec] | None = None,
    port_forwards: list[PortForwardSpec] | None = None,
    resource_controls: ResourceControlSpec | None = None,
) -> dict[str, Any]:
    """Inputs whose rendered output is applied as host configuration."""
    nspawn = None
    override = None
    if profile is not None and (
        profile.nspawn_config_content or bind_mounts or tmpfs_mounts or port_forwards
    ):
        nspawn = {
            "path": f"{container_name}.nspawn",
            "mode": 0o644,
            "content": render_nspawn_config(
                profile,
                container_name,
                proxy,
                extra_parameters,
                bind_mounts,
                tmpfs_mounts,
                port_forwards,
            ),
        }
    if profile is not None and (profile.systemd_override_content or resource_controls is not None):
        override = {
            "path": f"systemd-nspawn@{container_name}.service.d/override.conf",
            "mode": 0o644,
            "content": render_systemd_override(profile, container_name, resource_controls),
        }
    # Proxy values consumed by the nspawn template are already in `content`.
    return {"nspawn": nspawn, "override": override}


def requested_cloud_init_identity(cloud_init: CloudInitSpec | None) -> dict[str, Any] | None:
    """Preserve the operator-requested template name and explicit overrides."""
    if cloud_init is None:
        return None
    return model_dump(cloud_init, exclude_none=True)
