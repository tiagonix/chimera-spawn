"""SimpleStreams resolution, artifact-kind, and signed-metadata contracts."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from chimera.errors import ChimeraError
from chimera.images.simplestreams import fetch_json, https_origin, resolve_simplestreams_artifact
from chimera.models.image import ImageSourceSpec

OLD_SHA256 = "aa" * 32
NEW_SHA256 = "bb" * 32
DISK_SHA256 = "cc" * 32

INDEX = {
    "format": "index:1.0",
    "index": {
        "downloads": {
            "datatype": "image-downloads",
            "path": "streams/v1/products.json",
        }
    },
}

PRODUCT = {
    "arch": "amd64",
    "release": "noble",
    "version": "24.04",
    "aliases": "24.04,noble",
    "versions": {
        "20240202.9": {
            "items": {
                "root.tar.xz": {
                    "ftype": "root.tar.xz",
                    "path": "images/old/root.tar.xz",
                    "sha256": OLD_SHA256,
                    "size": 10,
                }
            }
        },
        "20240202.10": {
            "items": {
                "root.tar.xz": {
                    "ftype": "root.tar.xz",
                    "path": "images/new/root.tar.xz",
                    "sha256": NEW_SHA256,
                    "size": 20,
                }
            }
        },
    },
}


def _products(products: dict[str, Any]) -> dict[str, Any]:
    return {"format": "products:1.0", "products": products}


def _source() -> ImageSourceSpec:
    return ImageSourceSpec(
        name="example",
        url="https://images.example/releases/",
        metadata_verify="tls",
    )


def _signed_source(keyring: str) -> ImageSourceSpec:
    return ImageSourceSpec(
        name="ubuntu",
        url="https://cloud-images.example/releases/",
        metadata_verify="signature",
        keyring=keyring,
    )


async def _fetch_fixture(url: str, *, source: ImageSourceSpec) -> object:
    if url.endswith("streams/v1/index.json"):
        return INDEX
    if url.endswith("streams/v1/products.json"):
        return _products({"example:server:24.04:amd64": PRODUCT})
    raise AssertionError(f"unexpected metadata URL {url}")


@pytest.mark.asyncio
async def test_simplestreams_selects_newest_root_tar():
    """Source-published aliases resolve the newest supported rootfs tar."""
    with patch("chimera.images.simplestreams.fetch_json", side_effect=_fetch_fixture):
        artifact = await resolve_simplestreams_artifact(_source(), "24.04", architecture="amd64")

    assert artifact.url == "https://images.example/releases/images/new/root.tar.xz"
    assert artifact.sha256 == NEW_SHA256
    assert artifact.size == 20
    assert artifact.serial == "20240202.10"
    assert artifact.product == "example:server:24.04:amd64"
    assert artifact.architecture == "amd64"
    assert artifact.ftype == "root.tar.xz"
    assert artifact.artifact_kind == "rootfs"
    assert artifact.source == "example"


@pytest.mark.asyncio
async def test_exact_product_key_wins_over_alias_match():
    """An exact canonical product key is selected without alias guessing."""
    with patch("chimera.images.simplestreams.fetch_json", side_effect=_fetch_fixture):
        artifact = await resolve_simplestreams_artifact(
            _source(), "example:server:24.04:amd64", architecture="amd64"
        )
    assert artifact.product == "example:server:24.04:amd64"
    assert artifact.serial == "20240202.10"


@pytest.mark.parametrize(
    "products",
    [
        {
            "example:server:24.04:amd64": {**PRODUCT, "variant": "default"},
            "example:minimal:24.04:amd64": {**PRODUCT, "variant": "cloud"},
        },
        {
            "example:minimal:24.04:amd64": {**PRODUCT, "variant": "cloud"},
            "example:server:24.04:amd64": {**PRODUCT, "variant": "default"},
        },
    ],
)
@pytest.mark.asyncio
async def test_ambiguous_product_selection_fails(products: dict[str, Any]):
    """Matching more than one product fails independently of dictionary order."""

    async def fetch(url: str, *, source: ImageSourceSpec) -> object:
        if url.endswith("streams/v1/index.json"):
            return INDEX
        return _products(products)

    with (
        patch("chimera.images.simplestreams.fetch_json", side_effect=fetch),
        pytest.raises(ChimeraError, match="multiple SimpleStreams products"),
    ):
        await resolve_simplestreams_artifact(_source(), "24.04", architecture="amd64")


@pytest.mark.asyncio
async def test_unpublished_release_is_not_a_reference():
    """Generic release/version metadata is not treated as a published alias."""
    product = {
        **PRODUCT,
        "aliases": "ubuntu/24.04/cloud",
        "release": "noble",
        "version": "24.04",
        "variant": "cloud",
    }

    async def fetch(url: str, *, source: ImageSourceSpec) -> object:
        if url.endswith("streams/v1/index.json"):
            return INDEX
        return _products({"example:server:24.04:amd64": product})

    with patch("chimera.images.simplestreams.fetch_json", side_effect=fetch):
        artifact = await resolve_simplestreams_artifact(
            _source(), "ubuntu/24.04/cloud", architecture="amd64"
        )
        assert artifact.product == "example:server:24.04:amd64"
        with pytest.raises(ChimeraError, match="No SimpleStreams product"):
            await resolve_simplestreams_artifact(_source(), "noble", architecture="amd64")
        with pytest.raises(ChimeraError, match="No SimpleStreams product"):
            await resolve_simplestreams_artifact(_source(), "24.04", architecture="amd64")


@pytest.mark.asyncio
async def test_squashfs_is_selected_when_no_root_tar():
    """Generic SimpleStreams may publish squashfs container rootfs instead of tar."""
    squashfs_product = {
        "arch": "amd64",
        "release": "9",
        "variant": "default",
        "aliases": "rockylinux/9/default,rockylinux/9",
        "versions": {
            "20260917_0805": {
                "items": {
                    "lxd.tar.xz": {
                        "ftype": "lxd.tar.xz",
                        "path": "images/meta/lxd.tar.xz",
                        "sha256": OLD_SHA256,
                        "size": 10,
                    },
                    "rootfs.squashfs": {
                        "ftype": "squashfs",
                        "path": "images/new/rootfs.squashfs",
                        "sha256": NEW_SHA256,
                        "size": 30,
                    },
                }
            }
        },
    }
    both_product = {
        **PRODUCT,
        "versions": {
            "20240202.10": {
                "items": {
                    **PRODUCT["versions"]["20240202.10"]["items"],
                    "rootfs.squashfs": {
                        "ftype": "squashfs",
                        "path": "images/ignored/rootfs.squashfs",
                        "sha256": OLD_SHA256,
                        "size": 5,
                    },
                }
            }
        },
    }

    async def fetch_squashfs(url: str, *, source: ImageSourceSpec) -> object:
        if url.endswith("streams/v1/index.json"):
            return INDEX
        return _products({"images:rockylinux:9:amd64:default": squashfs_product})

    async def fetch_both(url: str, *, source: ImageSourceSpec) -> object:
        if url.endswith("streams/v1/index.json"):
            return INDEX
        return _products({"example:server:24.04:amd64": both_product})

    with patch("chimera.images.simplestreams.fetch_json", side_effect=fetch_squashfs):
        artifact = await resolve_simplestreams_artifact(
            _source(), "rockylinux/9", architecture="amd64"
        )
    assert artifact.url.endswith("/rootfs.squashfs")
    assert artifact.ftype == "squashfs"
    assert artifact.artifact_kind == "rootfs"
    assert artifact.sha256 == NEW_SHA256

    with patch("chimera.images.simplestreams.fetch_json", side_effect=fetch_both):
        tar = await resolve_simplestreams_artifact(_source(), "24.04", architecture="amd64")
    assert tar.ftype == "root.tar.xz"


@pytest.mark.asyncio
async def test_disk_selects_root_disk_and_ignores_uefi():
    """Disk materialization uses a published root-disk ftype, not ancillary VM images."""
    product = {
        **PRODUCT,
        "versions": {
            "20240202.10": {
                "items": {
                    "uefi1.img": {
                        "ftype": "uefi1.img",
                        "path": "images/uefi.img",
                        "sha256": OLD_SHA256,
                        "size": 4,
                    },
                    "disk1.img": {
                        "ftype": "disk1.img",
                        "path": "images/disk1.img",
                        "sha256": DISK_SHA256,
                        "size": 40,
                    },
                    "lxd.tar.xz": {
                        "ftype": "lxd.tar.xz",
                        "path": "images/lxd.tar.xz",
                        "sha256": OLD_SHA256,
                        "size": 3,
                    },
                }
            }
        },
    }

    async def fetch(url: str, *, source: ImageSourceSpec) -> object:
        if url.endswith("streams/v1/index.json"):
            return INDEX
        return _products({"example:server:24.04:amd64": product})

    with patch("chimera.images.simplestreams.fetch_json", side_effect=fetch):
        artifact = await resolve_simplestreams_artifact(
            _source(), "24.04", architecture="amd64", artifact_kind="disk"
        )
    assert artifact.ftype == "disk1.img"
    assert artifact.artifact_kind == "disk"
    assert artifact.sha256 == DISK_SHA256
    with patch("chimera.images.simplestreams.fetch_json", side_effect=fetch):
        with pytest.raises(ChimeraError, match="rootfs"):
            await resolve_simplestreams_artifact(
                _source(), "24.04", architecture="amd64", artifact_kind="rootfs"
            )


@pytest.mark.asyncio
async def test_disk_kvm_is_preferred_over_disk1():
    """disk-kvm.img is selected before disk1.img when both are published."""
    product = {
        **PRODUCT,
        "versions": {
            "20240202.10": {
                "items": {
                    "disk1.img": {
                        "ftype": "disk1.img",
                        "path": "images/disk1.img",
                        "sha256": OLD_SHA256,
                        "size": 10,
                    },
                    "disk-kvm.img": {
                        "ftype": "disk-kvm.img",
                        "path": "images/disk-kvm.img",
                        "sha256": DISK_SHA256,
                        "size": 50,
                    },
                }
            }
        },
    }

    async def fetch(url: str, *, source: ImageSourceSpec) -> object:
        if url.endswith("streams/v1/index.json"):
            return INDEX
        return _products({"example:server:24.04:amd64": product})

    with patch("chimera.images.simplestreams.fetch_json", side_effect=fetch):
        artifact = await resolve_simplestreams_artifact(
            _source(), "24.04", architecture="amd64", artifact_kind="disk"
        )
    assert artifact.ftype == "disk-kvm.img"
    assert artifact.url.endswith("/disk-kvm.img")


@pytest.mark.asyncio
async def test_version_selection_is_requested_kind_aware():
    """Newest serial is chosen among versions that actually publish the requested kind."""
    product = {
        **PRODUCT,
        "versions": {
            "20240202.9": {
                "items": {
                    "root.tar.xz": {
                        "ftype": "root.tar.xz",
                        "path": "images/old/root.tar.xz",
                        "sha256": OLD_SHA256,
                        "size": 10,
                    }
                }
            },
            "20240202.10": {
                "items": {
                    "disk-kvm.img": {
                        "ftype": "disk-kvm.img",
                        "path": "images/new/disk-kvm.img",
                        "sha256": DISK_SHA256,
                        "size": 40,
                    }
                }
            },
        },
    }

    async def fetch(url: str, *, source: ImageSourceSpec) -> object:
        if url.endswith("streams/v1/index.json"):
            return INDEX
        return _products({"example:server:24.04:amd64": product})

    with patch("chimera.images.simplestreams.fetch_json", side_effect=fetch):
        rootfs = await resolve_simplestreams_artifact(
            _source(), "24.04", architecture="amd64", artifact_kind="rootfs"
        )
        disk = await resolve_simplestreams_artifact(
            _source(), "24.04", architecture="amd64", artifact_kind="disk"
        )
    assert rootfs.serial == "20240202.9"
    assert rootfs.ftype == "root.tar.xz"
    assert disk.serial == "20240202.10"
    assert disk.ftype == "disk-kvm.img"


@pytest.mark.asyncio
async def test_product_ambiguity_is_evaluated_after_kind_filter():
    """A disk-only sibling does not make a rootfs request ambiguous."""
    disk_only = {
        **PRODUCT,
        "variant": "disk",
        "versions": {
            "20240202.10": {
                "items": {
                    "disk-kvm.img": {
                        "ftype": "disk-kvm.img",
                        "path": "images/disk.img",
                        "sha256": DISK_SHA256,
                        "size": 40,
                    }
                }
            }
        },
    }
    products = {
        "example:server:24.04:amd64": {**PRODUCT, "variant": "default"},
        "example:disk:24.04:amd64": disk_only,
    }

    async def fetch(url: str, *, source: ImageSourceSpec) -> object:
        if url.endswith("streams/v1/index.json"):
            return INDEX
        return _products(products)

    with patch("chimera.images.simplestreams.fetch_json", side_effect=fetch):
        artifact = await resolve_simplestreams_artifact(
            _source(), "24.04", architecture="amd64", artifact_kind="rootfs"
        )
    assert artifact.product == "example:server:24.04:amd64"
    with patch("chimera.images.simplestreams.fetch_json", side_effect=fetch):
        disk = await resolve_simplestreams_artifact(
            _source(), "24.04", architecture="amd64", artifact_kind="disk"
        )
    assert disk.product == "example:disk:24.04:amd64"


@pytest.mark.parametrize(
    "url",
    [
        "https://images.example:abc/",
        "https://images.example:443abc/streams/v1/index.json",
    ],
)
def test_malformed_port_is_stable_chimera_error(url: str):
    """Malformed port syntax stays inside the SimpleStreams ChimeraError boundary."""
    with pytest.raises(ChimeraError, match="malformed host or port") as caught:
        https_origin(url)
    assert caught.value.code == "image_source_invalid"


@pytest.mark.asyncio
async def test_fetch_rejects_cross_origin_redirect():
    """Redirect hops must remain inside the configured ImageSource origin."""

    class FakeResponse:
        def __init__(self, status: int, url: str, location: str | None = None) -> None:
            self.status = status
            self.url = url
            self.headers = {"Location": location} if location else {}
            self.closed = False

        def release(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class FakeSession:
        async def get(self, url, timeout=None, allow_redirects=False):
            return FakeResponse(302, url, "https://evil.example/streams/v1/index.json")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    with (
        patch("chimera.images.simplestreams.aiohttp.ClientSession", return_value=FakeSession()),
        pytest.raises(ChimeraError, match="outside the configured image source origin"),
    ):
        await fetch_json(
            "https://images.example/releases/streams/v1/index.json",
            source=_source(),
        )


@pytest.mark.asyncio
async def test_signature_mode_requests_detached_signature_and_invokes_gpgv(tmp_path):
    """Signed sources fetch sibling .gpg bytes and parse JSON only after gpgv succeeds."""
    keyring = tmp_path / "trusted.gpg"
    keyring.write_bytes(b"trusted")
    fetched: list[str] = []

    async def fake_bytes(
        url: str, *, source_base: str, kind: str, missing_message: str | None = None
    ):
        fetched.append(url)
        if url.endswith(".gpg"):
            return b"SIG"
        return b'{"ok": true}'

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "gpgv"
        assert "--keyring" in cmd
        assert str(keyring) in cmd
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    with (
        patch("chimera.images.simplestreams.fetch_metadata_bytes", side_effect=fake_bytes),
        patch("chimera.images.simplestreams.subprocess.run", side_effect=fake_run) as run,
    ):
        payload = await fetch_json(
            "https://cloud-images.example/releases/streams/v1/index.json",
            source=_signed_source(str(keyring)),
        )
    assert payload == {"ok": True}
    assert fetched == [
        "https://cloud-images.example/releases/streams/v1/index.json",
        "https://cloud-images.example/releases/streams/v1/index.json.gpg",
    ]
    assert run.call_count == 1


@pytest.mark.asyncio
async def test_failed_signature_rejects_metadata(tmp_path):
    """A non-zero gpgv result never yields parsed metadata."""
    keyring = tmp_path / "trusted.gpg"
    keyring.write_bytes(b"trusted")

    async def fake_bytes(
        url: str, *, source_base: str, kind: str, missing_message: str | None = None
    ):
        return b"SIG" if url.endswith(".gpg") else b'{"ok": true}'

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, b"", b"BADSIG")

    with (
        patch("chimera.images.simplestreams.fetch_metadata_bytes", side_effect=fake_bytes),
        patch("chimera.images.simplestreams.subprocess.run", side_effect=fake_run),
        pytest.raises(ChimeraError, match="signature verification failed"),
    ):
        await fetch_json(
            "https://cloud-images.example/releases/streams/v1/index.json",
            source=_signed_source(str(keyring)),
        )


@pytest.mark.asyncio
async def test_missing_signature_rejects_metadata(tmp_path):
    """Signed sources fail closed when the detached signature is absent."""
    keyring = tmp_path / "trusted.gpg"
    keyring.write_bytes(b"trusted")

    async def fake_bytes(
        url: str, *, source_base: str, kind: str, missing_message: str | None = None
    ):
        if url.endswith(".gpg"):
            raise ChimeraError(
                code="image_source_invalid",
                message=missing_message or "missing",
                status=502,
            )
        return b'{"ok": true}'

    with (
        patch("chimera.images.simplestreams.fetch_metadata_bytes", side_effect=fake_bytes),
        patch("chimera.images.simplestreams.subprocess.run") as run,
        pytest.raises(ChimeraError, match="detached signature is missing"),
    ):
        await fetch_json(
            "https://cloud-images.example/releases/streams/v1/index.json",
            source=_signed_source(str(keyring)),
        )
    run.assert_not_called()


@pytest.mark.asyncio
async def test_missing_keyring_rejects_signed_source(tmp_path):
    """A configured keyring path must exist before gpgv is invoked."""
    keyring = tmp_path / "missing.gpg"
    source = _signed_source(str(keyring))

    async def fake_bytes(
        url: str, *, source_base: str, kind: str, missing_message: str | None = None
    ):
        return b"SIG" if url.endswith(".gpg") else b'{"ok": true}'

    with (
        patch("chimera.images.simplestreams.fetch_metadata_bytes", side_effect=fake_bytes),
        patch("chimera.images.simplestreams.subprocess.run") as run,
        pytest.raises(ChimeraError, match="keyring"),
    ):
        await fetch_json(
            "https://cloud-images.example/releases/streams/v1/index.json",
            source=source,
        )
    run.assert_not_called()


@pytest.mark.asyncio
async def test_tls_mode_does_not_invoke_gpgv():
    """TLS metadata trust parses JSON without a detached signature."""
    called = False

    async def fake_bytes(
        url: str, *, source_base: str, kind: str, missing_message: str | None = None
    ):
        nonlocal called
        if url.endswith(".gpg"):
            called = True
        return json.dumps(INDEX).encode()

    with patch("chimera.images.simplestreams.fetch_metadata_bytes", side_effect=fake_bytes):
        payload = await fetch_json(
            "https://images.example/releases/streams/v1/index.json",
            source=_source(),
        )
    assert payload["format"] == "index:1.0"
    assert called is False
