"""Transient effective-image identity and local cache naming.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from dataclasses import dataclass

from chimera.errors import ChimeraError
from chimera.models.image import (
    ArtifactKind,
    CustomFileSpec,
    DEFAULT_ARTIFACT_KIND,
    ImageProductPolicy,
    ImageSourceSpec,
    empty_product_policy,
    normalize_artifact_kind,
)

logger = logging.getLogger(__name__)

IMAGE_CACHE_PREFIX = "chimera-src-"
IMAGE_CACHE_DIGEST_LENGTH = 32
GENERATED_IMAGE_CACHE_PATTERN = re.compile(
    rf"^{re.escape(IMAGE_CACHE_PREFIX)}[0-9a-f]{{{IMAGE_CACHE_DIGEST_LENGTH}}}$"
)
_NATIVE_ARCHITECTURE: str | None = None


def is_image_cache_name(name: str) -> bool:
    """Return True when a public name uses the reserved image-cache prefix."""
    return isinstance(name, str) and name.startswith(IMAGE_CACHE_PREFIX)


def is_generated_image_cache_name(name: str) -> bool:
    """Return True for an exact Chimera-generated image-cache object name."""
    return isinstance(name, str) and GENERATED_IMAGE_CACHE_PATTERN.fullmatch(name) is not None


def require_unreserved_public_name(name: str, *, kind: str) -> str:
    """Reject operator-controlled names that would enter the image-cache namespace."""
    if is_image_cache_name(name):
        raise ValueError(f"{kind} name cannot use reserved prefix '{IMAGE_CACHE_PREFIX}'")
    return name


@dataclass(frozen=True, slots=True)
class EffectiveImage:
    """Transient base-image identity and policy used after request resolution."""

    source_name: str
    canonical_image_id: str
    artifact_kind: ArtifactKind
    local_image_name: str
    custom_files: tuple[CustomFileSpec, ...]
    nspawn_parameters: tuple[str, ...]
    source_spec: ImageSourceSpec

    @property
    def name(self) -> str:
        """Local machinectl image name used for observation and clone."""
        return self.local_image_name


def cache_image_name(
    source_name: str, canonical_product: str, artifact_kind: ArtifactKind = DEFAULT_ARTIFACT_KIND
) -> str:
    """Return a deterministic machinectl-safe name for source, product, and kind."""
    kind = normalize_artifact_kind(artifact_kind)
    digest = hashlib.sha256(f"{source_name}\0{canonical_product}\0{kind}".encode()).hexdigest()
    return f"{IMAGE_CACHE_PREFIX}{digest[:IMAGE_CACHE_DIGEST_LENGTH]}"


def native_debian_architecture() -> str:
    """Return the managed server's native Debian architecture, cached per process."""
    global _NATIVE_ARCHITECTURE
    if _NATIVE_ARCHITECTURE is not None:
        return _NATIVE_ARCHITECTURE
    try:
        result = subprocess.run(
            ["dpkg", "--print-architecture"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as error:
        raise ChimeraError(
            code="architecture_unavailable",
            message="Could not determine the server native architecture because dpkg is missing.",
            suggestion="Install dpkg on this Debian-family Chimera server and retry.",
            status=503,
        ) from error
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        raise ChimeraError(
            code="architecture_unavailable",
            message="Could not determine the server native architecture.",
            detail=str(error),
            suggestion="Ensure dpkg --print-architecture works on this Chimera server and retry.",
            status=503,
        ) from error
    architecture = result.stdout.strip()
    if not architecture or any(character.isspace() for character in architecture):
        raise ChimeraError(
            code="architecture_unavailable",
            message="The server native architecture could not be determined.",
            detail=repr(result.stdout),
            suggestion="Ensure dpkg --print-architecture reports a single architecture name.",
            status=503,
        )
    _NATIVE_ARCHITECTURE = architecture
    logger.debug("Native Debian architecture: %s", architecture)
    return architecture


def effective_from_source(
    source: ImageSourceSpec,
    canonical_product: str,
    policy: ImageProductPolicy | None,
    artifact_kind: ArtifactKind = DEFAULT_ARTIFACT_KIND,
) -> EffectiveImage:
    """Compose an effective image from a named source, product, and kind."""
    kind = normalize_artifact_kind(artifact_kind)
    selected = policy if policy is not None else empty_product_policy()
    return EffectiveImage(
        source_name=source.name,
        canonical_image_id=canonical_product,
        artifact_kind=kind,
        local_image_name=cache_image_name(source.name, canonical_product, kind),
        custom_files=tuple(selected.custom_files),
        nspawn_parameters=tuple(selected.nspawn_parameters),
        source_spec=source,
    )
