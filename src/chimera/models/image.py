"""
SimpleStreams image sources and local product policy.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field

from chimera.pydantic_compat import (
    ForbidExtraModel,
    PYDANTIC_V2,
    validated_field,
)

if PYDANTIC_V2:
    from pydantic import model_validator
else:
    from pydantic import root_validator

ArtifactKind = Literal["rootfs", "disk"]
MetadataVerify = Literal["signature", "tls"]
DEFAULT_ARTIFACT_KIND: ArtifactKind = "rootfs"


def normalize_https_source_url(url: str) -> str:
    """Accept only an administrator-configured HTTPS image-source base URL."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("source URL is required")
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("source URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("source URL must not contain credentials")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("source URL must include a host")
    if parsed.query:
        raise ValueError("source URL must not include a query string")
    if parsed.fragment:
        raise ValueError("source URL must not include a fragment")
    host = hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    path = parsed.path if parsed.path else "/"
    if not path.endswith("/"):
        path += "/"
    return urlunsplit(("https", netloc, path, "", ""))


def validate_nspawn_parameter_tokens(value: list[str]) -> list[str]:
    """Keep extra nspawn parameters as single kernel command-line tokens."""
    if not isinstance(value, list):
        raise ValueError("nspawn_parameters must be a list of kernel command-line tokens")
    tokens: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or any(character.isspace() for character in item):
            raise ValueError("nspawn_parameters entries must be single kernel command-line tokens")
        tokens.append(item)
    return tokens


def normalize_artifact_kind(value: object) -> ArtifactKind:
    """Accept only the two Chimera materialization kinds."""
    if value == "rootfs":
        return "rootfs"
    if value == "disk":
        return "disk"
    raise ValueError("artifact kind must be 'rootfs' or 'disk'")


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


class ImageProductPolicy(ForbidExtraModel):
    """Local custom files and nspawn parameters for one canonical product."""

    custom_files: list[CustomFileSpec] = Field(default_factory=list)
    nspawn_parameters: list[str] = Field(default_factory=list)

    @validated_field("nspawn_parameters")
    @classmethod
    def validate_nspawn_parameters(cls, value: list[str]) -> list[str]:
        """Keep extra nspawn parameters as single kernel command-line tokens."""
        return validate_nspawn_parameter_tokens(value)


class ImageSourceSpec(ForbidExtraModel):
    """Named SimpleStreams source plus local policy for its canonical products."""

    name: str = Field(..., description="Configured image source name")
    url: str = Field(..., description="HTTPS SimpleStreams base URL")
    metadata_verify: MetadataVerify = Field(..., description="Metadata authenticity policy")
    keyring: str | None = Field(None, description="Absolute trusted GnuPG keyring path")
    products: dict[str, ImageProductPolicy] = Field(default_factory=dict)

    @validated_field("name")
    @classmethod
    def validate_source_name(cls, value: str) -> str:
        """Require a non-empty configured source name."""
        if not value:
            raise ValueError("source name is required")
        return value

    @validated_field("url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        """Require a credential-free HTTPS SimpleStreams base URL."""
        return normalize_https_source_url(value)

    @validated_field("keyring")
    @classmethod
    def validate_keyring_path(cls, value: str | None) -> str | None:
        """Keep signature keyrings as absolute administrator-configured paths."""
        if value is None:
            return None
        if not value or any(character.isspace() for character in value):
            raise ValueError("keyring must be an absolute path")
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("keyring must be an absolute path")
        return str(path)

    if PYDANTIC_V2:

        @model_validator(mode="after")
        def validate_source(self) -> ImageSourceSpec:
            """Bind metadata trust and exact canonical product keys."""
            _require_metadata_trust(self.metadata_verify, self.keyring)
            _require_product_keys(self.products)
            return self

    else:

        @root_validator(skip_on_failure=True)
        def validate_source(cls, values: dict[str, Any]) -> dict[str, Any]:
            """Bind metadata trust and exact canonical product keys."""
            _require_metadata_trust(values.get("metadata_verify"), values.get("keyring"))
            _require_product_keys(values.get("products") or {})
            return values


def empty_product_policy() -> ImageProductPolicy:
    """Return the default empty policy for an undeclared source product."""
    return ImageProductPolicy()


def _require_metadata_trust(mode: object, keyring: object) -> None:
    if mode == "signature":
        if not isinstance(keyring, str) or not keyring:
            raise ValueError("metadata_verify=signature requires keyring")
        return
    if mode == "tls":
        if keyring is not None:
            raise ValueError("metadata_verify=tls must not set a keyring")
        return
    raise ValueError("metadata_verify must be 'signature' or 'tls'")


def _require_product_keys(products: object) -> None:
    if not isinstance(products, dict):
        raise ValueError("products must be a mapping")
    for key in products:
        if not isinstance(key, str) or not key:
            raise ValueError("product keys must be non-empty canonical product ids")
