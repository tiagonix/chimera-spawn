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
    manager.images = {}
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
