"""Verified SimpleStreams artifact download.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from chimera.errors import ChimeraError
from chimera.images.simplestreams import fetch_https_chunks, resolve_simplestreams_artifact
from chimera.models.image import ArtifactKind, DEFAULT_ARTIFACT_KIND, ImageSourceSpec

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=1800, connect=10, sock_read=60)
DOWNLOAD_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResolvedImageArtifact:
    """Transient resolved source artifact; not container desired state."""

    url: str
    sha256: str
    size: int | None
    serial: str
    product: str
    architecture: str
    source: str
    aliases: tuple[str, ...] = ()
    release: str | None = None
    version: str | None = None
    variant: str | None = None
    ftype: str = "root.tar.xz"
    artifact_kind: ArtifactKind = DEFAULT_ARTIFACT_KIND


async def resolve_image_artifact(
    source: ImageSourceSpec,
    reference: str,
    *,
    architecture: str,
    artifact_kind: ArtifactKind = DEFAULT_ARTIFACT_KIND,
) -> ResolvedImageArtifact:
    """Resolve the current SimpleStreams artifact for a source product and kind."""
    return await resolve_simplestreams_artifact(
        source, reference, architecture=architecture, artifact_kind=artifact_kind
    )


async def download_verified_artifact(
    artifact: ResolvedImageArtifact,
    destination: Path,
    *,
    source_base: str,
    chunks: AsyncIterator[bytes] | None = None,
) -> None:
    """Stream an artifact to disk and require advertised SHA-256 and size."""
    hasher = hashlib.sha256()
    size = 0
    iterator = (
        chunks
        if chunks is not None
        else fetch_https_chunks(
            artifact.url,
            source_base=source_base,
            timeout=DOWNLOAD_TIMEOUT,
            chunk_size=DOWNLOAD_CHUNK_BYTES,
        )
    )
    try:
        with destination.open("wb") as handle:
            async for chunk in iterator:
                handle.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
    except ChimeraError:
        _unlink_quietly(destination)
        raise
    except OSError as error:
        _unlink_quietly(destination)
        raise ChimeraError(
            code="image_source_unavailable",
            message="Could not write the downloaded image artifact.",
            detail=str(error),
            suggestion="Check free disk space on the server and retry the image pull.",
            status=502,
        ) from error

    computed = hasher.hexdigest()
    if computed != artifact.sha256:
        _unlink_quietly(destination)
        raise ChimeraError(
            code="image_integrity_failed",
            message=(f"Image artifact SHA-256 mismatch from image source '{artifact.source}'."),
            detail=f"expected={artifact.sha256} computed={computed}",
            suggestion="Retry the pull. If it persists, inspect the SimpleStreams source metadata.",
            status=502,
        )
    if artifact.size is not None and size != artifact.size:
        _unlink_quietly(destination)
        raise ChimeraError(
            code="image_integrity_failed",
            message=(f"Image artifact size mismatch from image source '{artifact.source}'."),
            detail=f"expected={artifact.size} computed={size}",
            suggestion="Retry the pull. If it persists, inspect the SimpleStreams source metadata.",
            status=502,
        )
    logger.debug(
        "Verified image artifact source=%s product=%s serial=%s bytes=%s",
        artifact.source,
        artifact.product,
        artifact.serial,
        size,
    )


def _unlink_quietly(path: Path) -> None:
    """Remove a partial download without masking the integrity failure."""
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Could not remove failed image download '%s': %s", path, error)
