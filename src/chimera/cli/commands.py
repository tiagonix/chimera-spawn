"""Command implementations for CLI."""

import subprocess
import sys
import shlex
from typing import Optional, List, Dict, Any
from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import print as rprint

from chimera.cli.client import IPCClient


console = Console()
stderr_console = Console(stderr=True)


def _run_action(
    client: IPCClient,
    description: str,
    command: str,
    args: Dict[str, Any],
    success_msg: Optional[str] = None,
    quiet: bool = False
) -> Dict[str, Any]:
    """Helper to run an IPC action with a progress spinner."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        disable=quiet,
    ) as progress:
        task = progress.add_task(description, total=None)
        
        response = client.request(command, args)
        
        progress.update(task, completed=True)
        
    if success_msg and not quiet:
        console.print(success_msg)
        
    return response


def list_resources(client: IPCClient, resource_type: Optional[str] = None):
    """List resources with formatted output."""
    if not resource_type:
        resource_type = "all"
        
    response = client.request("list", {"type": resource_type})
    
    # Display images
    if "images" in response:
        table = Table(title="Images")
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="magenta")
        table.add_column("Verify")
        table.add_column("Source", style="dim", max_width=50)
        
        for name, info in response["images"].items():
            table.add_row(
                name,
                info["type"],
                info["verify"],
                info["source"]
            )
            
        console.print(table)
        console.print()
        
    # Display containers
    if "containers" in response:
        table = Table(title="Containers")
        table.add_column("Name", style="cyan")
        table.add_column("State", style="green")
        table.add_column("Running")
        table.add_column("Image", style="magenta")
        table.add_column("Profile")
        
        for name, info in response["containers"].items():
            state_color = "green" if info["running"] else "yellow"
            running_status = "[green]●[/green]" if info["running"] else "[red]○[/red]"
            
            table.add_row(
                name,
                f"[{state_color}]{info['desired_state']}[/{state_color}]",
                running_status,
                info["image"],
                info["profile"]
            )
            
        console.print(table)
        console.print()
        
    # Display profiles
    if "profiles" in response:
        table = Table(title="Profiles")
        table.add_column("Name", style="cyan")
        table.add_column("nspawn Config")
        table.add_column("systemd Override")
        
        for name, info in response["profiles"].items():
            nspawn = "✓" if info["has_nspawn_config"] else "✗"
            systemd = "✓" if info["has_systemd_override"] else "✗"
            
            table.add_row(name, nspawn, systemd)
            
        console.print(table)


def spawn_container(client: IPCClient, name: Optional[str], all_containers: bool, quiet: bool = False):
    """Spawn one or all containers."""
    if all_containers:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            disable=quiet,
        ) as progress:
            task = progress.add_task("Spawning all containers...", total=None)
            
            response = client.request("spawn", {"all": True})
            results = response.get("results", {})
            
            success_count = sum(1 for r in results.values() if r.get("success"))
            total_count = len(results)
            
            progress.update(task, completed=True)
            
        if not quiet:
            console.print(f"[green]✓[/green] Spawned {success_count}/{total_count} containers")
            
            # Show any errors
            for container, result in results.items():
                if not result.get("success"):
                    console.print(f"  [red]✗[/red] {container}: {result.get('error')}")
    else:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            disable=quiet,
        ) as progress:
            task = progress.add_task(f"Spawning container {name}...", total=None)
            
            response = client.request("spawn", {"name": name})
            
            progress.update(task, completed=True)
            
        if not quiet:
            console.print(f"[green]✓[/green] Container {name} spawned successfully")


def stop_container(client: IPCClient, name: str, quiet: bool = False):
    """Stop a container."""
    _run_action(
        client,
        description=f"Stopping container {name}...",
        command="stop",
        args={"name": name},
        success_msg=f"[green]✓[/green] Container {name} stopped",
        quiet=quiet
    )


def start_container(client: IPCClient, name: str, quiet: bool = False):
    """Start a container."""
    _run_action(
        client,
        description=f"Starting container {name}...",
        command="start",
        args={"name": name},
        success_msg=f"[green]✓[/green] Container {name} started",
        quiet=quiet
    )


def remove_container(client: IPCClient, name: str):
    """Remove a container."""
    _run_action(
        client,
        description=f"Removing container {name}...",
        command="remove",
        args={"name": name},
        success_msg=f"[green]✓[/green] Container {name} removed"
    )


def exec_in_container(name: str, command: List[str]):
    """Execute command in container locally to preserve TTY."""
    # `machinectl shell` requires an absolute path for the executable.
    # To run commands like `apt` which rely on $PATH, we must invoke the
    # container's own shell and pass the user's command to it.
    # `shlex.join` safely quotes the arguments into a single string.
    safe_command = shlex.join(command)
    cmd = ["machinectl", "shell", name, "/bin/bash", "-c", safe_command]
    
    # subprocess.run will stream output to stdout/stderr by default
    result = subprocess.run(cmd, check=False)
    
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def shell_in_container(name: str):
    """Open interactive shell in container."""
    # Use machinectl directly for interactive shell
    cmd = ["machinectl", "shell", name]
    subprocess.run(cmd)


def pull_image(client: IPCClient, name: str):
    """Pull an image."""
    _run_action(
        client,
        description=f"Pulling image {name}...",
        command="image_pull",
        args={"name": name},
        success_msg=f"[green]✓[/green] Image {name} pulled successfully"
    )


def show_status(client: IPCClient, container: Optional[str] = None):
    """Show system or container status."""
    response = client.request("status", {"container": container} if container else {})
    
    if container:
        # Show specific container status
        info = response["containers"].get(container)
        if not info:
            console.print(f"[red]Container {container} not found[/red]")
            return
            
        console.print(f"[bold]Container: {container}[/bold]")
        console.print(f"  Exists: {'Yes' if info['exists'] else 'No'}")
        console.print(f"  Running: {'Yes' if info['running'] else 'No'}")
        console.print(f"  Desired State: {info['desired_state']}")
        console.print(f"  Image: {info['image']}")
        console.print(f"  Profile: {info['profile']}")
    else:
        # Show overall status
        agent_info = response.get("agent", {})
        containers = response.get("containers", {})
        
        console.print("[bold]Agent Status[/bold]")
        console.print(f"  Running: {'Yes' if agent_info.get('running') else 'No'}")
        
        last_recon = agent_info.get("last_reconciliation")
        if last_recon:
            dt = datetime.fromisoformat(last_recon)
            console.print(f"  Last Reconciliation: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            console.print("  Last Reconciliation: Never")
            
        console.print()
        
        # Container summary
        total = len(containers)
        running = sum(1 for c in containers.values() if c.get("running"))
        console.print(f"[bold]Containers[/bold]: {running}/{total} running")
        
        if containers:
            console.print()
            list_resources(client, "containers")


def validate_config(client: IPCClient):
    """Validate configuration."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Validating configuration...", total=None)
        
        response = client.request("validate", {})
        
        progress.update(task, completed=True)
        
    if response.get("valid"):
        console.print("[green]✓[/green] Configuration is valid")
        console.print(f"  Images: {response.get('images', 0)}")
        console.print(f"  Profiles: {response.get('profiles', 0)}")
        console.print(f"  Containers: {response.get('containers', 0)}")
    else:
        console.print("[red]✗[/red] Configuration is invalid")
        console.print(f"  Error: {response.get('error')}")


def agent_status(client: IPCClient):
    """Show agent status."""
    show_status(client)


def agent_reload(client: IPCClient):
    """Reload agent configuration."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Reloading configuration...", total=None)
        
        response = client.request("reload", {})
        
        progress.update(task, completed=True)
        
    console.print("[green]✓[/green] Configuration reloaded")
