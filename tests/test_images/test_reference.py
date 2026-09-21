"""Unqualified SimpleStreams image-reference discovery contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from chimera.errors import ChimeraError
from chimera.images.reference import resolve_image_reference
from chimera.models.image import ImageSourceSpec, empty_product_policy

PRODUCT = {
    "arch": "amd64",
    "release": "resolute",
    "version": "26.04",
    "aliases": "26.04,resolute",
    "versions": {
        "20240101": {
            "items": {
                "root.tar.xz": {
                    "ftype": "root.tar.xz",
                    "path": "images/root.tar.xz",
                    "sha256": "aa" * 32,
                    "size": 1,
                }
            }
        }
    },
}


def _source(name: str) -> ImageSourceSpec:
    return ImageSourceSpec(
        name=name,
        url=f"https://{name}.example/releases/",
        metadata_verify="tls",
    )


def _config(*, sources: list[ImageSourceSpec] | None = None):
    source_map = {item.name: item for item in (sources or [])}
    manager = SimpleNamespace(
        image_sources=source_map,
    )
    manager.get_image_source_spec = source_map.get
    manager.get_product_policy = lambda source, product: empty_product_policy()
    return manager


@pytest.mark.asyncio
async def test_one_source_match_is_selected():
    """A unique source match is used without an explicit source."""

    async def loader(source: ImageSourceSpec) -> dict[str, Any]:
        if source.name == "images":
            return {
                "images:centos/10-Stream:amd64:cloud": {
                    **PRODUCT,
                    "aliases": "centos/10-Stream/cloud",
                    "release": "10-Stream",
                    "variant": "cloud",
                }
            }
        return {"com.ubuntu.cloud:server:26.04:amd64": PRODUCT}

    config = _config(sources=[_source("ubuntu"), _source("images")])
    with pytest.MonkeyPatch.context() as monkey:
        monkey.setattr("chimera.images.reference.native_debian_architecture", lambda: "amd64")
        ubuntu = await resolve_image_reference(config, "resolute", load_products=loader)
        images = await resolve_image_reference(
            config, "centos/10-Stream/cloud", load_products=loader
        )
    assert ubuntu.effective.source_name == "ubuntu"
    assert ubuntu.effective.canonical_image_id == "com.ubuntu.cloud:server:26.04:amd64"
    assert ubuntu.effective.artifact_kind == "rootfs"
    assert images.effective.source_name == "images"
    assert images.effective.canonical_image_id == "images:centos/10-Stream:amd64:cloud"
    assert ubuntu.effective.local_image_name != images.effective.local_image_name


@pytest.mark.asyncio
async def test_two_source_matches_are_ambiguous():
    """Implicit discovery cannot choose between two matching sources."""

    async def loader(source: ImageSourceSpec) -> dict[str, Any]:
        return {"com.ubuntu.cloud:server:26.04:amd64": PRODUCT}

    config = _config(sources=[_source("ubuntu"), _source("images")])
    with pytest.MonkeyPatch.context() as monkey:
        monkey.setattr("chimera.images.reference.native_debian_architecture", lambda: "amd64")
        with pytest.raises(ChimeraError, match="multiple image sources"):
            await resolve_image_reference(config, "resolute", load_products=loader)


@pytest.mark.asyncio
async def test_one_match_and_unavailable_source_is_incomplete():
    """A reachable match is not unique while another source cannot be queried."""

    async def loader(source: ImageSourceSpec) -> dict[str, Any]:
        if source.name == "images":
            raise ChimeraError(
                code="image_source_unavailable",
                message="down",
                status=502,
            )
        return {"com.ubuntu.cloud:server:26.04:amd64": PRODUCT}

    config = _config(sources=[_source("ubuntu"), _source("images")])
    with pytest.MonkeyPatch.context() as monkey:
        monkey.setattr("chimera.images.reference.native_debian_architecture", lambda: "amd64")
        with pytest.raises(ChimeraError, match="discovery is incomplete"):
            await resolve_image_reference(config, "resolute", load_products=loader)


@pytest.mark.asyncio
async def test_explicit_source_does_not_search_others():
    """--source ubuntu searches only that configured source."""

    async def loader(source: ImageSourceSpec) -> dict[str, Any]:
        assert source.name == "ubuntu"
        return {"com.ubuntu.cloud:server:26.04:amd64": PRODUCT}

    config = _config(sources=[_source("ubuntu"), _source("images")])
    with pytest.MonkeyPatch.context() as monkey:
        monkey.setattr("chimera.images.reference.native_debian_architecture", lambda: "amd64")
        resolution = await resolve_image_reference(
            config, "resolute", explicit_source="ubuntu", load_products=loader
        )
    assert resolution.effective.source_name == "ubuntu"
    assert resolution.effective.canonical_image_id == "com.ubuntu.cloud:server:26.04:amd64"


@pytest.mark.asyncio
async def test_unknown_explicit_source_fails():
    """An unknown --source name is an invalid argument."""
    config = _config()
    with pytest.raises(ChimeraError, match="is not configured"):
        await resolve_image_reference(config, "resolute", explicit_source="missing")


@pytest.mark.asyncio
async def test_artifact_kind_filters_cross_source_candidates():
    """A disk-only product does not make a rootfs request ambiguous."""
    disk_only = {
        **PRODUCT,
        "aliases": "shared",
        "versions": {
            "20240101": {
                "items": {
                    "disk-kvm.img": {
                        "ftype": "disk-kvm.img",
                        "path": "images/disk.qcow2",
                        "sha256": "cc" * 32,
                        "size": 2,
                    }
                }
            }
        },
    }

    async def loader(source: ImageSourceSpec) -> dict[str, Any]:
        if source.name == "images":
            return {"images:shared:amd64": disk_only}
        return {"com.ubuntu.cloud:server:26.04:amd64": {**PRODUCT, "aliases": "shared,resolute"}}

    config = _config(sources=[_source("ubuntu"), _source("images")])
    with pytest.MonkeyPatch.context() as monkey:
        monkey.setattr("chimera.images.reference.native_debian_architecture", lambda: "amd64")
        rootfs = await resolve_image_reference(
            config, "shared", artifact_kind="rootfs", load_products=loader
        )
        disk = await resolve_image_reference(
            config, "shared", artifact_kind="disk", load_products=loader
        )
    assert rootfs.effective.source_name == "ubuntu"
    assert disk.effective.source_name == "images"
