"""Process-level exclusive ownership of one ContainerStore state directory.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path

from chimera.errors import ChimeraError


class StateLock:
    """Hold a kernel advisory exclusive lock for the server lifetime.

    Ownership is the flock on the open descriptor, not a PID file. The lock
    pathname is not unlinked on release, so cooperating processes keep locking
    the same inode.
    """

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "server.lock"
        self._fd: int | None = None
        self._dir_fd: int | None = None
        self.directory_identity: tuple[int, int] | None = None

    @property
    def directory_fd(self) -> int | None:
        """Return the locked state-directory descriptor while the lock is held."""
        return self._dir_fd

    def acquire(self) -> None:
        """Take exclusive nonblocking ownership of a verified state directory."""
        if self._fd is not None:
            return
        state_dir = self.path.parent
        self._ensure_state_directory(state_dir)
        try:
            dir_stat = os.lstat(state_dir)
        except FileNotFoundError as error:
            raise ChimeraError(
                code="invalid_configuration",
                message="The Chimera state path disappeared before it could be locked.",
                detail=str(state_dir),
                status=500,
            ) from error
        if stat.S_ISLNK(dir_stat.st_mode):
            raise ChimeraError(
                code="invalid_configuration",
                message="Refusing to follow a symlink at the Chimera state directory.",
                detail=str(state_dir),
                status=500,
            )
        try:
            dir_fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ChimeraError(
                    code="invalid_configuration",
                    message="Refusing to follow a symlink at the Chimera state directory.",
                    detail=str(state_dir),
                    status=500,
                ) from error
            raise
        try:
            dir_stat = os.fstat(dir_fd)
            if not stat.S_ISDIR(dir_stat.st_mode):
                raise ChimeraError(
                    code="invalid_configuration",
                    message="The Chimera state path is not a directory.",
                    detail=str(state_dir),
                    status=500,
                )
            try:
                fd = os.open(
                    "server.lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=dir_fd,
                )
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise ChimeraError(
                        code="invalid_configuration",
                        message="Refusing to follow a symlink at the Chimera state lock path.",
                        detail=str(self.path),
                        status=500,
                    ) from error
                raise
        except Exception:
            os.close(dir_fd)
            raise
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ChimeraError(
                    code="invalid_configuration",
                    message="The Chimera state lock must be a regular file.",
                    detail=str(self.path),
                    status=500,
                )
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(fd)
            os.close(dir_fd)
            raise ChimeraError(
                code="already_running",
                message="Another Chimera server already owns this state directory.",
                detail=str(self.path),
                suggestion=(
                    "Stop the running server or choose a different --state-dir. "
                    "A second process cannot share ContainerStore authority."
                ),
                status=409,
            ) from error
        except Exception:
            os.close(fd)
            os.close(dir_fd)
            raise
        self._fd = fd
        self._dir_fd = dir_fd
        self.directory_identity = (dir_stat.st_dev, dir_stat.st_ino)

    def release(self) -> None:
        """Drop the kernel lock and close descriptors without unlinking the file."""
        fd = self._fd
        dir_fd = self._dir_fd
        self._fd = None
        self._dir_fd = None
        try:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            if dir_fd is not None:
                os.close(dir_fd)

    def held(self) -> bool:
        """Report whether this object currently owns the lock descriptor."""
        return self._fd is not None

    @staticmethod
    def _ensure_state_directory(state_dir: Path) -> None:
        """Create a real directory without chmod'ing through a symlink."""
        try:
            os.mkdir(state_dir, 0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise ChimeraError(
                code="state_write_failed",
                message="Could not create the Chimera state directory.",
                detail=str(error),
                status=500,
            ) from error
