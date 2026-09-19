"""
Base provider interface.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from chimera.providers.registry import ProviderRegistry


class ProviderStatus(Enum):
    """Provider resource status."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    ERROR = "error"


class BaseProvider[ProviderSpecT: BaseModel](ABC):
    """Base provider interface that all providers must implement."""

    @abstractmethod
    async def initialize(self, config: Any, registry: "ProviderRegistry") -> None:
        """Initialize the provider with configuration and registry."""
        raise NotImplementedError

    @abstractmethod
    async def status(self, spec: ProviderSpecT) -> ProviderStatus:
        """Check the current status of a resource."""
        raise NotImplementedError

    @abstractmethod
    async def present(self, spec: ProviderSpecT) -> None:
        """Ensure the resource is present."""
        raise NotImplementedError

    @abstractmethod
    async def absent(self, spec: ProviderSpecT) -> None:
        """Ensure the resource is absent."""
        raise NotImplementedError

    @abstractmethod
    async def validate_spec(self, spec: ProviderSpecT) -> bool:
        """Validate the resource specification."""
        raise NotImplementedError
