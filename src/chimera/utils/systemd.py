"""
Systemd utilities and DBus integration.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

from dbus_next import BusType  # type: ignore[attr-defined]
from dbus_next.aio import MessageBus  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)

_CHILD_GRACE_SECONDS = 2.0
_UNIT_OPERATION_TIMEOUT = 120.0


@dataclass
class CommandResult:
    """Result from running a command."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


async def _reap_owned_child(process: asyncio.subprocess.Process) -> None:
    """Terminate and wait for a child this process created, then reap it."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=_CHILD_GRACE_SECONDS)
        return
    except TimeoutError:
        pass
    except asyncio.CancelledError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    try:
        process.kill()
    except ProcessLookupError:
        return
    await process.wait()


async def run_command(
    cmd: list[str],
    check: bool = True,
    capture_output: bool = True,
    timeout: float | None = 30,
    **kwargs: Any,
) -> CommandResult:
    """Run a command asynchronously and reap the owned child on timeout or cancel."""
    logger.debug(f"Running command: {' '.join(cmd)}")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE if capture_output else None,
        stderr=asyncio.subprocess.PIPE if capture_output else None,
        **kwargs,
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as error:
        await _reap_owned_child(process)
        raise subprocess.TimeoutExpired(cmd, timeout or 0) from error
    except asyncio.CancelledError:
        await _reap_owned_child(process)
        raise

    returncode = process.returncode
    if returncode is None:
        raise RuntimeError(f"Command did not report a return code: {' '.join(cmd)}")

    result = CommandResult(
        returncode=returncode,
        stdout=stdout.decode() if stdout else "",
        stderr=stderr.decode() if stderr else "",
    )

    if check and returncode != 0:
        command_error = subprocess.CalledProcessError(returncode, cmd)
        command_error.stdout = result.stdout
        command_error.stderr = result.stderr
        raise command_error

    return result


class SystemdDBus:
    """DBus interface to systemd."""

    def __init__(self) -> None:
        """Initialize DBus connection."""
        self.bus: Any | None = None
        self.systemd: Any | None = None
        self.machine: Any | None = None

    async def connect(self) -> None:
        """Connect to system DBus."""
        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

            # Get systemd manager interface
            introspection = await self.bus.introspect(
                "org.freedesktop.systemd1", "/org/freedesktop/systemd1"
            )
            self.systemd = self.bus.get_proxy_object(
                "org.freedesktop.systemd1", "/org/freedesktop/systemd1", introspection
            ).get_interface("org.freedesktop.systemd1.Manager")

            # Get machine manager interface
            try:
                introspection = await self.bus.introspect(
                    "org.freedesktop.machine1", "/org/freedesktop/machine1"
                )
                self.machine = self.bus.get_proxy_object(
                    "org.freedesktop.machine1", "/org/freedesktop/machine1", introspection
                ).get_interface("org.freedesktop.machine1.Manager")
            except Exception as e:
                logger.warning(f"Failed to connect to machine1: {e}")

            logger.debug("Connected to systemd DBus")

        except Exception as e:
            logger.error(f"Failed to connect to DBus: {e}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from DBus."""
        if self.bus:
            self.bus.disconnect()

    async def _execute_fallback(
        self,
        dbus_method_name: str,
        dbus_args: list[Any],
        cli_cmd: list[str],
        success_msg: str,
        error_action: str,
    ) -> None:
        """Execute a DBus method with CLI fallback."""
        if self.systemd and self.bus:
            try:
                method = getattr(self.systemd, dbus_method_name)
                await method(*dbus_args)
                logger.debug(success_msg)
                return
            except Exception as e:
                logger.error(f"Failed to {error_action} via DBus: {e}")

        # Fall back to command (executed if systemd is None or if DBus failed)
        await run_command(cli_cmd)

    async def reload_daemon(self) -> None:
        """Reload systemd daemon configuration."""
        await self._execute_fallback(
            "call_reload",
            [],
            ["systemctl", "daemon-reload"],
            "Reloaded systemd daemon",
            "reload systemd",
        )

    async def start_unit(self, unit_name: str) -> None:
        """Start a systemd unit and wait until the requested operation completes."""
        await run_command(
            ["systemctl", "start", unit_name],
            timeout=_UNIT_OPERATION_TIMEOUT,
        )
        logger.debug("Started unit %s", unit_name)

    async def stop_unit(self, unit_name: str) -> None:
        """Stop a systemd unit and wait until the requested operation completes."""
        await run_command(
            ["systemctl", "stop", unit_name],
            timeout=_UNIT_OPERATION_TIMEOUT,
        )
        logger.debug("Stopped unit %s", unit_name)

    async def restart_unit(self, unit_name: str) -> None:
        """Restart a systemd unit and wait until the requested operation completes."""
        await run_command(
            ["systemctl", "restart", unit_name],
            timeout=_UNIT_OPERATION_TIMEOUT,
        )
        logger.debug("Restarted unit %s", unit_name)

    async def enable_unit(self, unit_name: str) -> None:
        """Enable a systemd unit."""
        await self._execute_fallback(
            "call_enable_unit_files",
            [[unit_name], False, True],
            ["systemctl", "enable", unit_name],
            f"Enabled unit {unit_name}",
            "enable unit",
        )

    async def disable_unit(self, unit_name: str) -> None:
        """Disable a systemd unit."""
        await self._execute_fallback(
            "call_disable_unit_files",
            [[unit_name], False],
            ["systemctl", "disable", unit_name],
            f"Disabled unit {unit_name}",
            "disable unit",
        )

    async def get_unit_state(self, unit_name: str) -> str:
        """Return a known systemd state or raise when observation is broken."""
        dbus_error: Exception | None = None
        if self.systemd and self.bus:
            try:
                unit_path = await self.systemd.call_get_unit(unit_name)
                introspection = await self.bus.introspect("org.freedesktop.systemd1", unit_path)
                unit_proxy = self.bus.get_proxy_object(
                    "org.freedesktop.systemd1", unit_path, introspection
                ).get_interface("org.freedesktop.DBus.Properties")
                state = await unit_proxy.call_get("org.freedesktop.systemd1.Unit", "ActiveState")
                return str(state.value)
            except Exception as error:
                message = str(error)
                if "org.freedesktop.systemd1.NoSuchUnit" in message:
                    return "not-found"
                dbus_error = error
                logger.debug("Failed to get unit state via DBus: %s", error)

        try:
            result = await run_command(
                [
                    "systemctl",
                    "show",
                    unit_name,
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--no-pager",
                ],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                f"Could not observe systemd unit {unit_name}: systemctl missing"
            ) from error
        if result.returncode != 0:
            detail = result.stderr.strip() or str(dbus_error or "systemctl show failed")
            lowered = detail.lower()
            if (
                "permission" in lowered
                or "denied" in lowered
                or "dbus" in lowered
                or "timed out" in lowered
            ):
                raise RuntimeError(f"Could not observe systemd unit {unit_name}: {detail}")
            raise RuntimeError(f"Could not observe systemd unit {unit_name}: {detail}")

        properties: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                properties[key] = value
        load_state = properties.get("LoadState", "")
        active_state = properties.get("ActiveState", "")
        if load_state == "not-found":
            return "not-found"
        if active_state in {"active", "inactive", "failed", "activating", "deactivating"}:
            return active_state
        detail = result.stderr.strip() or str(dbus_error or "systemctl produced no state")
        raise RuntimeError(f"Could not observe systemd unit {unit_name}: {detail}")

    async def list_machines(self) -> list[dict[str, Any]]:
        """List online machines; observation failure is not an empty inventory."""
        dbus_error: Exception | None = None
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
                dbus_error = e
                logger.debug(f"Failed to list machines via DBus: {e}")

        try:
            result = await run_command(
                ["machinectl", "list", "--no-legend", "--no-pager"],
                capture_output=True,
                check=False,
            )
        except FileNotFoundError as error:
            raise RuntimeError("Could not list machines: machinectl missing") from error
        if result.returncode != 0:
            detail = result.stderr.strip() or str(dbus_error or "machinectl list failed")
            raise RuntimeError(f"Could not list machines: {detail}")

        machines = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split()
                if len(parts) >= 2:
                    machines.append(
                        {
                            "name": parts[0],
                            "class": parts[1] if len(parts) > 1 else "",
                            "service": "",
                            "object_path": "",
                        }
                    )

        return machines
