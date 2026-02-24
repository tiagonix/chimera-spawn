"""IPC server for agent communication."""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional

from chimera.agent.engine import StateEngine
from chimera.agent.config import ConfigManager


logger = logging.getLogger(__name__)


# Commands that require root privileges
PRIVILEGED_COMMANDS = {
    "spawn",
    "stop",
    "start",
    "remove",
    "exec",
    "reconcile",
    "reload",
    "image_pull",
}


class IPCServer:
    """Unix socket server for IPC communication."""
    
    def __init__(self, socket_path: Path, state_engine: StateEngine, config_manager: ConfigManager):
        """Initialize IPC server."""
        self.socket_path = Path(socket_path)
        self.state_engine = state_engine
        self.config_manager = config_manager
        self.server: Optional[asyncio.Server] = None
        
    async def start(self):
        """Start the IPC server."""
        # Remove existing socket if it exists
        if self.socket_path.exists():
            self.socket_path.unlink()
            
        # Ensure socket directory exists
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )
        
        # Set socket permissions
        os.chmod(self.socket_path, 0o666)
        
        logger.info(f"IPC server listening on {self.socket_path}")
        
        async with self.server:
            await self.server.serve_forever()
            
    async def stop(self):
        """Stop the IPC server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            
        if self.socket_path.exists():
            self.socket_path.unlink()
            
        logger.info("IPC server stopped")
        
    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle individual client connections."""
        client_addr = writer.get_extra_info('peername')
        
        # Get peer credentials to identify the user
        try:
            # peercred is a tuple of (pid, uid, gid)
            creds = writer.get_extra_info('peercred')
            client_uid = creds[1] if creds else None
        except Exception as e:
            logger.warning(f"Failed to get peer credentials: {e}")
            client_uid = None
            
        logger.debug(f"Client connected: {client_addr}, UID: {client_uid}")
        
        try:
            # Read request
            data = await reader.read(65536)  # 64KB max request size
            if not data:
                return
                
            request = json.loads(data.decode())
            logger.debug(f"Received request: {request.get('command')}")
            
            # Process request with authorization check
            response = await self._process_request(request, client_uid)
            
            # Send response
            response_data = json.dumps(response).encode()
            writer.write(response_data)
            await writer.drain()
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON request: {e}")
            error_response = {
                "success": False,
                "error": "Invalid JSON request",
            }
            writer.write(json.dumps(error_response).encode())
            await writer.drain()
            
        except Exception as e:
            logger.error(f"Request handling error: {e}", exc_info=True)
            error_response = {
                "success": False,
                "error": str(e),
            }
            writer.write(json.dumps(error_response).encode())
            await writer.drain()
            
        finally:
            writer.close()
            await writer.wait_closed()
            
    async def _process_request(self, request: Dict[str, Any], client_uid: Optional[int]) -> Dict[str, Any]:
        """Process IPC request and return response."""
        command = request.get("command")
        args = request.get("args", {})
        
        # Check authorization for privileged commands
        if command in PRIVILEGED_COMMANDS:
            if client_uid != 0:
                logger.warning(f"Denied privileged command '{command}' for non-root user (UID: {client_uid})")
                return {
                    "success": False,
                    "error": "Permission denied: Root privileges required",
                }
        
        handlers = {
            "status": self._handle_status,
            "list": self._handle_list,
            "spawn": self._handle_spawn,
            "stop": self._handle_stop,
            "start": self._handle_start,
            "remove": self._handle_remove,
            "exec": self._handle_exec,
            "reconcile": self._handle_reconcile,
            "reload": self._handle_reload,
            "image_pull": self._handle_image_pull,
            "validate": self._handle_validate,
        }
        
        handler = handlers.get(command)
        if not handler:
            return {
                "success": False,
                "error": f"Unknown command: {command}",
            }
            
        try:
            result = await handler(args)
            return {
                "success": True,
                "data": result,
            }
        except Exception as e:
            logger.error(f"Command {command} failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }
            
    async def _handle_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle status request."""
        container_name = args.get("container")
        
        if container_name:
            status = await self.state_engine.get_container_status(container_name)
            if not status:
                raise ValueError(f"Container {container_name} not found")
            return {"containers": {container_name: status}}
        else:
            # Return overall status
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
        """Handle list request."""
        resource_type = args.get("type", "all")
        
        result = {}
        
        if resource_type in ["all", "images"]:
            result["images"] = {
                name: {
                    "name": name,
                    "type": spec.type,
                    "source": spec.source,
                    "verify": spec.verify,
                }
                for name, spec in self.config_manager.images.items()
            }
            
        if resource_type in ["all", "containers"]:
            containers = await self.state_engine.get_all_container_statuses()
            result["containers"] = containers
            
        if resource_type in ["all", "profiles"]:
            result["profiles"] = {
                name: {
                    "name": name,
                    "has_nspawn_config": bool(spec.nspawn_config_content),
                    "has_systemd_override": bool(spec.systemd_override_content),
                }
                for name, spec in self.config_manager.profiles.items()
            }
            
        return result
        
    async def _handle_spawn(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle spawn (create) request."""
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
        """Handle stop request."""
        container_name = args.get("name")
        if not container_name:
            raise ValueError("Container name required")
            
        await self.state_engine.stop_container(container_name)
        return {"container": container_name, "stopped": True}
        
    async def _handle_start(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle start request."""
        container_name = args.get("name")
        if not container_name:
            raise ValueError("Container name required")
            
        await self.state_engine.start_container(container_name)
        return {"container": container_name, "started": True}
        
    async def _handle_remove(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle remove request."""
        container_name = args.get("name")
        if not container_name:
            raise ValueError("Container name required")
            
        await self.state_engine.remove_container(container_name)
        return {"container": container_name, "removed": True}
        
    async def _handle_exec(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle exec request."""
        container_name = args.get("name")
        command = args.get("command")
        
        if not container_name:
            raise ValueError("Container name required")
        if not command:
            raise ValueError("Command required")
            
        result = await self.state_engine.execute_in_container(container_name, command)
        return result
        
    async def _handle_reconcile(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle reconcile request."""
        await self.state_engine.reconcile()
        return {"reconciled": True}
        
    async def _handle_reload(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle reload configuration request."""
        await self.config_manager.load()
        return {"reloaded": True}
        
    async def _handle_image_pull(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle image pull request."""
        image_name = args.get("name")
        if not image_name:
            raise ValueError("Image name required")
            
        image_spec = self.config_manager.get_image_spec(image_name)
        if not image_spec:
            raise ValueError(f"Image {image_name} not found in configuration")
            
        image_provider = self.state_engine.provider_registry.get_provider("image")
        if not image_provider:
            raise RuntimeError("Image provider not available")
            
        await image_provider.present(image_spec)
        return {"image": image_name, "pulled": True}
        
    async def _handle_validate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Handle configuration validation request."""
        # Reload configuration to validate
        try:
            await self.config_manager.load()
            return {
                "valid": True,
                "images": len(self.config_manager.images),
                "profiles": len(self.config_manager.profiles),
                "containers": len(self.config_manager.containers),
            }
        except Exception as e:
            return {
                "valid": False,
                "error": str(e),
            }
