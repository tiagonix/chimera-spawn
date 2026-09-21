"""Server-side image resolution for SimpleStreams sources.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from chimera.images.identity import (
    EffectiveImage,
    cache_image_name,
    effective_from_source,
    is_generated_image_cache_name,
    is_image_cache_name,
    native_debian_architecture,
    require_unreserved_public_name,
)
from chimera.images.reference import ImageResolution, resolve_image_reference
from chimera.images.resolver import (
    ResolvedImageArtifact,
    download_verified_artifact,
    resolve_image_artifact,
)

__all__ = [
    "EffectiveImage",
    "ImageResolution",
    "ResolvedImageArtifact",
    "cache_image_name",
    "download_verified_artifact",
    "effective_from_source",
    "is_generated_image_cache_name",
    "is_image_cache_name",
    "native_debian_architecture",
    "require_unreserved_public_name",
    "resolve_image_artifact",
    "resolve_image_reference",
]
