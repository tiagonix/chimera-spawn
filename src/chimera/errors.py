"""Stable errors shared by the server transport and command-line client.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ChimeraError(Exception):
    """An expected failure with a machine-readable code and operator remedy."""

    code: str
    message: str
    detail: str | None = None
    suggestion: str | None = None
    status: int = 400

    def __post_init__(self) -> None:
        """Populate Exception.args so normal exception rendering remains useful."""
        Exception.__init__(self, self.message)

    def as_dict(self) -> dict[str, Any]:
        """Return the stable error payload exposed by the API."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        return payload


class StoreCorruptionError(ChimeraError):
    """The durable store cannot be safely read or replaced."""

    def __init__(self, detail: str):
        super().__init__(
            code="state_corrupt",
            message="The managed-container registry is invalid and was left unchanged.",
            detail=detail,
            suggestion="Restore the registry from backup or correct it before retrying.",
            status=500,
        )
