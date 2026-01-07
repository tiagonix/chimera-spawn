"""Main CLI implementation using Typer."""

import sys
from pathlib import Path
from typing import Optional, List

import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import print as rprint

from chimera.cli.commands import (
    list_resources,
    spawn_container,
    stop_container,
    start_container,
    remove_container,
    exec_in_container,
    shell_in_container,
    pull_image,
    show_status,
    validate_config,
    agent_status,
    agent_reload,
)
from chimera.cli.client import IPCClient, IPCError


# Create Typer app
app = typer.Typer(
    name="chimeractl",
    help="Chimera Spawn - Modern systemd-nspawn container orchestration",
    add_completion=False,
)

# Console for rich output
console = Console()


@app.command("list")
def list_command(
    resource_type: Optional[str] = typer.Argument(
        None,
        help="Resource type to list (images, containers, profiles)"
    ),
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """List resources (images, containers, profiles)."""
    try:
        client = IPCClient(socket_path=socket)
        list_resources(client, resource_type)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("spawn")
def spawn_command(
    name: Optional[str] = typer.Argument(
        None,
        help="Container name to spawn"
    ),
    all: bool = typer.Option(
        False,
        "--all",
        help="Spawn all configured containers"
    ),
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Create and start container(s)."""
    if not name and not all:
        console.print("[red]Error:[/red] Specify container name or use --all")
        raise typer.Exit(1)
        
    try:
        client = IPCClient(socket_path=socket)
        spawn_container(client, name, all)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("stop")
def stop_command(
    name: str = typer.Argument(..., help="Container name"),
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Stop a running container."""
    try:
        client = IPCClient(socket_path=socket)
        stop_container(client, name)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("start")
def start_command(
    name: str = typer.Argument(..., help="Container name"),
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Start a stopped container."""
    try:
        client = IPCClient(socket_path=socket)
        start_container(client, name)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("restart")
def restart_command(
    name: str = typer.Argument(..., help="Container name"),
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Restart a container."""
    try:
        client = IPCClient(socket_path=socket)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Restarting {name}...", total=None)
            stop_container(client, name, quiet=True)
            start_container(client, name, quiet=True)
            progress.update(task, completed=True)
            
        console.print(f"[green]✓[/green] Container {name} restarted")
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("remove")
def remove_command(
    name: str = typer.Argument(..., help="Container name"),
    force: bool = typer.Option(
        False,
        "--force", "-f",
        help="Force removal without confirmation"
    ),
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Remove a container completely."""
    if not force:
        confirm = typer.confirm(f"Remove container {name}?")
        if not confirm:
            raise typer.Abort()
            
    try:
        client = IPCClient(socket_path=socket)
        remove_container(client, name)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("exec")
def exec_command(
    name: str = typer.Argument(..., help="Container name"),
    command: List[str] = typer.Argument(..., help="Command to execute"),
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Execute command in container."""
    try:
        client = IPCClient(socket_path=socket)
        exec_in_container(client, name, command)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("shell")
def shell_command(
    name: str = typer.Argument(..., help="Container name"),
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Open interactive shell in container."""
    try:
        shell_in_container(name)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


# Image subcommands
image_app = typer.Typer(help="Image management commands")
app.add_typer(image_app, name="image")


@image_app.command("pull")
def image_pull_command(
    name: str = typer.Argument(..., help="Image name to pull"),
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Pull a container image."""
    try:
        client = IPCClient(socket_path=socket)
        pull_image(client, name)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@image_app.command("list")
def image_list_command(
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """List available images."""
    try:
        client = IPCClient(socket_path=socket)
        list_resources(client, "images")
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


# Profile subcommands
profile_app = typer.Typer(help="Profile management commands")
app.add_typer(profile_app, name="profile")


@profile_app.command("list")
def profile_list_command(
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """List available profiles."""
    try:
        client = IPCClient(socket_path=socket)
        list_resources(client, "profiles")
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


# System commands
@app.command("status")
def status_command(
    container: Optional[str] = typer.Argument(
        None,
        help="Show status for specific container"
    ),
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Show overall system status."""
    try:
        client = IPCClient(socket_path=socket)
        show_status(client, container)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


# Config subcommands
config_app = typer.Typer(help="Configuration commands")
app.add_typer(config_app, name="config")


@config_app.command("validate")
def config_validate_command(
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Validate configuration files."""
    try:
        client = IPCClient(socket_path=socket)
        validate_config(client)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


# Agent subcommands
agent_app = typer.Typer(help="Agent management commands")
app.add_typer(agent_app, name="agent")


@agent_app.command("status")
def agent_status_command(
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Show agent status."""
    try:
        client = IPCClient(socket_path=socket)
        agent_status(client)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@agent_app.command("reload")
def agent_reload_command(
    socket: Optional[str] = typer.Option(
        None,
        "--socket", "-s",
        help="Agent socket path"
    ),
):
    """Reload agent configuration."""
    try:
        client = IPCClient(socket_path=socket)
        agent_reload(client)
    except IPCError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def main():
    """Main entry point for CLI."""
    app()
