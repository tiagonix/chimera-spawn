"""Authorization and command-contract tests for the transport-independent service."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from chimera.errors import ChimeraError
from chimera.runtime import resolve_runtime_paths
from chimera.server.service import CommandService, PeerCredentials


@pytest.fixture
def service(tmp_path):
    """Create a minimal service with a non-root administrator group."""
    engine = Mock()
    engine.get_all_container_statuses = AsyncMock(return_value={})
    engine.get_container_status = AsyncMock()
    manager = Mock()
    manager.image_sources = {}
    manager.profiles = {}
    return CommandService(
        engine,
        manager,
        resolve_runtime_paths(state_dir=str(tmp_path)),
        admin_group_gid=4242,
    )


@pytest.mark.asyncio
async def test_mutation_requires_root_or_administrator_group(service):
    """Root-equivalent lifecycle access must never be granted by socket mode alone."""
    with pytest.raises(ChimeraError) as error:
        await service.execute("delete", {"name": "demo"}, PeerCredentials(uid=1000, gids=(1000,)))

    assert error.value.code == "permission_denied"


@pytest.mark.asyncio
async def test_verified_remote_certificate_is_administrator(service):
    """A TLS client certificate trusted by the server CA is root-equivalent."""
    service.state_engine.start_container = AsyncMock(
        return_value=SimpleNamespace(name="demo", desired_state="running")
    )
    peer = PeerCredentials(
        transport="tls",
        cert_sha256="abc123",
        cert_subject="CN=chimera-test-admin",
    )
    result = await service.execute("start", {"name": "demo"}, peer)
    assert result == {"name": "demo", "desired_state": "running"}


@pytest.mark.asyncio
async def test_tls_identity_cannot_spoof_unix_uid(service):
    """Remote callers are authorized by certificate, not claimed UID values."""
    service.state_engine.start_container = AsyncMock(
        return_value=SimpleNamespace(name="demo", desired_state="running")
    )
    peer = PeerCredentials(
        transport="tls",
        uid=0,
        cert_sha256="abc123",
        cert_subject="CN=chimera-test-admin",
    )
    await service.execute("start", {"name": "demo"}, peer)
    peer_unverified = PeerCredentials(transport="tls", uid=0)
    with pytest.raises(ChimeraError) as error:
        await service.execute("start", {"name": "demo"}, peer_unverified)
    assert error.value.code == "permission_denied"


@pytest.mark.asyncio
async def test_image_list_requires_source(service):
    """Listing images without a configured source is an invalid argument."""
    service.state_engine.list_source_images = AsyncMock()
    with pytest.raises(ChimeraError, match="requires a configured SimpleStreams source"):
        await service.execute("list", {"type": "images"}, PeerCredentials(uid=1000))
    service.state_engine.list_source_images.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_list_with_source_queries_that_source(service):
    """image list --source queries only that SimpleStreams source."""
    service.state_engine.list_source_images = AsyncMock(
        return_value=[{"product": "demo:product:amd64", "artifacts": ["rootfs"]}]
    )
    result = await service.execute(
        "list",
        {"type": "images", "image_source": "ubuntu"},
        PeerCredentials(uid=1000),
    )
    assert result["image_source"] == "ubuntu"
    service.state_engine.list_source_images.assert_awaited_once_with("ubuntu")


@pytest.mark.asyncio
async def test_image_info_is_a_read_command(service):
    """image_info uses the same resolution path and does not require root."""
    service.state_engine.describe_image = AsyncMock(
        return_value={"requested": "noble", "source": "ubuntu"}
    )
    result = await service.execute(
        "image_info",
        {"name": "noble", "image_source": "ubuntu"},
        PeerCredentials(uid=1000, gids=(1000,)),
    )
    assert result["source"] == "ubuntu"
    service.state_engine.describe_image.assert_awaited_once_with(
        "noble", image_source="ubuntu", image_artifact="rootfs"
    )
