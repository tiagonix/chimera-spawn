"""IPC client for communicating with agent."""

import json
import socket
from pathlib import Path
from typing import Dict, Any, Optional


class IPCError(Exception):
    """IPC communication error."""
    pass


class IPCClient:
    """Client for communicating with agent via Unix socket."""
    
    def __init__(self, socket_path: Optional[str] = None):
        """Initialize IPC client."""
        if socket_path:
            self.socket_path = Path(socket_path)
        else:
            # Default to state directory socket
            self.socket_path = Path("./state/chimera-agent.sock")
            
    def request(self, command: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send request to agent and return response."""
        if not self.socket_path.exists():
            raise IPCError(
                f"Agent socket not found at {self.socket_path}. "
                "Is the agent running?"
            )
            
        # Prepare request
        request = {
            "command": command,
            "args": args or {},
        }
        
        # Connect to socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self.socket_path))
            
            # Send request
            request_data = json.dumps(request).encode()
            sock.sendall(request_data)
            sock.shutdown(socket.SHUT_WR)
            
            # Read response
            response_data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response_data += chunk
                
            # Parse response
            if not response_data:
                raise IPCError("Empty response from agent")
                
            response = json.loads(response_data.decode())
            
            if not response.get("success"):
                error = response.get("error", "Unknown error")
                raise IPCError(f"Agent error: {error}")
                
            return response.get("data", {})
            
        except socket.error as e:
            raise IPCError(f"Socket error: {e}")
        except json.JSONDecodeError as e:
            raise IPCError(f"Invalid response from agent: {e}")
        finally:
            sock.close()
