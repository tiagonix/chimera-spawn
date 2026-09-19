"""Behavioral contracts for durable lifecycle operations."""

from unittest.mock import AsyncMock, Mock

import pytest

from chimera.errors import ChimeraError
from chimera.models.config import ChimeraConfig
from chimera.models.image import CustomFileSpec, ImageSpec
from chimera.models.profile import ProfileSpec
from chimera.providers.base import ProviderStatus
from chimera.server.engine import StateEngine
from chimera.server.store import ContainerStore
from tests.support.stateful_provider import StatefulContainerProvider


@pytest.fixture
def lifecycle_engine(tmp_path):
    """Build an engine whose provider state actually changes."""
    image = ImageSpec(name="ubuntu", type="tar", source="https://example.invalid/ubuntu.tar")
    manager = Mock()
    manager.config = ChimeraConfig()
    manager.images = {"ubuntu": image}
    manager.cloud_init_templates = {}
    manager.get_image_spec.side_effect = lambda name: image if name == "ubuntu" else None
    manager.get_profile_spec.side_effect = lambda name: ProfileSpec(
        name=name, nspawn_config_content="[Exec]", systemd_override_content="[Service]"
    )
    image_provider = Mock()
    image_provider.status = AsyncMock(return_value=ProviderStatus.PRESENT)
    image_provider.present = AsyncMock()
    image_provider.validate_spec = AsyncMock(return_value=True)
    container_provider = StatefulContainerProvider()
    registry = Mock()
    registry.get_provider.side_effect = lambda name: {
        "image": image_provider,
        "container": container_provider,
        "profile": Mock(validate_spec=AsyncMock(return_value=True)),
    }.get(name)
    store = ContainerStore(tmp_path / "state.json")
    store.load()
    engine = StateEngine(manager, registry, store)
    return engine, store, image_provider, container_provider


@pytest.mark.asyncio
async def test_create_persists_stopped_intent_before_host_materialization(lifecycle_engine):
    """Create durable state exists even if the host materialization later fails."""
    engine, store, _image_provider, container_provider = lifecycle_engine
    container_provider.fail_at = "present"

    with pytest.raises(ChimeraError, match="Could not create"):
        await engine.create_container(image="ubuntu", name="demo")

    record = store.get("demo")
    assert record.desired_state == "stopped"
    assert record.provisioning_state == "pending"
    assert "present failed" in record.last_error


@pytest.mark.asyncio
async def test_failed_delete_remains_visible_for_reconciliation(lifecycle_engine):
    """Delete state is not discarded when host cleanup cannot complete."""
    engine, store, _image_provider, container_provider = lifecycle_engine
    await engine.create_container(image="ubuntu", name="demo")
    container_provider.fail_at = "absent"

    with pytest.raises(ChimeraError, match="Could not delete"):
        await engine.remove_container("demo")

    record = store.get("demo")
    assert record.deleting is True
    assert "absent failed" in record.last_error


@pytest.mark.asyncio
async def test_launch_does_not_start_until_provisioning_completes(lifecycle_engine):
    """A failed creation-time provision keeps running intent and does not start."""
    engine, store, _image_provider, container_provider = lifecycle_engine
    container_provider.fail_at = "provision_rootfs"

    with pytest.raises(ChimeraError, match="Could not launch"):
        await engine.create_container(image="ubuntu", name="demo", start=True)

    record = store.get("demo")
    assert record.desired_state == "running"
    assert record.provisioning_state == "pending"
    assert container_provider.start_counts.get("demo", 0) == 0


@pytest.mark.asyncio
async def test_successful_reconcile_does_not_rerun_creation_provisioning(lifecycle_engine):
    """Steady-state reconcile must not rewrite custom-files or cloud-init."""
    engine, _store, _image_provider, container_provider = lifecycle_engine
    await engine.create_container(image="ubuntu", name="demo")
    assert container_provider.provision_count == 1

    await engine.reconcile()

    assert container_provider.provision_count == 1


@pytest.mark.asyncio
async def test_creation_provisioning_drift_is_diagnosed_not_applied(lifecycle_engine):
    """Catalog custom-file changes after success require recreate, not live mutation."""
    engine, store, _image_provider, container_provider = lifecycle_engine
    await engine.create_container(image="ubuntu", name="demo")
    drifted = ImageSpec(
        name="ubuntu",
        type="tar",
        source="https://example.invalid/ubuntu.tar",
        custom_files=[CustomFileSpec(path="etc/example", ensure="absent")],
    )
    engine.config_manager.get_image_spec.side_effect = lambda name: (
        drifted if name == "ubuntu" else None
    )

    await engine.reconcile()

    record = store.get("demo")
    assert record.provisioning_state == "complete"
    assert "Recreate" in (record.last_error or "")
    assert container_provider.provision_count == 1


@pytest.mark.asyncio
async def test_restart_applies_pending_host_config_without_rootfs_provisioning(lifecycle_engine):
    """Explicit restart may apply host-side profile changes after stopping."""
    engine, store, _image_provider, container_provider = lifecycle_engine
    await engine.create_container(image="ubuntu", name="demo", start=True)
    engine.config_manager.get_profile_spec.side_effect = lambda name: ProfileSpec(
        name=name,
        nspawn_config_content="[Exec]\nBoot=yes",
        systemd_override_content="[Service]",
    )
    apply_count = container_provider.apply_count

    await engine.restart_container("demo")

    assert store.get("demo").desired_state == "running"
    assert "demo" in container_provider.running
    assert container_provider.apply_count == apply_count + 1
    assert container_provider.provision_count == 1
    assert container_provider.restart_counts.get("demo", 0) == 0
    assert container_provider.start_counts.get("demo", 0) >= 2


@pytest.mark.asyncio
async def test_disappeared_materialization_is_reinitialized(lifecycle_engine):
    """Completion cannot apply to a later filesystem under the same name."""
    engine, store, _image_provider, container_provider = lifecycle_engine
    await engine.create_container(image="ubuntu", name="demo")
    first_id = store.get("demo").materialization_id
    assert store.get("demo").provisioning_state == "complete"
    first_provision = container_provider.provision_count
    container_provider.disappear("demo")

    await engine.start_container("demo")

    record = store.get("demo")
    assert record.provisioning_state == "complete"
    assert record.materialization_id != first_id
    assert container_provider.provision_count == first_provision + 1


@pytest.mark.asyncio
async def test_observation_error_does_not_authorize_restart(lifecycle_engine):
    """An observation failure cannot be treated as absence or a safe restart."""
    engine, store, _image_provider, container_provider = lifecycle_engine
    await engine.create_container(image="ubuntu", name="demo", start=True)
    container_provider.inspect_error = True

    with pytest.raises(ChimeraError, match="Could not determine"):
        await engine.restart_container("demo")

    assert "demo" in container_provider.running
    assert store.get("demo").desired_state == "running"


@pytest.mark.asyncio
async def test_bound_materialization_mismatch_is_not_adopted(lifecycle_engine):
    """A known non-null binding still fails closed on a different filesystem."""
    engine, store, _image_provider, container_provider = lifecycle_engine
    await engine.create_container(image="ubuntu", name="demo")
    record = store.get("demo")
    record.materialization_id = "dir:1:1"
    store.replace(record)
    container_provider.materialized["demo"] = "dir:1:99"

    with pytest.raises(ChimeraError, match="different materialization"):
        await engine.start_container("demo")
    assert container_provider.provision_count == 1
    assert store.get("demo").materialization_id == "dir:1:1"
