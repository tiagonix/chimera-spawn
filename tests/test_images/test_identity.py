"""Image-cache identity and native architecture contracts."""

from unittest.mock import patch

import pytest

import chimera.images.identity as identity
from chimera.errors import ChimeraError
from chimera.images.identity import (
    cache_image_name,
    effective_from_source,
    is_generated_image_cache_name,
    is_image_cache_name,
    native_debian_architecture,
    require_unreserved_public_name,
)
from chimera.models.image import (
    ImageProductPolicy,
    ImageSourceSpec,
    empty_product_policy,
)


def _source(name: str = "ubuntu") -> ImageSourceSpec:
    return ImageSourceSpec(
        name=name,
        url=f"https://{name}.example/releases/",
        metadata_verify="tls",
    )


def test_image_cache_namespace_is_prefix_reserved():
    """Public names may not enter the chimera-src- cache prefix."""
    assert is_image_cache_name("chimera-src-abcd")
    assert not is_image_cache_name("chimera-other")
    assert not is_image_cache_name("x-chimera-src-abcd")
    require_unreserved_public_name("ubuntu", kind="container")
    require_unreserved_public_name("not-chimera-src-demo", kind="container")
    with pytest.raises(ValueError, match="reserved prefix"):
        require_unreserved_public_name("chimera-src-abcd", kind="container")


def test_generated_cache_name_is_exact_digest_shape():
    """Unmanaged ownership uses the generated digest shape, not the reserved prefix."""
    generated = cache_image_name("ubuntu", "com.ubuntu.cloud:server:24.04:amd64")
    assert is_generated_image_cache_name(generated)
    assert generated.startswith("chimera-src-")
    assert len(generated) == len("chimera-src-") + 32
    assert not is_generated_image_cache_name("chimera-src-random-junk")
    assert not is_generated_image_cache_name("chimera-src-abcd")
    assert is_image_cache_name("chimera-src-random-junk")


def test_cache_identity_is_source_product_and_kind():
    """Cache identity is source, canonical product, and artifact kind."""
    left = cache_image_name("ubuntu", "com.ubuntu.cloud:server:24.04:amd64", "rootfs")
    right = cache_image_name("ubuntu", "com.ubuntu.cloud:server:24.04:amd64", "rootfs")
    assert left == right
    disk = cache_image_name("ubuntu", "com.ubuntu.cloud:server:24.04:amd64", "disk")
    assert disk != left
    other_source = cache_image_name("company", "com.ubuntu.cloud:server:24.04:amd64", "rootfs")
    assert other_source != left
    defaulted = cache_image_name("ubuntu", "com.ubuntu.cloud:server:24.04:amd64")
    assert defaulted == left


def test_missing_policy_is_empty():
    """An undeclared product still resolves with empty accommodations."""
    effective = effective_from_source(_source(), "other:product:amd64", None)
    assert effective.custom_files == ()
    assert effective.nspawn_parameters == ()
    assert effective.artifact_kind == "rootfs"
    assert empty_product_policy().custom_files == []


def test_policy_is_keyed_by_source_and_product():
    """Product policy is selected by source plus canonical product."""
    policy = ImageProductPolicy(nspawn_parameters=["fstab=no"])
    selected = effective_from_source(_source(), "com.ubuntu.cloud:server:24.04:amd64", policy)
    assert selected.nspawn_parameters == ("fstab=no",)
    other = effective_from_source(_source("company"), "com.ubuntu.cloud:server:24.04:amd64", None)
    assert other.nspawn_parameters == ()
    assert other.local_image_name != selected.local_image_name


def test_native_architecture_uses_dpkg_and_does_not_default_amd64():
    """Architecture discovery fails closed instead of guessing amd64."""
    identity._NATIVE_ARCHITECTURE = None
    with patch("chimera.images.identity.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(ChimeraError, match="dpkg"):
            native_debian_architecture()
    identity._NATIVE_ARCHITECTURE = None
