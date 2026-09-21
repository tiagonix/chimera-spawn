"""The discoverable operator CLI for Chimera Spawn.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional, cast

import typer
from pydantic import ValidationError
from rich.console import Console
from ruamel.yaml import YAML

from chimera.cli.client import ChimeraClient, ClientError
from chimera.cli.commands import (
    print_doctor,
    print_image_info,
    print_info,
    print_json,
    print_resources,
    print_success,
    stream_logs,
    stream_terminal,
)
from chimera.cli.diagnostics import (
    OutputFormat,
    diagnose_client_error,
    local_doctor,
    remote_doctor,
    render_error,
)
from chimera.models.container import (
    BindMountSpec,
    ContainerSpec,
    PortForwardSpec,
    ResourceControlSpec,
    TmpfsMountSpec,
)
from chimera.pydantic_compat import model_dump, model_validate

app = typer.Typer(
    name="chimeractl",
    help=(
        "Manage systemd-nspawn containers through the Chimera server.\n\n"
        "Normal workflow: image list --source SOURCE, image pull IMAGE, launch IMAGE NAME, "
        "info NAME, stop NAME, restart NAME, delete NAME.\n\n"
        "Use 'chimeractl doctor' when the server or host is unavailable."
    ),
    add_completion=False,
    no_args_is_help=True,
)
image_app = typer.Typer(help="Discover and pull container images.", no_args_is_help=True)
image_source_app = typer.Typer(
    help="Inspect administrator-configured image sources.", no_args_is_help=True
)
profile_app = typer.Typer(help="List reusable container profiles.", no_args_is_help=True)
config_app = typer.Typer(help="Validate and import configuration.", no_args_is_help=True)
server_app = typer.Typer(help="Inspect or reload the Chimera server.", no_args_is_help=True)
app.add_typer(image_app, name="image")
image_app.add_typer(image_source_app, name="source")
app.add_typer(profile_app, name="profile")
app.add_typer(config_app, name="config")
app.add_typer(server_app, name="server")

console = Console()
error_console = Console(stderr=True)


def _format(value: str) -> OutputFormat:
    """Validate a response format consistently across every command."""
    if value not in {"table", "json"}:
        raise typer.BadParameter("must be 'table' or 'json'")
    return cast(OutputFormat, value)


def _split_escaped_colons(value: str, maxsplit: int) -> list[str]:
    """Split nspawn syntax while decoding escaped literal colons in paths."""
    parts = [""]
    splits = 0
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in {":", "\\"}:
            parts[-1] += value[index + 1]
            index += 2
            continue
        if char == ":" and splits < maxsplit:
            parts.append("")
            splits += 1
        else:
            parts[-1] += char
        index += 1
    return parts


def _parse_bind(value: str, *, read_only: bool) -> BindMountSpec:
    parts = _split_escaped_colons(value, 2)
    if len(parts) < 2:
        raise typer.BadParameter("bind must use SOURCE:DEST[:OPTIONS]")
    options = parts[2] if len(parts) == 3 else None
    try:
        return BindMountSpec(
            source=parts[0],
            destination=parts[1],
            read_only=read_only,
            options=options,
        )
    except ValidationError as error:
        raise typer.BadParameter(str(error)) from error


def _parse_tmpfs(value: str) -> TmpfsMountSpec:
    parts = _split_escaped_colons(value, 1)
    try:
        return TmpfsMountSpec(
            destination=parts[0],
            options=parts[1] if len(parts) == 2 else None,
        )
    except ValidationError as error:
        raise typer.BadParameter(str(error)) from error


def _parse_publish(value: str) -> PortForwardSpec:
    parts = value.split(":")
    protocol = "tcp"
    try:
        if len(parts) == 1:
            host_port = container_port = int(parts[0])
        elif len(parts) == 2:
            host_port, container_port = (int(item) for item in parts)
        elif len(parts) == 3:
            protocol = parts[0]
            host_port, container_port = (int(item) for item in parts[1:])
        else:
            raise ValueError("publish must use PORT, HOST:CONTAINER, or PROTO:HOST:CONTAINER")
        return PortForwardSpec(
            protocol=cast(Any, protocol),
            host_port=host_port,
            container_port=container_port,
        )
    except (ValidationError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


def _runtime_payload(
    *,
    bind: list[str] | None,
    bind_ro: list[str] | None,
    tmpfs: list[str] | None,
    publish: list[str] | None,
    memory_high: str | None,
    memory_max: str | None,
    memory_swap_max: str | None,
    tasks_max: int | None,
    cpu_quota: int | None,
    cpu_weight: int | None,
    io_weight: int | None,
) -> dict[str, Any]:
    """Normalize repeatable CLI declarations into durable structured intent."""
    bind_mounts = [
        *(_parse_bind(value, read_only=False) for value in bind or []),
        *(_parse_bind(value, read_only=True) for value in bind_ro or []),
    ]
    try:
        controls = ResourceControlSpec(
            memory_high=memory_high,
            memory_max=memory_max,
            memory_swap_max=memory_swap_max,
            tasks_max=tasks_max,
            cpu_quota_percent=cpu_quota,
            cpu_weight=cpu_weight,
            io_weight=io_weight,
        )
    except ValidationError as error:
        raise typer.BadParameter(str(error)) from error
    controls_payload = model_dump(controls, exclude_none=True)
    return {
        "bind_mounts": [model_dump(item) for item in bind_mounts],
        "tmpfs_mounts": [model_dump(_parse_tmpfs(value)) for value in tmpfs or []],
        "port_forwards": [model_dump(_parse_publish(value)) for value in publish or []],
        "resource_controls": controls_payload or None,
    }


def _common_socket() -> Any:
    """Return the command-local local-server socket option."""
    return typer.Option(None, "--socket", "-s", help="Server Unix socket path.")


def _common_host() -> Any:
    """Remote TLS endpoint; mutually exclusive with --socket."""
    return typer.Option(
        None,
        "--host",
        "-H",
        help="Remote server host or host:port (default port 8080).",
    )


def _common_tls_ca() -> Any:
    """CA that signed the remote server certificate."""
    return typer.Option(None, "--tls-ca", help="CA that signed the remote server certificate.")


def _common_tls_cert() -> Any:
    """Client certificate presented to the remote server."""
    return typer.Option(
        None, "--tls-cert", help="Client certificate presented to the remote server."
    )


def _common_tls_key() -> Any:
    """Private key for the client certificate."""
    return typer.Option(None, "--tls-key", help="PEM private key for the client certificate.")


def _common_source() -> Any:
    """Command-local configured SimpleStreams source name."""
    return typer.Option(
        None,
        "--source",
        help="Configured SimpleStreams source name.",
    )


def _common_artifact() -> Any:
    """Command-local materialization kind for a resolved SimpleStreams product."""
    return typer.Option(
        "rootfs",
        "--artifact",
        help="Desired local materialization kind: rootfs (directory) or disk (raw image).",
    )


def _make_client(
    *,
    socket: str | None,
    host: str | None,
    tls_ca: str | None,
    tls_cert: str | None,
    tls_key: str | None,
    timeout: float,
) -> ChimeraClient:
    """Construct a local or remote client, converting setup errors to ClientError."""
    return ChimeraClient(
        socket_path=socket,
        timeout=timeout,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
    )


def _request(
    command: str,
    args: dict[str, Any],
    *,
    socket: str | None,
    host: str | None,
    tls_ca: str | None,
    tls_cert: str | None,
    tls_key: str | None,
    timeout: float | None,
    default_timeout: float,
    output_format: OutputFormat,
) -> dict[str, Any]:
    """Call the server and render known failures consistently."""
    try:
        client = _make_client(
            socket=socket,
            host=host,
            tls_ca=tls_ca,
            tls_cert=tls_cert,
            tls_key=tls_key,
            timeout=timeout or default_timeout,
        )
        return client.request(command, args, timeout=timeout)
    except ClientError as error:
        _exit_with_error(error, output_format)
    raise AssertionError("unreachable")


def _exit_with_error(error: ClientError, output_format: OutputFormat) -> None:
    """Send human diagnostics to stderr and JSON errors to stdout."""
    rendered = render_error(error, output_format)
    if output_format == "json":
        print(rendered)
    else:
        error_console.print(rendered)
    raise typer.Exit(1)


@app.command("list")
def list_command(
    resource_type: str = typer.Argument("all", help="image_sources, containers, profiles, or all"),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """List image sources, profiles, and managed containers."""
    output_format = _format(output_format)
    result = _request(
        "list",
        {"type": resource_type},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=10,
        output_format=output_format,
    )
    print_resources(result, output_format)


def _create_or_launch(
    command: str,
    image: str,
    name: str,
    profile: str,
    cloud_init: str | None,
    bind: list[str] | None,
    bind_ro: list[str] | None,
    tmpfs: list[str] | None,
    publish: list[str] | None,
    memory_high: str | None,
    memory_max: str | None,
    memory_swap_max: str | None,
    tasks_max: int | None,
    cpu_quota: int | None,
    cpu_weight: int | None,
    io_weight: int | None,
    image_source: str | None,
    image_artifact: str,
    output_format: str,
    socket: str | None,
    host: str | None,
    tls_ca: str | None,
    tls_cert: str | None,
    tls_key: str | None,
    timeout: float | None,
) -> None:
    """Share the create and launch argument/response contract."""
    output_format = _format(output_format)
    runtime = _runtime_payload(
        bind=bind,
        bind_ro=bind_ro,
        tmpfs=tmpfs,
        publish=publish,
        memory_high=memory_high,
        memory_max=memory_max,
        memory_swap_max=memory_swap_max,
        tasks_max=tasks_max,
        cpu_quota=cpu_quota,
        cpu_weight=cpu_weight,
        io_weight=io_weight,
    )
    payload: dict[str, Any] = {
        "image": image,
        "name": name,
        "profile": profile,
        "cloud_init": cloud_init,
        **runtime,
    }
    if image_source is not None:
        payload["image_source"] = image_source
    payload["image_artifact"] = image_artifact
    result = _request(
        command,
        payload,
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=600,
        output_format=output_format,
    )
    action = "Launched" if command in {"launch", "spawn"} else "Created"
    state = result["desired_state"]
    shown = result.get("image", image)
    print_success(f"{action} {name} from {shown} ({state}).", result, output_format)


@app.command("create")
def create_command(
    image: str = typer.Argument(..., metavar="IMAGE", help="Source-published image reference"),
    name: str = typer.Argument(..., metavar="NAME", help="New container name"),
    profile: str = typer.Option("standard", "--profile", help="Catalog profile to apply"),
    cloud_init: Optional[str] = typer.Option(None, "--cloud-init", help="Cloud-init template"),
    bind: Optional[List[str]] = typer.Option(None, "--bind", help="Writable SOURCE:DEST[:OPTIONS]"),
    bind_ro: Optional[List[str]] = typer.Option(
        None, "--bind-ro", help="Read-only SOURCE:DEST[:OPTIONS]"
    ),
    tmpfs: Optional[List[str]] = typer.Option(None, "--tmpfs", help="Tmpfs DEST[:OPTIONS]"),
    publish: Optional[List[str]] = typer.Option(None, "--publish", help="Publish a guest port"),
    memory_high: Optional[str] = typer.Option(None, "--memory-high", help="Systemd MemoryHigh"),
    memory_max: Optional[str] = typer.Option(None, "--memory-max", help="Systemd MemoryMax"),
    memory_swap_max: Optional[str] = typer.Option(
        None, "--memory-swap-max", help="Systemd MemorySwapMax"
    ),
    tasks_max: Optional[int] = typer.Option(None, "--tasks-max", min=1),
    cpu_quota: Optional[int] = typer.Option(None, "--cpu-quota", min=1, help="CPU quota percent"),
    cpu_weight: Optional[int] = typer.Option(None, "--cpu-weight", min=1, max=10000),
    io_weight: Optional[int] = typer.Option(None, "--io-weight", min=1, max=10000),
    source: Optional[str] = _common_source(),
    artifact: str = _common_artifact(),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Create a stopped container: create IMAGE NAME."""
    _create_or_launch(
        "create",
        image,
        name,
        profile,
        cloud_init,
        bind,
        bind_ro,
        tmpfs,
        publish,
        memory_high,
        memory_max,
        memory_swap_max,
        tasks_max,
        cpu_quota,
        cpu_weight,
        io_weight,
        source,
        artifact,
        output_format,
        socket,
        host,
        tls_ca,
        tls_cert,
        tls_key,
        timeout,
    )


@app.command("launch")
def launch_command(
    image: str = typer.Argument(..., metavar="IMAGE", help="Source-published image reference"),
    name: str = typer.Argument(..., metavar="NAME", help="New container name"),
    profile: str = typer.Option("standard", "--profile", help="Catalog profile to apply"),
    cloud_init: Optional[str] = typer.Option(None, "--cloud-init", help="Cloud-init template"),
    bind: Optional[List[str]] = typer.Option(None, "--bind", help="Writable SOURCE:DEST[:OPTIONS]"),
    bind_ro: Optional[List[str]] = typer.Option(
        None, "--bind-ro", help="Read-only SOURCE:DEST[:OPTIONS]"
    ),
    tmpfs: Optional[List[str]] = typer.Option(None, "--tmpfs", help="Tmpfs DEST[:OPTIONS]"),
    publish: Optional[List[str]] = typer.Option(None, "--publish", help="Publish a guest port"),
    memory_high: Optional[str] = typer.Option(None, "--memory-high", help="Systemd MemoryHigh"),
    memory_max: Optional[str] = typer.Option(None, "--memory-max", help="Systemd MemoryMax"),
    memory_swap_max: Optional[str] = typer.Option(
        None, "--memory-swap-max", help="Systemd MemorySwapMax"
    ),
    tasks_max: Optional[int] = typer.Option(None, "--tasks-max", min=1),
    cpu_quota: Optional[int] = typer.Option(None, "--cpu-quota", min=1, help="CPU quota percent"),
    cpu_weight: Optional[int] = typer.Option(None, "--cpu-weight", min=1, max=10000),
    io_weight: Optional[int] = typer.Option(None, "--io-weight", min=1, max=10000),
    source: Optional[str] = _common_source(),
    artifact: str = _common_artifact(),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Create and start a container: launch IMAGE NAME."""
    _create_or_launch(
        "launch",
        image,
        name,
        profile,
        cloud_init,
        bind,
        bind_ro,
        tmpfs,
        publish,
        memory_high,
        memory_max,
        memory_swap_max,
        tasks_max,
        cpu_quota,
        cpu_weight,
        io_weight,
        source,
        artifact,
        output_format,
        socket,
        host,
        tls_ca,
        tls_cert,
        tls_key,
        timeout,
    )


@app.command("spawn")
def spawn_command(
    image: str = typer.Argument(..., metavar="IMAGE", help="Source-published image reference"),
    name: str = typer.Argument(..., metavar="NAME", help="New container name"),
    profile: str = typer.Option("standard", "--profile", help="Catalog profile to apply"),
    cloud_init: Optional[str] = typer.Option(None, "--cloud-init", help="Cloud-init template"),
    bind: Optional[List[str]] = typer.Option(None, "--bind", help="Writable SOURCE:DEST[:OPTIONS]"),
    bind_ro: Optional[List[str]] = typer.Option(
        None, "--bind-ro", help="Read-only SOURCE:DEST[:OPTIONS]"
    ),
    tmpfs: Optional[List[str]] = typer.Option(None, "--tmpfs", help="Tmpfs DEST[:OPTIONS]"),
    publish: Optional[List[str]] = typer.Option(None, "--publish", help="Publish a guest port"),
    memory_high: Optional[str] = typer.Option(None, "--memory-high", help="Systemd MemoryHigh"),
    memory_max: Optional[str] = typer.Option(None, "--memory-max", help="Systemd MemoryMax"),
    memory_swap_max: Optional[str] = typer.Option(
        None, "--memory-swap-max", help="Systemd MemorySwapMax"
    ),
    tasks_max: Optional[int] = typer.Option(None, "--tasks-max", min=1),
    cpu_quota: Optional[int] = typer.Option(None, "--cpu-quota", min=1, help="CPU quota percent"),
    cpu_weight: Optional[int] = typer.Option(None, "--cpu-weight", min=1, max=10000),
    io_weight: Optional[int] = typer.Option(None, "--io-weight", min=1, max=10000),
    source: Optional[str] = _common_source(),
    artifact: str = _common_artifact(),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Deprecated alias for launch."""
    error_console.print("Warning: 'spawn' is deprecated; use 'launch'.")
    _create_or_launch(
        "spawn",
        image,
        name,
        profile,
        cloud_init,
        bind,
        bind_ro,
        tmpfs,
        publish,
        memory_high,
        memory_max,
        memory_swap_max,
        tasks_max,
        cpu_quota,
        cpu_weight,
        io_weight,
        source,
        artifact,
        output_format,
        socket,
        host,
        tls_ca,
        tls_cert,
        tls_key,
        timeout,
    )


def _lifecycle(
    command: str,
    name: str,
    output_format: str,
    socket: str | None,
    host: str | None,
    tls_ca: str | None,
    tls_cert: str | None,
    tls_key: str | None,
    timeout: float | None,
) -> None:
    """Run a one-name lifecycle transition and state its result."""
    output_format = _format(output_format)
    result = _request(
        command,
        {"name": name},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=120,
        output_format=output_format,
    )
    words = {"start": "Started", "stop": "Stopped", "restart": "Restarted"}[command]
    print_success(f"{words} {name}.", result, output_format)


@app.command("start")
def start_command(
    name: str = typer.Argument(..., metavar="NAME", help="Stopped managed container"),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Start an existing container."""
    _lifecycle("start", name, output_format, socket, host, tls_ca, tls_cert, tls_key, timeout)


@app.command("stop")
def stop_command(
    name: str = typer.Argument(..., metavar="NAME", help="Running managed container"),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Stop an existing container."""
    _lifecycle("stop", name, output_format, socket, host, tls_ca, tls_cert, tls_key, timeout)


@app.command("restart")
def restart_command(
    name: str = typer.Argument(..., metavar="NAME", help="Managed container"),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Restart an existing container and leave it running."""
    _lifecycle("restart", name, output_format, socket, host, tls_ca, tls_cert, tls_key, timeout)


def _confirm_delete(name: str, force: bool, output_format: str) -> None:
    """Confirm destructive deletes without ever mixing prompts into JSON."""
    if force:
        return
    if output_format == "json":
        _exit_with_error(
            ClientError(
                "invalid_argument",
                "JSON deletion requires --force.",
                suggestion=f"Review '{name}', then retry with: chimeractl delete {name} --force --format json",
            ),
            cast(OutputFormat, output_format),
        )
    if not sys.stdin.isatty():
        _exit_with_error(
            ClientError(
                "invalid_argument",
                "Refusing to prompt for deletion on non-interactive input.",
                suggestion=f"Use 'chimeractl delete {name} --force' after reviewing the target.",
            ),
            cast(OutputFormat, output_format),
        )
    if not typer.confirm(f"Delete container {name}?"):
        raise typer.Abort()


def _delete(
    command: str,
    name: str,
    force: bool,
    output_format: str,
    socket: str | None,
    host: str | None,
    tls_ca: str | None,
    tls_cert: str | None,
    tls_key: str | None,
    timeout: float | None,
) -> None:
    """Delete a container using the primary command or compatibility alias."""
    output_format = _format(output_format)
    _confirm_delete(name, force, output_format)
    result = _request(
        command,
        {"name": name},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=600,
        output_format=output_format,
    )
    message = f"Deleted {name}." if result["deleted"] else f"{name} is already absent."
    print_success(message, result, output_format)


@app.command("delete")
def delete_command(
    name: str = typer.Argument(..., metavar="NAME", help="Managed container to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Delete without confirmation"),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Delete a container and its Chimera-managed files."""
    _delete("delete", name, force, output_format, socket, host, tls_ca, tls_cert, tls_key, timeout)


@app.command("remove")
def remove_command(
    name: str = typer.Argument(..., metavar="NAME", help="Managed container to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Delete without confirmation"),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Deprecated alias for delete."""
    error_console.print("Warning: 'remove' is deprecated; use 'delete'.")
    _delete("remove", name, force, output_format, socket, host, tls_ca, tls_cert, tls_key, timeout)


@app.command("info")
def info_command(
    name: str = typer.Argument(..., metavar="NAME", help="Managed container name"),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Show desired state, observed state, and the latest error."""
    output_format = _format(output_format)
    result = _request(
        "status",
        {"container": name},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=15,
        output_format=output_format,
    )
    print_info(result, name, output_format)


@app.command("status")
def status_command(
    name: Optional[str] = typer.Argument(
        None, metavar="[NAME]", help="Deprecated container name; use info NAME"
    ),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Show server status; status NAME is a deprecated alias for info NAME."""
    if name:
        error_console.print("Warning: 'status NAME' is deprecated; use 'info NAME'.")
        info_command(
            name,
            output_format,
            socket,
            host,
            tls_ca,
            tls_cert,
            tls_key,
            timeout,
        )
        return
    output_format = _format(output_format)
    result = _request(
        "status",
        {},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=15,
        output_format=output_format,
    )
    if output_format == "json":
        print_json(result)
    else:
        container_count = len(result["containers"])
        console.print(f"Chimera server is running; managing {container_count} container(s).")


@app.command("doctor")
def doctor_command(
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Diagnose server setup even when its control endpoint is unavailable."""
    output_format = _format(output_format)
    try:
        client = _make_client(
            socket=socket,
            host=host,
            tls_ca=tls_ca,
            tls_cert=tls_cert,
            tls_key=tls_key,
            timeout=timeout or 15,
        )
    except ClientError as error:
        _exit_with_error(error, output_format)
        raise AssertionError("unreachable")
    if client.mode == "tls":
        result = remote_doctor(client, timeout=timeout)
    else:
        result = local_doctor(socket)
        try:
            server_result = client.request("doctor", timeout=timeout)
        except ClientError as error:
            diagnosed = diagnose_client_error(error)
            suggestion = diagnosed.suggestion
            if suggestion and "chimeractl doctor" in suggestion:
                suggestion = "Inspect the Chimera server journal and correct the reported cause."
            result["healthy"] = False
            result["server_error"] = {
                "code": diagnosed.code,
                "message": diagnosed.message,
                "detail": diagnosed.detail,
                "suggestion": suggestion,
            }
        else:
            result["server"] = server_result
            result["server_error"] = None
            result["healthy"] = bool(result["healthy"] and server_result.get("healthy"))
    print_doctor(result, output_format)
    if not result["healthy"]:
        raise typer.Exit(1)


@app.command("exec")
def exec_command(
    name: str = typer.Argument(..., metavar="NAME", help="Running managed container"),
    command: List[str] = typer.Argument(..., metavar="COMMAND", help="Command to execute"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Run an interactive command: exec NAME -- COMMAND."""
    _stream(socket, host, tls_ca, tls_cert, tls_key, timeout, name, command)


@app.command("shell")
def shell_command(
    name: str = typer.Argument(..., metavar="NAME", help="Running managed container"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Open an interactive shell in a running container."""
    _stream(socket, host, tls_ca, tls_cert, tls_key, timeout, name, None)


@app.command("logs")
def logs_command(
    name: str = typer.Argument(..., metavar="NAME", help="Managed container name"),
    unit: Optional[str] = typer.Option(None, "--unit", "-u", help="Guest journal unit"),
    lines: int = typer.Option(200, "--lines", "-n", min=0, help="Initial journal lines"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow new journal records"),
    supervisor: bool = typer.Option(
        False, "--supervisor", help="Read the host systemd-nspawn service journal"
    ),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Stream guest or nspawn-supervisor records from the server journal."""
    if supervisor and unit is not None:
        raise typer.BadParameter("--unit cannot be combined with --supervisor")
    try:
        client = _make_client(
            socket=socket,
            host=host,
            tls_ca=tls_ca,
            tls_cert=tls_cert,
            tls_key=tls_key,
            timeout=timeout or 30,
        )
        code = stream_logs(
            client,
            name,
            unit=unit,
            lines=lines,
            follow=follow,
            supervisor=supervisor,
            timeout=timeout or 30,
        )
    except ClientError as error:
        _exit_with_error(error, "table")
        raise AssertionError("unreachable")
    if code != 0:
        raise typer.Exit(code)


def _stream(
    socket: str | None,
    host: str | None,
    tls_ca: str | None,
    tls_cert: str | None,
    tls_key: str | None,
    timeout: float | None,
    name: str,
    command: list[str] | None,
) -> None:
    """Render stream startup failures like normal CLI errors."""
    try:
        client = _make_client(
            socket=socket,
            host=host,
            tls_ca=tls_ca,
            tls_cert=tls_cert,
            tls_key=tls_key,
            timeout=timeout or 30,
        )
        code = stream_terminal(client, name, command, timeout or 30)
    except ClientError as error:
        _exit_with_error(error, "table")
        raise AssertionError("unreachable")
    except Exception as error:
        _exit_with_error(
            ClientError(
                "host_operation_failed",
                "Could not open a terminal session in the container.",
                detail=str(error),
                suggestion=f"Check the container with: chimeractl info {name}",
            ),
            "table",
        )
        raise AssertionError("unreachable")
    if code != 0:
        raise typer.Exit(code)


@image_app.command("list")
def image_list_command(
    source: Optional[str] = _common_source(),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """List native-architecture products from one SimpleStreams source."""
    output_format = _format(output_format)
    if source is None:
        _exit_with_error(
            ClientError(
                "invalid_argument",
                "Listing images requires a configured SimpleStreams source.",
                suggestion="Run 'chimeractl image source list' then 'chimeractl image list --source SOURCE'.",
            ),
            output_format,
        )
    result = _request(
        "list",
        {"type": "images", "image_source": source},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=60,
        output_format=output_format,
    )
    print_resources(result, output_format)


@image_source_app.command("list")
def image_source_list_command(
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """List administrator-configured image sources."""
    output_format = _format(output_format)
    result = _request(
        "list",
        {"type": "image_sources"},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=10,
        output_format=output_format,
    )
    print_resources(result, output_format)


@image_app.command("info")
def image_info_command(
    name: str = typer.Argument(..., metavar="IMAGE", help="Source-published image reference"),
    source: Optional[str] = _common_source(),
    artifact: str = _common_artifact(),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Show SimpleStreams image identity, selected artifact, and local materialization."""
    output_format = _format(output_format)
    args: dict[str, Any] = {"name": name, "image_artifact": artifact}
    if source is not None:
        args["image_source"] = source
    result = _request(
        "image_info",
        args,
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=60,
        output_format=output_format,
    )
    print_image_info(result, output_format)


@image_app.command("pull")
def image_pull_command(
    name: str = typer.Argument(..., metavar="IMAGE", help="Source-published image reference"),
    source: Optional[str] = _common_source(),
    artifact: str = _common_artifact(),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Pull an image now; launch pulls a missing image automatically."""
    output_format = _format(output_format)
    args: dict[str, Any] = {"name": name, "image_artifact": artifact}
    if source is not None:
        args["image_source"] = source
    result = _request(
        "image_pull",
        args,
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=600,
        output_format=output_format,
    )
    print_success(f"Pulled image {result.get('image', name)}.", result, output_format)


@profile_app.command("list")
def profile_list_command(
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """List profiles accepted by create and launch."""
    output_format = _format(output_format)
    result = _request(
        "list",
        {"type": "profiles"},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=10,
        output_format=output_format,
    )
    print_resources(result, output_format)


@config_app.command("validate")
def config_validate_command(
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Validate image, profile, cloud-init, and service configuration."""
    output_format = _format(output_format)
    result = _request(
        "validate",
        {},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=30,
        output_format=output_format,
    )
    print_success("Configuration is valid.", result, output_format)


@config_app.command("import-nodes")
def import_nodes_command(
    path: Path = typer.Argument(..., exists=True, readable=True, help="Legacy node YAML file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate without storing records"),
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Import legacy configs/nodes YAML into durable CLI-managed state."""
    output_format = _format(output_format)
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        containers = data.get("containers", {})
        if not isinstance(containers, dict):
            raise ValueError("'containers' must be a mapping")
        records = []
        for name, spec in containers.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                raise ValueError("each legacy container must have a string name and mapping value")
            embedded_name = spec.get("name")
            if embedded_name is not None and embedded_name != name:
                raise ValueError(
                    f"legacy mapping key '{name}' does not match embedded name '{embedded_name}'"
                )
            record = {**spec, "name": name}
            records.append(model_dump(model_validate(ContainerSpec, record)))
    except (OSError, ValidationError, ValueError, TypeError) as error:
        _exit_with_error(
            ClientError(
                "invalid_configuration",
                "The legacy node file is invalid.",
                detail=str(error),
                suggestion="Correct the file and retry 'chimeractl config import-nodes'.",
            ),
            output_format,
        )
    result = _request(
        "import_records",
        {"records": records, "dry_run": dry_run},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=30,
        output_format=output_format,
    )
    if dry_run:
        print_success(
            f"Validated {result['validated']} legacy container record(s) without importing.",
            result,
            output_format,
        )
    else:
        print_success(
            f"Imported {result['imported']} legacy container record(s).", result, output_format
        )


@server_app.command("status")
def server_status_command(
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Show whether the server is serving commands."""
    status_command(
        name=None,
        output_format=output_format,
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
    )


@server_app.command("reload")
def server_reload_command(
    output_format: str = typer.Option("table", "--format", help="Output: table or json"),
    socket: Optional[str] = _common_socket(),
    host: Optional[str] = _common_host(),
    tls_ca: Optional[str] = _common_tls_ca(),
    tls_cert: Optional[str] = _common_tls_cert(),
    tls_key: Optional[str] = _common_tls_key(),
    timeout: Optional[float] = typer.Option(None, "--timeout", min=1.0),
) -> None:
    """Reload image definitions, profiles, and cloud-init templates."""
    output_format = _format(output_format)
    result = _request(
        "reload",
        {},
        socket=socket,
        host=host,
        tls_ca=tls_ca,
        tls_cert=tls_cert,
        tls_key=tls_key,
        timeout=timeout,
        default_timeout=30,
        output_format=output_format,
    )
    print_success("Reloaded the Chimera catalog.", result, output_format)


def main() -> None:
    """Run the Typer application."""
    app()
