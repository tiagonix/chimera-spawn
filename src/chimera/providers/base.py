"""Base provider interface."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any
from pydantic import BaseModel


class ProviderStatus(Enum):
    """Provider resource status."""
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    ERROR = "error"


class BaseProvider(ABC):
    """Base provider interface that all providers must implement."""
    
    @abstractmethod
    async def initialize(self, config: Any):
        """Initialize the provider with configuration."""
        pass
        
    @abstractmethod
    async def status(self, spec: BaseModel) -> ProviderStatus:
        """Check the current status of a resource."""
        pass
        
    @abstractmethod
    async def present(self, spec: BaseModel) -> None:
        """Ensure the resource is present."""
        pass
        
    @abstractmethod
    async def absent(self, spec: BaseModel) -> None:
        """Ensure the resource is absent."""
        pass
        
    @abstractmethod
    async def validate_spec(self, spec: BaseModel) -> bool:
        """Validate the resource specification."""
        pass
