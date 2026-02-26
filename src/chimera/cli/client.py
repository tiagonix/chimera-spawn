"""HTTP/WebSocket client for communicating with agent."""

import json
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, AsyncContextManager
import httpx
import websockets
# NOTE: Using async websockets API for compatibility with Ubuntu 24.04 (websockets ~10.4).
# Sync API (websockets.sync) was added in 11.0. Consider upgrading when Ubuntu 26.04 ships.

class IPCError(Exception):
    """Communication error."""
    pass


class IPCClient:
    """Client for communicating with agent via Unix socket or TCP."""
    
    def __init__(self, socket_path: Optional[str] = None, host: Optional[str] = None):
        """Initialize IPC client."""
        self.socket_path = Path(socket_path) if socket_path else None
        self.host = host
        
        if not self.socket_path and not self.host:
            self.socket_path = Path("./state/chimera-agent.sock")
            
        if self.host:
            self.base_url = f"http://{self.host}"
            self.ws_base_url = f"ws://{self.host}"
            self.transport = None  # Default TCP transport
        else:
            # Use Unix socket
            self.base_url = "http://localhost"
            self.ws_base_url = "ws://localhost"
            self.transport = httpx.HTTPTransport(uds=str(self.socket_path))
            
    def request(self, command: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send REST request to agent."""
        if self.socket_path and not self.socket_path.exists() and not self.host:
            raise IPCError(f"Agent socket not found at {self.socket_path}")
            
        payload = {
            "command": command,
            "args": args or {}
        }
        
        try:
            with httpx.Client(transport=self.transport, base_url=self.base_url, timeout=10.0) as client:
                response = client.post("/api/v1/command", json=payload)
                response.raise_for_status()
                data = response.json()
                
                if not data.get("success"):
                    raise IPCError(f"Agent error: {data.get('error')}")
                    
                return data.get("data", {})
                
        except httpx.RequestError as e:
            raise IPCError(f"Connection error: {e}")
        except httpx.HTTPStatusError as e:
            raise IPCError(f"HTTP error {e.response.status_code}: {e.response.text}")

    def stream_connect(self, endpoint: str, params: Dict[str, Any]) -> AsyncContextManager:
        """Connect to WebSocket endpoint and return async connection context manager."""
        query = urllib.parse.urlencode(params)
        url = f"{self.ws_base_url}{endpoint}?{query}"
        
        if self.socket_path:
            # Unix socket connection using async API
            # websockets.unix_connect is available in older versions
            return websockets.unix_connect(str(self.socket_path), uri=url)
        else:
            # TCP connection using async API
            return websockets.connect(url)
