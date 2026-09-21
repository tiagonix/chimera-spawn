"""SimpleStreams image-downloads discovery and product selection.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp

from chimera.errors import ChimeraError
from chimera.models.image import ArtifactKind, DEFAULT_ARTIFACT_KIND, ImageSourceSpec

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from chimera.images.resolver import ResolvedImageArtifact

INDEX_RELATIVE_PATH = "streams/v1/index.json"
IMAGE_DOWNLOADS = "image-downloads"
SIMPLESTREAMS_ROOTFS_FTYPE = "root.tar.xz"
SIMPLESTREAMS_SQUASHFS_FTYPE = "squashfs"
DISK_FTYPE_PRIORITY = ("disk-kvm.img", "disk1.img")
GPGV_TIMEOUT = 15
METADATA_MAX_BYTES = 32 * 1024 * 1024
METADATA_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)
MAX_REDIRECTS = 10
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SERIAL_PARTS = re.compile(r"(\d+)")


async def load_simplestreams_products(source: ImageSourceSpec) -> Mapping[str, Any]:
    """Fetch the SimpleStreams products document for a configured source."""
    source_base = require_source_base(source)
    index_url = join_source_path(source_base, INDEX_RELATIVE_PATH, kind="index")
    index_document = await fetch_json(index_url, source=source)
    products_path = image_downloads_path(index_document, source.name)
    products_url = join_source_path(source_base, products_path, kind="products")
    payload = await fetch_json(products_url, source=source)
    return require_products_document(payload, source.name)


async def resolve_simplestreams_artifact(
    source: ImageSourceSpec,
    reference: str,
    *,
    architecture: str,
    artifact_kind: ArtifactKind = DEFAULT_ARTIFACT_KIND,
) -> ResolvedImageArtifact:
    """Resolve a SimpleStreams artifact of the requested kind from source metadata."""
    from chimera.images.resolver import ResolvedImageArtifact

    products = await load_simplestreams_products(source)
    product_key, product = select_product(
        products, reference, architecture, source.name, artifact_kind=artifact_kind
    )
    serial, version = select_newest_version(product, product_key, artifact_kind=artifact_kind)
    item_key, item = select_artifact_item(version, product_key, serial, artifact_kind=artifact_kind)
    artifact_path = require_relative_path(item.get("path"), kind="artifact")
    source_base = require_source_base(source)
    artifact_url = join_source_path(source_base, artifact_path, kind="artifact")
    digest = require_sha256(item.get("sha256"), product=product_key, item_key=item_key)
    size = optional_size(item.get("size"), product=product_key, item_key=item_key)
    return ResolvedImageArtifact(
        url=artifact_url,
        sha256=digest,
        size=size,
        serial=serial,
        product=product_key,
        architecture=str(product.get("arch") or architecture),
        source=source.name,
        aliases=published_references(product),
        release=_optional_string(product.get("release")),
        version=_optional_string(product.get("version")),
        variant=_optional_string(product.get("variant")),
        ftype=_artifact_ftype(item, item_key, artifact_kind),
        artifact_kind=artifact_kind,
    )


def require_source_base(source: ImageSourceSpec) -> str:
    """Return the configured SimpleStreams HTTPS base URL."""
    return source.url


def image_downloads_path(index_document: object, source_name: str) -> str:
    """Return the products document path from a SimpleStreams index."""
    payload = require_mapping(index_document, kind="index")
    fmt = payload.get("format")
    if fmt != "index:1.0":
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams index for source '{source_name}' is not format index:1.0.",
            suggestion="Point the image source at a current SimpleStreams v1 index.",
            status=502,
        )
    entries = require_mapping(payload.get("index"), kind="index.index")
    matches: list[str] = []
    for entry in entries.values():
        if not isinstance(entry, Mapping):
            raise ChimeraError(
                code="image_source_invalid",
                message=f"SimpleStreams index for source '{source_name}' has a malformed entry.",
                suggestion="Correct the source metadata or use another HTTPS SimpleStreams server.",
                status=502,
            )
        if entry.get("datatype") == IMAGE_DOWNLOADS:
            matches.append(require_relative_path(entry.get("path"), kind="products"))
    if not matches:
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams index for source '{source_name}' has no image-downloads entry.",
            suggestion="Use a SimpleStreams server that publishes datatype image-downloads.",
            status=502,
        )
    if len(matches) > 1:
        raise ChimeraError(
            code="image_source_invalid",
            message=(
                f"SimpleStreams index for source '{source_name}' has multiple "
                "image-downloads entries."
            ),
            suggestion="Point the image source at an index with a single image-downloads document.",
            status=502,
        )
    return matches[0]


def require_products_document(products_document: object, source_name: str) -> Mapping[str, Any]:
    """Return the products mapping from a SimpleStreams products:1.0 document."""
    payload = require_mapping(products_document, kind="products")
    fmt = payload.get("format")
    if fmt != "products:1.0":
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams products for source '{source_name}' are not format products:1.0.",
            suggestion="Point the image source at a current SimpleStreams products document.",
            status=502,
        )
    return require_mapping(payload.get("products"), kind="products.products")


def select_product(
    products: Mapping[str, Any],
    reference: str,
    architecture: str,
    source_name: str,
    *,
    artifact_kind: ArtifactKind = DEFAULT_ARTIFACT_KIND,
) -> tuple[str, Mapping[str, Any]]:
    """Select exactly one product for a reference, architecture, and artifact kind."""
    exact = products.get(reference)
    if exact is not None:
        if not isinstance(exact, Mapping):
            raise ChimeraError(
                code="image_source_invalid",
                message=f"SimpleStreams product '{reference}' is not a mapping.",
                suggestion="Correct the image source products document.",
                status=502,
            )
        _require_architecture(exact, architecture, reference)
        _require_kind(exact, reference, artifact_kind)
        return reference, exact

    matches: list[tuple[str, Mapping[str, Any]]] = []
    for key, product in products.items():
        if not isinstance(product, Mapping):
            raise ChimeraError(
                code="image_source_invalid",
                message=f"SimpleStreams product '{key}' is not a mapping.",
                suggestion="Correct the image source products document.",
                status=502,
            )
        if not product_matches_reference(product, reference):
            continue
        if not architecture_matches(product, architecture):
            continue
        if not product_has_artifact_kind(product, artifact_kind):
            continue
        matches.append((key, product))
    if not matches:
        raise ChimeraError(
            code="image_not_found",
            message=(
                f"No SimpleStreams product in source '{source_name}' matches "
                f"'{reference}' for architecture '{architecture}' and artifact '{artifact_kind}'."
            ),
            suggestion=_kind_suggestion(artifact_kind),
            status=404,
        )
    if len(matches) > 1:
        keys = ", ".join(key for key, _product in matches)
        raise ChimeraError(
            code="ambiguous_image",
            message=(
                f"Reference '{reference}' matches multiple SimpleStreams products "
                f"in source '{source_name}': {keys}."
            ),
            suggestion="Use an exact canonical product key instead of guessing.",
            status=409,
        )
    return matches[0]


def product_matches_reference(product: Mapping[str, Any], reference: str) -> bool:
    """Match a user reference against source-published aliases only."""
    return reference in published_references(product)


def published_references(product: Mapping[str, Any]) -> tuple[str, ...]:
    """Return unique source-published aliases in document order."""
    seen: set[str] = set()
    values: list[str] = []
    aliases = product.get("aliases")
    if isinstance(aliases, str):
        for item in aliases.split(","):
            alias = item.strip()
            if alias and alias not in seen:
                seen.add(alias)
                values.append(alias)
    return tuple(values)


def architecture_matches(product: Mapping[str, Any], architecture: str | None) -> bool:
    """Require an exact architecture match against the product arch field."""
    return architecture is not None and product.get("arch") == architecture


def list_native_products(
    products: Mapping[str, Any],
    architecture: str,
    _source_name: str,
) -> list[tuple[str, Mapping[str, Any]]]:
    """Return native-architecture products that publish a usable rootfs or disk artifact."""
    listed: list[tuple[str, Mapping[str, Any]]] = []
    for key, product in products.items():
        if not isinstance(product, Mapping):
            raise ChimeraError(
                code="image_source_invalid",
                message=f"SimpleStreams product '{key}' is not a mapping.",
                suggestion="Correct the image source products document.",
                status=502,
            )
        if not architecture_matches(product, architecture):
            continue
        if not available_artifact_kinds(product):
            continue
        listed.append((key, product))
    return listed


def _require_kind(
    product: Mapping[str, Any], product_key: str, artifact_kind: ArtifactKind
) -> None:
    if product_has_artifact_kind(product, artifact_kind):
        return
    raise ChimeraError(
        code="image_not_found",
        message=(
            f"SimpleStreams product '{product_key}' does not publish a usable "
            f"'{artifact_kind}' artifact."
        ),
        suggestion=_kind_suggestion(artifact_kind),
        status=404,
    )


def _kind_suggestion(artifact_kind: ArtifactKind) -> str:
    other = "disk" if artifact_kind == "rootfs" else "rootfs"
    return (
        "Use an exact product key, another source-published reference, --source, "
        f"or --artifact {other}."
    )


def _require_architecture(product: Mapping[str, Any], architecture: str, product_key: str) -> None:
    if not architecture_matches(product, architecture):
        raise ChimeraError(
            code="image_not_found",
            message=(
                f"SimpleStreams product '{product_key}' architecture "
                f"'{product.get('arch')}' does not match '{architecture}'."
            ),
            suggestion="Choose a product published for this server architecture.",
            status=404,
        )


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def serial_sort_key(serial: str) -> tuple[tuple[int, int | str], ...]:
    """Order SimpleStreams serials by numeric segments so .10 sorts after .9."""
    parts: list[tuple[int, int | str]] = []
    for part in _SERIAL_PARTS.split(serial):
        if not part:
            continue
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return tuple(parts)


def select_newest_version(
    product: Mapping[str, Any],
    product_key: str,
    *,
    artifact_kind: ArtifactKind = DEFAULT_ARTIFACT_KIND,
) -> tuple[str, Mapping[str, Any]]:
    """Select the newest version that publishes the requested artifact kind."""
    versions = product.get("versions")
    if not isinstance(versions, Mapping) or not versions:
        raise ChimeraError(
            code="image_not_found",
            message=f"SimpleStreams product '{product_key}' has no usable versions.",
            suggestion="Choose another release or wait until the image source publishes a build.",
            status=404,
        )
    usable: list[str] = []
    for serial, version in versions.items():
        if not isinstance(serial, str):
            raise ChimeraError(
                code="image_source_invalid",
                message=f"SimpleStreams product '{product_key}' has a non-string version key.",
                suggestion="Correct the image source products document.",
                status=502,
            )
        if not isinstance(version, Mapping):
            raise ChimeraError(
                code="image_source_invalid",
                message=f"SimpleStreams version '{serial}' on product '{product_key}' is malformed.",
                suggestion="Correct the image source products document.",
                status=502,
            )
        items = version.get("items")
        if not isinstance(items, Mapping):
            raise ChimeraError(
                code="image_source_invalid",
                message=(
                    f"SimpleStreams version '{serial}' on product '{product_key}' "
                    "has malformed items."
                ),
                suggestion="Correct the image source products document.",
                status=502,
            )
        try:
            if find_artifact_item(items, artifact_kind) is not None:
                usable.append(serial)
        except ChimeraError as error:
            if error.code == "ambiguous_image":
                raise
            raise ChimeraError(
                code=error.code,
                message=error.message,
                detail=error.detail,
                suggestion=error.suggestion,
                status=error.status,
            ) from error
    if not usable:
        raise ChimeraError(
            code="image_not_found",
            message=(
                f"SimpleStreams product '{product_key}' has no version containing "
                f"a usable '{artifact_kind}' artifact."
            ),
            suggestion=_kind_suggestion(artifact_kind),
            status=404,
        )
    serial = max(usable, key=serial_sort_key)
    version = versions[serial]
    if not isinstance(version, Mapping):
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams version '{serial}' on product '{product_key}' is malformed.",
            status=502,
        )
    return serial, version


def product_has_artifact_kind(product: Mapping[str, Any], artifact_kind: ArtifactKind) -> bool:
    """Return True when any advertised version publishes the requested kind."""
    try:
        select_newest_version(product, "probe", artifact_kind=artifact_kind)
    except ChimeraError as error:
        if error.code == "image_not_found":
            return False
        raise
    return True


def available_artifact_kinds(product: Mapping[str, Any]) -> tuple[ArtifactKind, ...]:
    """Return high-level artifact kinds present on a product, in stable order."""
    kinds: list[ArtifactKind] = []
    for kind in ("rootfs", "disk"):
        if product_has_artifact_kind(product, kind):
            kinds.append(kind)
    return tuple(kinds)


def select_artifact_item(
    version: Mapping[str, Any],
    product_key: str,
    serial: str,
    *,
    artifact_kind: ArtifactKind,
) -> tuple[str, Mapping[str, Any]]:
    """Select the supported artifact item of the requested kind from one version."""
    items = version.get("items")
    if not isinstance(items, Mapping):
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams version '{serial}' on product '{product_key}' has malformed items.",
            status=502,
        )
    found = find_artifact_item(items, artifact_kind)
    if found is None:
        raise ChimeraError(
            code="image_not_found",
            message=(
                f"SimpleStreams version '{serial}' of product '{product_key}' does not "
                f"contain a usable '{artifact_kind}' artifact."
            ),
            suggestion=_kind_suggestion(artifact_kind),
            status=404,
        )
    return found


def find_artifact_item(
    items: Mapping[str, Any], artifact_kind: ArtifactKind
) -> tuple[str, Mapping[str, Any]] | None:
    """Return the unique preferred item for a high-level artifact kind."""
    if artifact_kind == "rootfs":
        return find_rootfs_item(items)
    return find_disk_item(items)


def find_rootfs_item(items: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    """Prefer a rootfs tar, otherwise a squashfs container rootfs; never LXD metadata."""
    tar = _find_root_tar_item(items)
    if tar is not None:
        return tar
    return _require_unique_or_none(items, SIMPLESTREAMS_SQUASHFS_FTYPE)


def find_disk_item(items: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    """Select a root-disk image; never UEFI/firmware ancillary artifacts."""
    for ftype in DISK_FTYPE_PRIORITY:
        found = _require_unique_or_none(items, ftype)
        if found is not None:
            return found
    return None


def _find_root_tar_item(items: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    direct = items.get(SIMPLESTREAMS_ROOTFS_FTYPE)
    if isinstance(direct, Mapping) and _item_ftype_is_root_tar(direct):
        return SIMPLESTREAMS_ROOTFS_FTYPE, direct
    return _require_unique_or_none(items, SIMPLESTREAMS_ROOTFS_FTYPE)


def _require_unique_or_none(
    items: Mapping[str, Any], ftype: str
) -> tuple[str, Mapping[str, Any]] | None:
    matches: list[tuple[str, Mapping[str, Any]]] = []
    for key, item in items.items():
        if isinstance(item, Mapping) and item.get("ftype") == ftype:
            matches.append((key, item))
    if not matches:
        return None
    if len(matches) > 1:
        raise ChimeraError(
            code="ambiguous_image",
            message=f"SimpleStreams metadata lists multiple '{ftype}' artifacts.",
            suggestion="Use an exact product that publishes one supported artifact of this kind.",
            status=409,
        )
    return matches[0]


def _artifact_ftype(item: Mapping[str, Any], item_key: str, artifact_kind: ArtifactKind) -> str:
    ftype = item.get("ftype")
    if isinstance(ftype, str) and ftype:
        return ftype
    if artifact_kind == "disk":
        return item_key
    if item_key == SIMPLESTREAMS_ROOTFS_FTYPE:
        return SIMPLESTREAMS_ROOTFS_FTYPE
    return SIMPLESTREAMS_ROOTFS_FTYPE


def _item_ftype_is_root_tar(item: Mapping[str, Any]) -> bool:
    ftype = item.get("ftype")
    return ftype is None or ftype == SIMPLESTREAMS_ROOTFS_FTYPE


def require_sha256(value: object, *, product: str, item_key: str) -> str:
    """Require a 64-character hex SHA-256 digest from SimpleStreams metadata."""
    if not isinstance(value, str):
        raise ChimeraError(
            code="image_source_invalid",
            message=(
                f"SimpleStreams item '{item_key}' on product '{product}' has no sha256 digest."
            ),
            suggestion="Use a SimpleStreams server that advertises SHA-256 for the image artifact.",
            status=502,
        )
    digest = value.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ChimeraError(
            code="image_source_invalid",
            message=(
                f"SimpleStreams item '{item_key}' on product '{product}' has an invalid sha256."
            ),
            suggestion="Correct the image source products document.",
            status=502,
        )
    return digest


def optional_size(value: object, *, product: str, item_key: str) -> int | None:
    """Capture advertised size when present and well-typed."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChimeraError(
            code="image_source_invalid",
            message=(
                f"SimpleStreams item '{item_key}' on product '{product}' has an invalid size."
            ),
            suggestion="Correct the image source products document.",
            status=502,
        )
    return value


def require_mapping(value: object, *, kind: str) -> Mapping[str, Any]:
    """Require a JSON object at a named SimpleStreams location."""
    if not isinstance(value, Mapping):
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams {kind} metadata is not an object.",
            suggestion="Correct the image source metadata or use another HTTPS SimpleStreams server.",
            status=502,
        )
    return value


def require_relative_path(value: object, *, kind: str) -> str:
    """Require a relative metadata path with no scheme, traversal, query, or fragment."""
    if not isinstance(value, str) or not value.strip():
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams {kind} path is missing.",
            suggestion="Correct the image source metadata.",
            status=502,
        )
    path = value.strip()
    _reject_unsafe_path(path, kind=kind)
    _reject_unsafe_path(unquote(path), kind=kind)
    return path


def _reject_unsafe_path(path: str, *, kind: str) -> None:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams {kind} path must be relative to the configured image source URL.",
            suggestion="Image source metadata must not supply absolute URLs, queries, or fragments.",
            status=502,
        )
    if path.startswith("/") or "\\" in path:
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams {kind} path must be a relative path.",
            suggestion="Image source metadata must stay under the configured source URL.",
            status=502,
        )
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams {kind} path is not a contained relative path.",
            suggestion="Correct the image source metadata.",
            status=502,
        )


def join_source_path(base_url: str, relative_path: str, *, kind: str) -> str:
    """Resolve a relative SimpleStreams path against the configured source base."""
    resolved = urljoin(base_url, relative_path)
    return assert_https_url(resolved, kind=kind, source_base=base_url)


def https_origin(url: str) -> tuple[str, str, int]:
    """Return scheme, hostname, and effective port for HTTPS origin comparison."""
    parsed = urlsplit(url)
    try:
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as error:
        raise ChimeraError(
            code="image_source_invalid",
            message="SimpleStreams URL has a malformed host or port.",
            detail=str(error),
            suggestion="Correct the configured image source URL or metadata redirect Location.",
            status=502,
        ) from error
    scheme = parsed.scheme.lower()
    if port is None and scheme == "https":
        port = 443
    if port is None:
        port = -1
    return scheme, host, port


def assert_https_url(url: str, *, kind: str, source_base: str | None = None) -> str:
    """Require an HTTPS URL without credentials, optionally same-origin as the source."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams {kind} URL must use HTTPS.",
            suggestion="Configure an HTTPS image source.",
            status=502,
        )
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams {kind} URL must not contain credentials.",
            suggestion="Remove userinfo from the image source metadata path.",
            status=502,
        )
    _scheme, host, _port = https_origin(url)
    if not host:
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams {kind} URL must include a host.",
            status=502,
        )
    if source_base is not None and https_origin(url) != https_origin(source_base):
        raise ChimeraError(
            code="image_source_invalid",
            message=f"SimpleStreams {kind} URL is outside the configured image source origin.",
            suggestion="Image source metadata and redirects must stay under the configured source host.",
            status=502,
        )
    return url


async def fetch_json(url: str, *, source: ImageSourceSpec) -> object:
    """Fetch bounded SimpleStreams JSON, verifying a detached signature when required."""
    source_base = require_source_base(source)
    payload = await fetch_metadata_bytes(url, source_base=source_base, kind="metadata")
    if source.metadata_verify == "signature":
        if not source.keyring:
            raise ChimeraError(
                code="invalid_configuration",
                message=f"Image source '{source.name}' requires a trusted metadata keyring.",
                suggestion="Set keyring: to an absolute packaged keyring path.",
                status=422,
            )
        signature_url = f"{url}.gpg"
        signature = await fetch_metadata_bytes(
            signature_url,
            source_base=source_base,
            kind="signature",
            missing_message="SimpleStreams detached signature is missing.",
        )
        await asyncio.to_thread(
            verify_detached_signature,
            payload=payload,
            signature=signature,
            keyring=source.keyring,
        )
        logger.info(
            "Verified SimpleStreams metadata signature url=%s keyring=%s",
            url,
            source.keyring,
        )
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChimeraError(
            code="image_source_invalid",
            message="SimpleStreams metadata is not valid JSON.",
            detail=str(error),
            suggestion="Inspect the SimpleStreams source metadata and retry.",
            status=502,
        ) from error


async def fetch_metadata_bytes(
    url: str,
    *,
    source_base: str,
    kind: str,
    missing_message: str | None = None,
) -> bytes:
    """Fetch bounded SimpleStreams bytes over HTTPS within the configured source origin."""
    buffer = bytearray()
    try:
        async with aiohttp.ClientSession() as session:
            async with _open_within_origin(
                session, url, source_base=source_base, timeout=METADATA_TIMEOUT, kind=kind
            ) as response:
                if response.status != 200:
                    message = missing_message or (
                        f"SimpleStreams {kind} request failed with HTTP {response.status}."
                    )
                    raise ChimeraError(
                        code=(
                            "image_source_invalid"
                            if missing_message is not None
                            else "image_source_unavailable"
                        ),
                        message=message,
                        detail=f"HTTP {response.status}",
                        suggestion="Check image source connectivity and retry the image pull.",
                        status=502,
                    )
                async for chunk in response.content.iter_chunked(64 * 1024):
                    buffer.extend(chunk)
                    if len(buffer) > METADATA_MAX_BYTES:
                        raise ChimeraError(
                            code="image_source_invalid",
                            message=f"SimpleStreams {kind} exceeded the 32 MiB size limit.",
                            suggestion="Point the image source at a SimpleStreams server with bounded metadata.",
                            status=502,
                        )
    except ChimeraError:
        raise
    except TimeoutError as error:
        raise ChimeraError(
            code="image_source_unavailable",
            message=f"Timed out fetching SimpleStreams {kind}.",
            suggestion="Check image source connectivity and retry the image pull.",
            status=502,
        ) from error
    except aiohttp.ClientError as error:
        raise ChimeraError(
            code="image_source_unavailable",
            message=f"Could not fetch SimpleStreams {kind}.",
            detail=_safe_http_detail(error),
            suggestion="Check image source connectivity and retry the image pull.",
            status=502,
        ) from error
    return bytes(buffer)


def verify_detached_signature(*, payload: bytes, signature: bytes, keyring: str) -> None:
    """Verify SimpleStreams JSON against a detached signature and trusted keyring."""
    keyring_path = Path(keyring)
    if not keyring_path.is_file():
        raise ChimeraError(
            code="invalid_configuration",
            message=f"Trusted SimpleStreams keyring '{keyring}' is missing.",
            suggestion="Install ubuntu-keyring or correct the configured keyring path.",
            status=422,
        )
    with tempfile.TemporaryDirectory(prefix="chimera-ss-") as tmp:
        data_path = Path(tmp) / "metadata.json"
        sig_path = Path(tmp) / "metadata.json.gpg"
        data_path.write_bytes(payload)
        sig_path.write_bytes(signature)
        try:
            result = subprocess.run(
                ["gpgv", "--keyring", str(keyring_path), str(sig_path), str(data_path)],
                capture_output=True,
                timeout=GPGV_TIMEOUT,
            )
        except FileNotFoundError as error:
            raise ChimeraError(
                code="service_unavailable",
                message="gpgv is not installed; signed SimpleStreams metadata cannot be verified.",
                suggestion="Install the gpgv package on this Chimera server.",
                status=503,
            ) from error
        except subprocess.TimeoutExpired as error:
            raise ChimeraError(
                code="image_source_unavailable",
                message="Timed out verifying SimpleStreams metadata signature.",
                suggestion="Retry the request and inspect gpgv on the Chimera server.",
                status=502,
            ) from error
        except OSError as error:
            raise ChimeraError(
                code="image_source_unavailable",
                message="Could not invoke gpgv to verify SimpleStreams metadata.",
                detail=str(error),
                suggestion="Install gpgv and retry.",
                status=502,
            ) from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            if len(detail) > 300:
                detail = detail[:300]
            raise ChimeraError(
                code="image_source_invalid",
                message="SimpleStreams metadata signature verification failed.",
                detail=detail or f"gpgv exited {result.returncode}",
                suggestion="Use a signed SimpleStreams source and the packaged Ubuntu cloud-image keyring.",
                status=502,
            )


async def fetch_https_chunks(
    url: str,
    *,
    source_base: str,
    timeout: aiohttp.ClientTimeout,
    chunk_size: int,
) -> AsyncIterator[bytes]:
    """Stream an HTTPS artifact in bounded chunks within the configured source origin."""
    try:
        async with aiohttp.ClientSession() as session:
            async with _open_within_origin(
                session, url, source_base=source_base, timeout=timeout, kind="artifact"
            ) as response:
                if response.status != 200:
                    raise ChimeraError(
                        code="image_source_unavailable",
                        message=f"Image artifact download failed with HTTP {response.status}.",
                        suggestion="Check image source connectivity and retry the image pull.",
                        status=502,
                    )
                async for chunk in response.content.iter_chunked(chunk_size):
                    yield chunk
    except ChimeraError:
        raise
    except TimeoutError as error:
        raise ChimeraError(
            code="image_source_unavailable",
            message="Timed out downloading the image artifact.",
            suggestion="Retry the pull or increase --timeout for a large image artifact.",
            status=502,
        ) from error
    except aiohttp.ClientError as error:
        raise ChimeraError(
            code="image_source_unavailable",
            message="Could not download the image artifact.",
            detail=_safe_http_detail(error),
            suggestion="Check image source connectivity and retry the image pull.",
            status=502,
        ) from error


class _OriginCheckedResponse:
    """Async context manager that follows redirects only within source origin."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        source_base: str,
        timeout: aiohttp.ClientTimeout,
        kind: str,
    ) -> None:
        self._session = session
        self._url = url
        self._source_base = source_base
        self._timeout = timeout
        self._kind = kind
        self._response: aiohttp.ClientResponse | None = None

    async def __aenter__(self) -> aiohttp.ClientResponse:
        current = assert_https_url(self._url, kind=self._kind, source_base=self._source_base)
        for _ in range(MAX_REDIRECTS):
            response = await self._session.get(
                current, timeout=self._timeout, allow_redirects=False
            )
            if response.status in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                response_url = str(response.url)
                response.release()
                response.close()
                if not location:
                    raise ChimeraError(
                        code="image_source_invalid",
                        message=f"SimpleStreams {self._kind} redirect is missing a Location header.",
                        suggestion="Correct the image source HTTP server or metadata.",
                        status=502,
                    )
                current = urljoin(response_url, location)
                current = assert_https_url(current, kind=self._kind, source_base=self._source_base)
                continue
            assert_https_url(str(response.url), kind=self._kind, source_base=self._source_base)
            self._response = response
            return response
        raise ChimeraError(
            code="image_source_unavailable",
            message=f"SimpleStreams {self._kind} request exceeded {MAX_REDIRECTS} redirects.",
            suggestion="Correct the image source HTTP server or metadata.",
            status=502,
        )

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._response is not None and not self._response.closed:
            self._response.close()


def _open_within_origin(
    session: aiohttp.ClientSession,
    url: str,
    *,
    source_base: str,
    timeout: aiohttp.ClientTimeout,
    kind: str,
) -> _OriginCheckedResponse:
    """Open one HTTPS GET that remains inside the configured source origin."""
    return _OriginCheckedResponse(session, url, source_base=source_base, timeout=timeout, kind=kind)


def _safe_http_detail(error: BaseException) -> str:
    """Return a short connection failure without payload or credential material."""
    text = str(error).strip()
    if not text:
        return error.__class__.__name__
    if len(text) > 300:
        return text[:300]
    return text
