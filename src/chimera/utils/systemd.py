"""Systemd utilities and DBus integration."""

import asyncio
import logging
import subprocess
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from dbus_next.aio import MessageBus
from dbus_next import BusType, Variant


logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result from running a command."""
    returncode: int
    stdout: str = ""
    stderr: str = ""


async def run_command(
    cmd: List[str],
    check: bool = True,
    capture_output: bool = False,
    timeout: Optional[int] = None,
    **kwargs
) -> CommandResult:
    """Run a command asynchronously."""
    logger.debug(f"Running command: {' '.join(cmd)}")
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE if capture_output else None,
        stderr=asyncio.subprocess.PIPE if capture_output else None,
        **kwargs
    )
    
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise subprocess.TimeoutExpired(cmd, timeout)
        
    result = CommandResult(
        returncode=process.returncode,
        stdout=stdout.decode() if stdout else "",
        stderr=stderr.decode() if stderr else "",
    )
    
    if check and process.returncode != 0:
        error = subprocess.CalledProcessError(
            process.returncode, cmd
        )
        error.stdout = result.stdout
        error.stderr = result.stderr
        raise error
        
    return result


class SystemdDBus:
    """DBus interface to systemd."""
    
    def __init__(self):
        """Initialize DBus connection."""
        self.bus: Optional[MessageBus] = None
        self.systemd = None
        self.machine = None
        
    async def connect(self):
        """Connect to system DBus."""
        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            
            # Get systemd manager interface
            introspection = await self.bus.introspect(
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1"
            )
            self.systemd = self.bus.get_proxy_object(
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                introspection
            ).get_interface("org.freedesktop.systemd1.Manager")
            
            # Get machine manager interface
            try:
                introspection = await self.bus.introspect(
                    "org.freedesktop.machine1",
                    "/org/freedesktop/machine1"
                )
                self.machine = self.bus.get_proxy_object(
                    "org.freedesktop.machine1",
                    "/org/freedesktop/machine1",
                    introspection
                ).get_interface("org.freedesktop.machine1.Manager")
            except Exception as e:
                logger.warning(f"Failed to connect to machine1: {e}")
                
            logger.debug("Connected to systemd DBus")
            
        except Exception as e:
            logger.error(f"Failed to connect to DBus: {e}")
            raise
            
    async def disconnect(self):
        """Disconnect from DBus."""
        if self.bus:
            self.bus.disconnect()
            
    async def reload_daemon(self):
        """Reload systemd daemon configuration."""
        if self.systemd:
            try:
                await self.systemd.call_reload()
                logger.debug("Reloaded systemd daemon")
            except Exception as e:
                logger.error(f"Failed to reload systemd: {e}")
                # Fall back to command
                await run_command(["systemctl", "daemon-reload"])
                
    async def start_unit(self, unit_name: str):
        """Start a systemd unit."""
        if self.systemd:
            try:
                await self.systemd.call_start_unit(unit_name, "replace")
                logger.debug(f"Started unit {unit_name}")
            except Exception as e:
                logger.error(f"Failed to start unit via DBus: {e}")
                # Fall back to command
                await run_command(["systemctl", "start", unit_name])
        else:
            await run_command(["systemctl", "start", unit_name])
            
    async def stop_unit(self, unit_name: str):
        """Stop a systemd unit."""
        if self.systemd:
            try:
                await self.systemd.call_stop_unit(unit_name, "replace")
                logger.debug(f"Stopped unit {unit_name}")
            except Exception as e:
                logger.error(f"Failed to stop unit via DBus: {e}")
                # Fall back to command
                await run_command(["systemctl", "stop", unit_name])
        else:
            await run_command(["systemctl", "stop", unit_name])
            
    async def enable_unit(self, unit_name: str):
        """Enable a systemd unit."""
        if self.systemd:
            try:
                await self.systemd.call_enable_unit_files([unit_name], False, True)
                logger.debug(f"Enabled unit {unit_name}")
            except Exception as e:
                logger.error(f"Failed to enable unit via DBus: {e}")
                # Fall back to command
                await run_command(["systemctl", "enable", unit_name])
        else:
            await run_command(["systemctl", "enable", unit_name])
            
    async def disable_unit(self, unit_name: str):
        """Disable a systemd unit."""
        if self.systemd:
            try:
                await self.systemd.call_disable_unit_files([unit_name], False)
                logger.debug(f"Disabled unit {unit_name}")
            except Exception as e:
                logger.error(f"Failed to disable unit via DBus: {e}")
                # Fall back to command
                await run_command(["systemctl", "disable", unit_name])
        else:
            await run_command(["systemctl", "disable", unit_name])
            
    async def get_unit_state(self, unit_name: str) -> str:
        """Get the active state of a unit."""
        if self.systemd:
            try:
                unit_path = await self.systemd.call_get_unit(unit_name)
                
                # Get unit properties
                introspection = await self.bus.introspect(
                    "org.freedesktop.systemd1",
                    unit_path
                )
                unit_proxy = self.bus.get_proxy_object(
                    "org.freedesktop.systemd1",
                    unit_path,
                    introspection
                ).get_interface("org.freedesktop.DBus.Properties")
                
                state = await unit_proxy.call_get(
                    "org.freedesktop.systemd1.Unit",
                    "ActiveState"
                )
                return state.value
                
            except Exception as e:
                logger.debug(f"Failed to get unit state via DBus: {e}")
                # Fall back to command
                result = await run_command(
                    ["systemctl", "is-active", unit_name],
                    check=False,
                    capture_output=True
                )
                return result.stdout.strip()
        else:
            result = await run_command(
                ["systemctl", "is-active", unit_name],
                check=False,
                capture_output=True
            )
            return result.stdout.strip()
            
    async def list_machines(self) -> List[Dict[str, Any]]:
        """List all machines."""
        if self.machine:
            try:
                machines = await self.machine.call_list_machines()
                return [
                    {
                        "name": m[0],
                        "class": m[1],
                        "service": m[2],
                        "object_path": m[3],
                    }
                    for m in machines
                ]
            except Exception as e:
                logger.debug(f"Failed to list machines via DBus: {e}")
                
        # Fall back to command
        result = await run_command(
            ["machinectl", "list", "--no-legend", "--no-pager"],
            capture_output=True
        )
        
        machines = []
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split()
                if len(parts) >= 2:
                    machines.append({
                        "name": parts[0],
                        "class": parts[1] if len(parts) > 1 else "",
                        "service": "",
                        "object_path": "",
                    })
                    
        return machines
