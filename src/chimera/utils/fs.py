"""Descriptor-relative, no-follow filesystem helpers for container roots.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from chimera.errors import ChimeraError

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def split_contained_parts(relative_path: str) -> list[str]:
    """Split a container-relative path and reject empty, dot, and parent parts."""
    if relative_path.startswith("/") or relative_path.startswith("\\"):
        raise ChimeraError(
            code="invalid_configuration",
            message=f"Path '{relative_path}' is not container-relative.",
            status=422,
        )
    parts = relative_path.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ChimeraError(
            code="invalid_configuration",
            message=f"Path '{relative_path}' is not a contained relative path.",
            status=422,
        )
    return parts


def open_directory_nofollow(path: Path) -> int:
    """Open a directory without following a terminal symlink."""
    return os.open(path, _DIR_FLAGS)


def identity_for(path: Path, kind: str) -> str:
    """Return a stable materialization identity for a verified filesystem object."""
    path_stat = os.lstat(path)
    if stat.S_ISLNK(path_stat.st_mode):
        raise ChimeraError(
            code="host_observation_failed",
            message=f"Refusing to treat symlink '{path}' as a container materialization.",
            status=503,
        )
    return f"{kind}:{path_stat.st_dev}:{path_stat.st_ino}"


def classify_materialization_path(path: Path) -> str:
    """Classify one machines-directory candidate without following a leaf symlink."""
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "error"
    if stat.S_ISLNK(path_stat.st_mode):
        return "invalid"
    if stat.S_ISDIR(path_stat.st_mode):
        return "directory"
    if stat.S_ISREG(path_stat.st_mode):
        return "file"
    return "invalid"


def write_contained_text(
    root: Path,
    relative_path: str,
    content: str,
    *,
    mode: int = 0o644,
) -> None:
    """Create parent directories and write a file under root without following links."""
    parts = split_contained_parts(relative_path)
    flags = _DIR_FLAGS
    root_fd = open_directory_nofollow(root)
    owned = [root_fd]
    try:
        parent_fd = _ensure_contained_parents(root_fd, parts[:-1], owned, flags)
        leaf = parts[-1]
        _replace_non_directory_leaf(parent_fd, leaf)
        fd = os.open(
            leaf,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
            dir_fd=parent_fd,
        )
        try:
            os.fchmod(fd, mode)
            payload = content.encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        for fd in reversed(owned):
            with suppress(OSError):
                os.close(fd)


def _ensure_contained_parents(
    root_fd: int, parts: Sequence[str], owned: list[int], flags: int
) -> int:
    """Create or open intermediate directories, refusing intermediate symlinks."""
    parent_fd = root_fd
    for part in parts:
        try:
            entry_stat = os.lstat(part, dir_fd=parent_fd)
        except FileNotFoundError:
            os.mkdir(part, 0o755, dir_fd=parent_fd)
            entry_stat = os.lstat(part, dir_fd=parent_fd)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise ChimeraError(
                code="invalid_configuration",
                message="A contained path includes an intermediate symlink.",
                detail=part,
                suggestion="Remove the intermediate link; Chimera will not follow it.",
                status=422,
            )
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise ChimeraError(
                code="provisioning_failed",
                message=f"Contained parent '{part}' is not a directory.",
                status=502,
            )
        next_fd = os.open(part, flags, dir_fd=parent_fd)
        owned.append(next_fd)
        parent_fd = next_fd
    return parent_fd


def _replace_non_directory_leaf(parent_fd: int, leaf: str) -> None:
    """Unlink an existing non-directory leaf so a regular file can replace it."""
    try:
        leaf_stat = os.lstat(leaf, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(leaf_stat.st_mode):
        raise ChimeraError(
            code="provisioning_failed",
            message=f"Contained path leaf '{leaf}' is a directory.",
            status=502,
        )
    os.unlink(leaf, dir_fd=parent_fd)
