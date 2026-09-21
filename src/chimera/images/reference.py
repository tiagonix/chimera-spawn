"""Server-side resolution of user image references.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chimera.errors import ChimeraError
from chimera.images.identity import (
    EffectiveImage,
    effective_from_source,
    native_debian_architecture,
)
from chimera.images.resolver import ResolvedImageArtifact, resolve_image_artifact
from chimera.images.simplestreams import (
    available_artifact_kinds,
    list_native_products,
    load_simplestreams_products,
    published_references,
    select_product,
)
from chimera.models.image import ArtifactKind, DEFAULT_ARTIFACT_KIND, ImageSourceSpec

if TYPE_CHECKING:
    from chimera.server.config import ConfigManager

ProductsLoader = Callable[[ImageSourceSpec], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class ImageResolution:
    """Canonical resolution of one user image reference."""

    effective: EffectiveImage
    requested_reference: str
    explicit_source: str | None
    aliases: tuple[str, ...] = ()
    release: str | None = None
    version: str | None = None
    architecture: str | None = None
    variant: str | None = None
    artifact_kinds: tuple[ArtifactKind, ...] = ()


async def resolve_image_reference(
    config: ConfigManager,
    reference: str,
    *,
    explicit_source: str | None = None,
    artifact_kind: ArtifactKind = DEFAULT_ARTIFACT_KIND,
    load_products: ProductsLoader | None = None,
) -> ImageResolution:
    """Resolve a user image reference against configured SimpleStreams sources."""
    if not reference:
        raise ChimeraError(
            code="invalid_argument",
            message="Image reference is required.",
            status=400,
        )
    loader = load_products or load_simplestreams_products
    if explicit_source is None:
        return await _discover_sources(
            config, reference, loader=loader, artifact_kind=artifact_kind
        )
    return await _resolve_explicit_source(
        config, reference, explicit_source, loader=loader, artifact_kind=artifact_kind
    )


async def resolve_current_artifact(
    config: ConfigManager,
    resolution: ImageResolution,
) -> ResolvedImageArtifact:
    """Resolve current source artifact metadata for an already-canonical product."""
    source = _require_source(config, resolution.effective.source_name)
    architecture = resolution.architecture or native_debian_architecture()
    return await resolve_image_artifact(
        source,
        resolution.effective.canonical_image_id,
        architecture=architecture,
        artifact_kind=resolution.effective.artifact_kind,
    )


async def list_source_products(
    config: ConfigManager,
    source_name: str,
    *,
    load_products: ProductsLoader | None = None,
) -> list[dict[str, Any]]:
    """List native-architecture products from one configured SimpleStreams source."""
    source = _require_source(config, source_name)
    architecture = native_debian_architecture()
    loader = load_products or load_simplestreams_products
    products = await loader(source)
    rows: list[dict[str, Any]] = []
    for product_key, product in list_native_products(products, architecture, source.name):
        rows.append(
            {
                "source": source.name,
                "product": product_key,
                "references": list(published_references(product)),
                "release": (
                    product.get("release") if isinstance(product.get("release"), str) else None
                ),
                "version": (
                    product.get("version") if isinstance(product.get("version"), str) else None
                ),
                "architecture": product.get("arch"),
                "variant": (
                    product.get("variant") if isinstance(product.get("variant"), str) else None
                ),
                "artifacts": list(available_artifact_kinds(product)),
                "policy": "configured" if product_key in source.products else "none",
                "metadata_verify": source.metadata_verify,
            }
        )
    return rows


async def _resolve_explicit_source(
    config: ConfigManager,
    reference: str,
    source_name: str,
    *,
    loader: ProductsLoader,
    artifact_kind: ArtifactKind,
) -> ImageResolution:
    source = _require_source(config, source_name)
    architecture = native_debian_architecture()
    products = await loader(source)
    product_key, product = select_product(
        products, reference, architecture, source.name, artifact_kind=artifact_kind
    )
    return _source_resolution(
        config,
        source,
        product_key,
        product,
        reference,
        architecture,
        artifact_kind=artifact_kind,
        explicit_source=source_name,
    )


async def _discover_sources(
    config: ConfigManager,
    reference: str,
    *,
    loader: ProductsLoader,
    artifact_kind: ArtifactKind,
) -> ImageResolution:
    architecture = native_debian_architecture()
    matches: list[ImageResolution] = []
    failures: list[str] = []
    for source in config.image_sources.values():
        try:
            products = await loader(source)
            product_key, product = select_product(
                products, reference, architecture, source.name, artifact_kind=artifact_kind
            )
        except ChimeraError as error:
            if error.code == "image_not_found":
                continue
            if error.code == "ambiguous_image":
                raise
            failures.append(f"{source.name}: {error.message}")
            continue
        matches.append(
            _source_resolution(
                config,
                source,
                product_key,
                product,
                reference,
                architecture,
                artifact_kind=artifact_kind,
                explicit_source=None,
            )
        )
    if len(matches) > 1:
        names = ", ".join(item.effective.source_name for item in matches)
        raise ChimeraError(
            code="ambiguous_image",
            message=f"Image '{reference}' matches multiple image sources: {names}.",
            suggestion="Retry with --source naming the configured source to use.",
            status=409,
        )
    if failures:
        raise ChimeraError(
            code="image_source_unavailable",
            message=(
                f"Could not uniquely resolve image '{reference}' because source "
                "discovery is incomplete."
            ),
            detail="; ".join(failures),
            suggestion="Retry the request or specify --source explicitly.",
            status=502,
        )
    if not matches:
        raise ChimeraError(
            code="image_not_found",
            message=f"Image '{reference}' was not found in configured SimpleStreams sources.",
            suggestion="Run 'chimeractl image source list' then 'chimeractl image list --source SOURCE'.",
            status=404,
        )
    return matches[0]


def _source_resolution(
    config: ConfigManager,
    source: ImageSourceSpec,
    product_key: str,
    product: Mapping[str, Any],
    reference: str,
    architecture: str,
    *,
    artifact_kind: ArtifactKind,
    explicit_source: str | None,
) -> ImageResolution:
    policy = config.get_product_policy(source.name, product_key)
    return ImageResolution(
        effective=effective_from_source(source, product_key, policy, artifact_kind),
        requested_reference=reference,
        explicit_source=explicit_source,
        aliases=published_references(product),
        release=product.get("release") if isinstance(product.get("release"), str) else None,
        version=product.get("version") if isinstance(product.get("version"), str) else None,
        architecture=str(product.get("arch") or architecture),
        variant=product.get("variant") if isinstance(product.get("variant"), str) else None,
        artifact_kinds=available_artifact_kinds(product),
    )


def _require_source(config: ConfigManager, source_name: str) -> ImageSourceSpec:
    source = config.get_image_source_spec(source_name)
    if source is None:
        raise ChimeraError(
            code="invalid_argument",
            message=f"Image source '{source_name}' is not configured.",
            suggestion="Run 'chimeractl image source list' and use a configured source name.",
            status=404,
        )
    return source
