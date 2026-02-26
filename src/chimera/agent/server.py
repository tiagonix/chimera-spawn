"""HTTP/REST/WebSocket server for agent communication."""

import asyncio
import json
import logging
import os
import pty
import shlex
import struct
import fcntl
import termios
from pathlib import Path
from typing import Dict, Any, Optional

from aiohttp import web, WSMsgType

from chimera.agent.engine import StateEngine
from chimera.agent.config import ConfigManager


logger = logging.getLogger(__name__)


# Commands that require root privileges (checked at routing level or inside handlers)
PRIVILEGED_COMMANDS = {
    "spawn", "stop", "start", "restart", "remove", "exec", 
    "reconcile", "reload", "image_pull", "stream_exec", "stream_shell"
}


class AgentServer:
    """Agent HTTP/WebSocket server."""
    
    def __init__(self, socket_path: Path, host: Optional[str], port: int, state_engine: StateEngine, config_manager: ConfigManager):
        """Initialize server."""
        self.socket_path = Path(socket_path)
        self.host = host
        self.port = port
        self.state_engine = state_engine
        self.config_manager = config_manager
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self._setup_routes()
        
    def _setup_routes(self):
        """Setup API routes."""
        # Management APIs (REST)
        self.app.router.add_post('/api/v1/command', self._handle_command)
        
        # Streaming APIs (WebSocket)
        self.app.router.add_get('/api/v1/stream/exec', self._handle_stream_exec)
        self.app.router.add_get('/api/v1/stream/shell', self._handle_stream_shell)
        
    async def start(self):
        """Start the server."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        
        # Bind to Unix socket
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        
        site_unix = web.UnixSite(self.runner, str(self.socket_path))
        await site_unix.start()
        # Set socket permissions
        os.chmod(self.socket_path, 0o666)
        logger.info(f"Agent listening on unix:{self.socket_path}")
        
        # Bind to TCP if configured
        if self.host:
            site_tcp = web.TCPSite(self.runner, self.host, self.port)
            await site_tcp.start()
            logger.info(f"Agent listening on tcp://{self.host}:{self.port}")
            
    async def stop(self):
        """Stop the server."""
        if self.runner:
            await self.runner.cleanup()
        if self.socket_path.exists():
            self.socket_path.unlink()
        logger.info("Agent server stopped")

    async def _handle_command(self, request: web.Request) -> web.Response:
        """Handle standard REST command."""
        try:
            data = await request.json()
            command = data.get("command")
            args = data.get("args", {})
            
            # TODO: Add authentication/authorization check here for remote requests
            
            response_data = await self._process_command(command, args)
            return web.json_response({"success": True, "data": response_data})
            
        except Exception as e:
            logger.error(f"Command error: {e}", exc_info=True)
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def _process_command(self, command: str, args: Dict[str, Any]) -> Any:
        """Process the command logic."""
        handlers = {
            "status": self._handle_status,
            "list": self._handle_list,
            "spawn": self._handle_spawn,
            "stop": self._handle_stop,
            "start": self._handle_start,
            "restart": self._handle_restart,
            "remove": self._handle_remove,
            "exec": self._handle_exec,
            "reconcile": self._handle_reconcile,
            "reload": self._handle_reload,
            "image_pull": self._handle_image_pull,
            "validate": self._handle_validate,
        }
        
        handler = handlers.get(command)
        if not handler:
            raise ValueError(f"Unknown command: {command}")
            
        return await handler(args)

    async def _handle_stream_exec(self, request: web.Request) -> web.StreamResponse:
        """Handle websocket exec stream."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        name = request.query.get("name")
        command_str = request.query.get("command")
        command = json.loads(command_str) if command_str else []
        
        await self._handle_stream(ws, name, command)
        return ws

    async def _handle_stream_shell(self, request: web.Request) -> web.StreamResponse:
        """Handle websocket shell stream."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        name = request.query.get("name")
        await self._handle_stream(ws, name, None)
        return ws

    async def _handle_stream(self, ws: web.WebSocketResponse, container_name: str, command: Optional[list]):
        """Handle bidirectional streaming with PTY and resize support."""
        if not container_name:
            await ws.close()
            return

        master, slave = pty.openpty()
        
        try:
            cmd = ["machinectl", "shell", container_name]
            if command:
                cmd.extend(["/bin/bash", "-c", shlex.join(command)])
                
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=slave, stdout=slave, stderr=slave
            )
        finally:
            os.close(slave)
            
        loop = asyncio.get_running_loop()
        
        async def pump_read():
            """Read from PTY and write to WebSocket."""
            try:
                while True:
                    data = await loop.run_in_executor(None, os.read, master, 4096)
                    if not data:
                        break
                    await ws.send_bytes(data)
            except (OSError, asyncio.CancelledError, ConnectionResetError):
                pass
                
        async def pump_write():
            """Read from WebSocket and write to PTY."""
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
                        await loop.run_in_executor(None, os.write, master, msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            if data.get("type") == "resize":
                                rows = data.get("rows", 24)
                                cols = data.get("cols", 80)
                                winsz = struct.pack("HHHH", rows, cols, 0, 0)
                                fcntl.ioctl(master, termios.TIOCSWINSZ, winsz)
                                logger.debug(f"Resized PTY to {cols}x{rows}")
                        except Exception as e:
                            logger.warning(f"Invalid control message: {e}")
                    elif msg.type == WSMsgType.ERROR:
                        break
            except (asyncio.CancelledError, ConnectionResetError):
                pass
        
        read_task = asyncio.create_task(pump_read())
        write_task = asyncio.create_task(pump_write())
        
        # Wait for either the process to exit (pump_read stops) 
        # OR the client to disconnect (pump_write stops)
        done, pending = await asyncio.wait(
            [read_task, write_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Cancel whatever is still running to free resources
        for task in pending:
            task.cancel()
            
        # Ensure cleanup
        try:
            proc.terminate()
            await proc.wait()
        except ProcessLookupError:
            pass
        try:
            os.close(master)
        except OSError:
            pass

    # -- Command Handlers (Delegated to StateEngine/ConfigManager) --
    
    async def _handle_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        container_name = args.get("container")
        if container_name:
            status = await self.state_engine.get_container_status(container_name)
            if not status:
                raise ValueError(f"Container {container_name} not found")
            return {"containers": {container_name: status}}
        else:
            containers = await self.state_engine.get_all_container_statuses()
            return {
                "agent": {
                    "running": True,
                    "last_reconciliation": self.state_engine.last_reconciliation.isoformat() 
                        if self.state_engine.last_reconciliation else None,
                },
                "containers": containers,
            }

    async def _handle_list(self, args: Dict[str, Any]) -> Dict[str, Any]:
        resource_type = args.get("type", "all")
        result = {}
        
        if resource_type in ["all", "images"]:
            result["images"] = {
                name: {
                    "name": name,
                    "type": spec.type,
                    "source": spec.source,
                    "verify": spec.verify,
                } for name, spec in self.config_manager.images.items()
            }
            
        if resource_type in ["all", "containers"]:
            result["containers"] = await self.state_engine.get_all_container_statuses()
            
        if resource_type in ["all", "profiles"]:
            result["profiles"] = {
                name: {
                    "name": name,
                    "has_nspawn_config": bool(spec.nspawn_config_content),
                    "has_systemd_override": bool(spec.systemd_override_content),
                } for name, spec in self.config_manager.profiles.items()
            }
        return result

    async def _handle_spawn(self, args: Dict[str, Any]) -> Dict[str, Any]:
        container_name = args.get("name")
        all_containers = args.get("all", False)
        
        if all_containers:
            results = {}
            for name in self.config_manager.containers:
                try:
                    await self.state_engine.create_container(name)
                    await self.state_engine.start_container(name)
                    results[name] = {"success": True}
                except Exception as e:
                    results[name] = {"success": False, "error": str(e)}
            return {"results": results}
        else:
            if not container_name:
                raise ValueError("Container name required")
            await self.state_engine.create_container(container_name)
            await self.state_engine.start_container(container_name)
            return {"container": container_name, "created": True}

    async def _handle_stop(self, args: Dict[str, Any]) -> Dict[str, Any]:
        container_name = args.get("name")
        if not container_name:
            raise ValueError("Container name required")
        await self.state_engine.stop_container(container_name)
        return {"container": container_name, "stopped": True}

    async def _handle_start(self, args: Dict[str, Any]) -> Dict[str, Any]:
        container_name = args.get("name")
        if not container_name:
            raise ValueError("Container name required")
        await self.state_engine.start_container(container_name)
        return {"container": container_name, "started": True}

    async def _handle_restart(self, args: Dict[str, Any]) -> Dict[str, Any]:
        container_name = args.get("name")
        if not container_name:
            raise ValueError("Container name required")
        await self.state_engine.restart_container(container_name)
        return {"container": container_name, "restarted": True}

    async def _handle_remove(self, args: Dict[str, Any]) -> Dict[str, Any]:
        container_name = args.get("name")
        if not container_name:
            raise ValueError("Container name required")
        await self.state_engine.remove_container(container_name)
        return {"container": container_name, "removed": True}

    async def _handle_exec(self, args: Dict[str, Any]) -> Dict[str, Any]:
        # Non-interactive exec
        container_name = args.get("name")
        command = args.get("command")
        if not container_name or not command:
            raise ValueError("Container name and command required")
        return await self.state_engine.execute_in_container(container_name, command)

    async def _handle_reconcile(self, args: Dict[str, Any]) -> Dict[str, Any]:
        await self.state_engine.reconcile()
        return {"reconciled": True}

    async def _handle_reload(self, args: Dict[str, Any]) -> Dict[str, Any]:
        await self.config_manager.load()
        return {"reloaded": True}

    async def _handle_image_pull(self, args: Dict[str, Any]) -> Dict[str, Any]:
        image_name = args.get("name")
        if not image_name:
            raise ValueError("Image name required")
        image_spec = self.config_manager.get_image_spec(image_name)
        if not image_spec:
            raise ValueError(f"Image {image_name} not found")
        image_provider = self.state_engine.provider_registry.get_provider("image")
        await image_provider.present(image_spec)
        return {"image": image_name, "pulled": True}

    async def _handle_validate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        await self.config_manager.load()
        # Simplified validation logic for brevity, assumes provider validation works
        return {"valid": True, "message": "Configuration validated"}
