"""Classify host-command outcomes without treating subsystem failures as absence.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import errno
from typing import Literal

ObservationClass = Literal["absent", "present", "error"]

_TRANSPORT_MARKERS = (
    "connection refused",
    "connection reset",
    "broken pipe",
    "transport endpoint",
    "failed to connect",
    "could not connect",
    "no such file or directory",
    "cannot connect to bus",
    "failed to get d-bus",
    "dbus",
    "timed out",
    "timeout",
    "permission denied",
    "access denied",
    "not permitted",
    "operation not permitted",
    "interactive authentication required",
)

_EXECUTABLE_MARKERS = (
    "no such file or directory",
    "not found",
    "command not found",
)


def classify_command_observation(
    *,
    returncode: int | None,
    stdout: str = "",
    stderr: str = "",
    executable: str | None = None,
    error: BaseException | None = None,
) -> ObservationClass:
    """Return present/absent/error for one host query.

    Successful inventory or show commands are present. A clean zero-result
    inventory is absent. Transport, permission, timeout, and missing-executable
    failures are observation errors and must not authorize mutation.
    """
    if error is not None:
        if isinstance(error, FileNotFoundError):
            return "error"
        if isinstance(error, TimeoutError):
            return "error"
        if isinstance(error, PermissionError):
            return "error"
        if isinstance(error, OSError) and error.errno in {
            errno.EACCES,
            errno.EPERM,
            errno.EAGAIN,
            errno.EIO,
            errno.ENOENT,
            errno.ETIMEDOUT,
        }:
            return "error"
        text = str(error).lower()
        if any(marker in text for marker in _TRANSPORT_MARKERS):
            return "error"

    combined = f"{stdout}\n{stderr}".lower()
    if returncode not in {0, None} and _looks_like_transport_failure(combined, executable):
        return "error"
    if returncode == 0:
        return "present"
    return "error"


def _looks_like_transport_failure(combined: str, executable: str | None) -> bool:
    """Detect bus, permission, timeout, and executable failures."""
    if any(marker in combined for marker in _TRANSPORT_MARKERS):
        if executable and f"{executable.lower()}: not found" in combined:
            return True
        if "permission" in combined or "denied" in combined:
            return True
        if "timed out" in combined or "timeout" in combined:
            return True
        if "dbus" in combined or "connect" in combined or "bus" in combined:
            return True
        if "no such file or directory" in combined:
            return True
    if executable and any(marker in combined for marker in _EXECUTABLE_MARKERS):
        if executable.lower() in combined:
            return True
    return "permission denied" in combined or "access denied" in combined


def known_named_absence(stderr: str, *, kind: Literal["machine", "image"], name: str) -> bool:
    """Accept only a named machinectl absence diagnostic for this object."""
    message = stderr.strip()
    if not message:
        return False
    lowered = message.lower()
    if _looks_like_transport_failure(lowered, "machinectl"):
        return False
    quoted = f"'{name}'".lower()
    double = f'"{name}"'.lower()
    if kind == "machine":
        return (
            f"no machine {quoted}" in lowered
            or f"no machine {double}" in lowered
            or f"machine {quoted} not found" in lowered
        )
    return (
        f"no image {quoted}" in lowered
        or f"no image {double}" in lowered
        or f"image {quoted} not found" in lowered
    )
