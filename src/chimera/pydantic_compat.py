"""Small Pydantic 1/2 compatibility layer for supported Debian-family releases.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

try:
    from pydantic import ConfigDict, field_validator

    PYDANTIC_V2 = True
except ImportError:
    from pydantic import validator

    PYDANTIC_V2 = False


def validated_field(*fields: str) -> Callable[[Any], Any]:
    """Return the appropriate Pydantic 1/2 field validator decorator."""
    if PYDANTIC_V2:
        return field_validator(*fields)
    return validator(*fields)


class IgnoreExtraModel(BaseModel):
    """A model that accepts forward-compatible catalog fields."""

    if PYDANTIC_V2:
        model_config = ConfigDict(extra="ignore")
    else:

        class Config:
            extra = "ignore"


class AllowExtraModel(BaseModel):
    """A model that permits cloud-init template merge fields."""

    if PYDANTIC_V2:
        model_config = ConfigDict(extra="allow")
    else:

        class Config:
            extra = "allow"


class ForbidExtraModel(BaseModel):
    """A model that rejects unknown durable registry fields."""

    if PYDANTIC_V2:
        model_config = ConfigDict(extra="forbid")
    else:

        class Config:
            extra = "forbid"


def model_copy[ModelT: BaseModel](model: ModelT, *, deep: bool = False) -> ModelT:
    """Copy a model without Pydantic 2 deprecation warnings."""
    if PYDANTIC_V2:
        return model.model_copy(deep=deep)
    return model.copy(deep=deep)


def model_dump(model: BaseModel, **kwargs: Any) -> dict[str, Any]:
    """Return a Python-compatible model representation across Pydantic versions."""
    if PYDANTIC_V2:
        return model.model_dump(**kwargs)
    return model.dict(**kwargs)


def model_dump_json(model: BaseModel, **kwargs: Any) -> str:
    """Return JSON from a model across Pydantic versions."""
    if PYDANTIC_V2:
        return model.model_dump_json(**kwargs)
    return model.json(**kwargs)


def model_validate[ModelT: BaseModel](model_type: type[ModelT], value: Any) -> ModelT:
    """Build a model from parsed external data across Pydantic versions."""
    if PYDANTIC_V2:
        return model_type.model_validate(value)
    return model_type.parse_obj(value)
