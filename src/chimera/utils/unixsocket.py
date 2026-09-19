"""Unix-domain socket liveness probes and exclusive pathname reservation.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import errno
import fcntl
import os
import socket
import stat
from pathlib import Path
from typing import Literal

from chimera.errors import ChimeraError

UnixSocketState = Literal["absent", "not_socket", "stale", "live", "permission_denied"]


def inspect_unix_socket(path: Path) -> UnixSocketState:
    """Classify a candidate Unix socket without resolving or unlinking it."""
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except PermissionError:
        return "permission_denied"
    if stat.S_ISLNK(path_stat.st_mode):
        return "not_socket"
    if not stat.S_ISSOCK(path_stat.st_mode):
        return "not_socket"
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(os.fspath(path))
        return "live"
    except ConnectionRefusedError:
        return "stale"
    except FileNotFoundError:
        return "stale"
    except PermissionError:
        return "permission_denied"
    except OSError:
        # Fail closed: an indeterminate connect error may still be a live listener.
        return "live"
    finally:
        probe.close()


def socket_identity(path: Path) -> tuple[int, int] | None:
    """Return device/inode of a socket pathname without following a symlink."""
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISSOCK(path_stat.st_mode):
        return None
    return (path_stat.st_dev, path_stat.st_ino)


class SocketPathLock:
    """Serialize cooperating starters that choose the same socket pathname."""

    def __init__(self, socket_path: Path):
        self.socket_path = Path(socket_path)
        self.lock_path = self.socket_path.parent / f".{self.socket_path.name}.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take exclusive ownership of the socket pathname reservation."""
        if self._fd is not None:
            return
        parent = self.socket_path.parent
        try:
            os.mkdir(parent, 0o750)
        except FileExistsError:
            pass
        try:
            parent_stat = os.lstat(parent)
        except FileNotFoundError as error:
            raise ChimeraError(
                code="invalid_socket_path",
                message="The Unix socket parent directory disappeared before it could be reserved.",
                detail=str(parent),
                status=409,
            ) from error
        if stat.S_ISLNK(parent_stat.st_mode):
            raise ChimeraError(
                code="invalid_socket_path",
                message="Refusing to follow a symlink at the Unix socket parent directory.",
                detail=str(parent),
                status=409,
            )
        try:
            dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ChimeraError(
                    code="invalid_socket_path",
                    message="Refusing to follow a symlink at the Unix socket parent directory.",
                    detail=str(parent),
                    status=409,
                ) from error
            raise
        try:
            fd = os.open(
                self.lock_path.name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dir_fd,
            )
        except OSError as error:
            os.close(dir_fd)
            if error.errno == errno.ELOOP:
                raise ChimeraError(
                    code="invalid_socket_path",
                    message="Refusing to follow a symlink at the Unix socket lock path.",
                    detail=str(self.lock_path),
                    status=409,
                ) from error
            raise
        os.close(dir_fd)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(fd)
            raise ChimeraError(
                code="already_running",
                message=(f"Another process is starting or listening at '{self.socket_path}'."),
                suggestion="Stop the running server or choose a different --socket path.",
                status=409,
            ) from error
        except OSError:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        """Drop the reservation without unlinking the lock file."""
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
