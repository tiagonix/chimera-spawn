"""Runtime path resolution for installed execution.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_DIR = Path("/etc/chimera-spawn")
DEFAULT_STATE_DIR = Path("/var/lib/chimera-spawn")
DEFAULT_SOCKET_PATH = Path("/run/chimera-spawn/server.sock")
DEFAULT_CATALOG_DIR = Path("/usr/share/chimera-spawn/catalog")


@dataclass(frozen=True)
class RuntimePaths:
    """Filesystem locations used by a Chimera server and its clients.

    FHS defaults apply when no path is configured. Explicit command-line and
    environment overrides support configured deployments without depending on
    the caller's current working directory.
    """

    config_dir: Path
    state_dir: Path
    socket_path: Path
    catalog_dir: Path

    @property
    def store_path(self) -> Path:
        """Return the durable managed-container registry location."""
        return self.state_dir / "state.json"

    @property
    def lock_path(self) -> Path:
        """Return the process-level state-authority lock pathname."""
        return self.state_dir / "server.lock"


def _path(value: str | None, fallback: Path) -> Path:
    """Expand a supplied path without resolving away a user-provided symlink."""
    if not value:
        return fallback
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def resolve_runtime_paths(
    *,
    config_dir: str | None = None,
    state_dir: str | None = None,
    socket_path: str | None = None,
    catalog_dir: str | None = None,
) -> RuntimePaths:
    """Resolve runtime locations with explicit options before environment values."""
    resolved_state_dir = _path(state_dir or os.getenv("CHIMERA_STATE_DIR"), DEFAULT_STATE_DIR)
    resolved_socket_path = _path(socket_path or os.getenv("CHIMERA_SOCKET"), DEFAULT_SOCKET_PATH)
    return RuntimePaths(
        config_dir=_path(config_dir or os.getenv("CHIMERA_CONFIG_DIR"), DEFAULT_CONFIG_DIR),
        state_dir=resolved_state_dir,
        socket_path=resolved_socket_path,
        catalog_dir=_path(catalog_dir or os.getenv("CHIMERA_CATALOG_DIR"), DEFAULT_CATALOG_DIR),
    )
