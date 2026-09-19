"""
Image specification models.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from typing import Literal

from pydantic import Field

from chimera.pydantic_compat import ForbidExtraModel, IgnoreExtraModel, validated_field


class CustomFileSpec(ForbidExtraModel):
    """Custom file modification specification."""

    path: str = Field(..., description="File path relative to container root")
    ensure: Literal["present", "absent", "link"] = Field(...)
    target: str | None = Field(None, description="Target for symbolic links")

    @validated_field("path")
    @classmethod
    def validate_relative_container_path(cls, value: str) -> str:
        """Keep custom-file operations within a container root filesystem."""
        if (
            not value
            or value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("must be a relative container path with a leaf and without traversal")
        return value


class ImageSpec(IgnoreExtraModel):
    """Image specification."""

    name: str = Field(..., description="Image name")
    type: Literal["tar", "raw"] = Field(..., description="Image type")
    verify: Literal["signature", "checksum", "no"] = Field(default="signature")
    source: str = Field(..., description="Image source URL")
    custom_files: list[CustomFileSpec] = Field(default_factory=list)
    nspawn_parameters: list[str] = Field(
        default_factory=list,
        description="Extra systemd-nspawn kernel command-line tokens for [Exec] Parameters=",
    )

    @validated_field("nspawn_parameters")
    @classmethod
    def validate_nspawn_parameters(cls, value: list[str]) -> list[str]:
        """Keep extra nspawn parameters as single kernel command-line tokens."""
        if not isinstance(value, list):
            raise ValueError("nspawn_parameters must be a list of kernel command-line tokens")
        tokens: list[str] = []
        for item in value:
            if (
                not isinstance(item, str)
                or not item
                or any(character.isspace() for character in item)
            ):
                raise ValueError(
                    "nspawn_parameters entries must be single kernel command-line tokens"
                )
            tokens.append(item)
        return tokens
