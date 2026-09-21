"""Behavioral contracts for durable lifecycle operations."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from chimera.errors import ChimeraError
from chimera.images.identity import effective_from_source
from chimera.images.reference import ImageResolution
from chimera.models.config import ChimeraConfig
from chimera.models.image import (
    CustomFileSpec,
    ImageProductPolicy,
    ImageSourceSpec,
    empty_product_policy,
)
from chimera.models.profile import ProfileSpec
from chimera.providers.base import ProviderStatus
from chimera.server.engine import StateEngine
from chimera.server.store import ContainerStore
from tests.support.stateful_provider import StatefulContainerProvider


@pytest.fixture
def lifecycle_engine(tmp_path):
    """Build an engine whose provider state actually changes."""
    source = ImageSourceSpec(
        name="ubuntu",
        url="https://images.example/releases/",
        metadata_verify="tls",
    )
    product = "com.ubuntu.cloud:server:24.04:amd64"
    manager = Mock()
    manager.config = ChimeraConfig()
    manager.image_sources = {"ubuntu": source}
    manager.cloud_init_templates = {}
    manager.get_image_source_spec.side_effect = lambda name: manager.image_sources.get(name)
    manager.get_product_policy.side_effect = lambda source_name, product_key: empty_product_policy()
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

    async def fake_resolve(
        config,
        reference,
        explicit_source=None,
        artifact_kind="rootfs",
        **kwargs,
    ):
        policy = manager.get_product_policy("ubuntu", product)
        return ImageResolution(
            effective=effective_from_source(source, product, policy, artifact_kind),
            requested_reference=reference,
            explicit_source=explicit_source,
            aliases=("ubuntu", "24.04"),
            architecture="amd64",
            artifact_kinds=("rootfs", "disk"),
        )

    patcher = patch("chimera.server.engine.resolve_image_reference", side_effect=fake_resolve)
    patcher.start()
    try:
        yield engine, store, image_provider, container_provider
    finally:
        patcher.stop()


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
    drifted = ImageProductPolicy(custom_files=[CustomFileSpec(path="etc/example", ensure="absent")])
    engine.config_manager.get_product_policy.side_effect = lambda source_name, product_key: drifted

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


@pytest.mark.asyncio
async def test_create_does_not_clone_an_incomplete_image_cache(lifecycle_engine):
    """Create and launch refuse an incomplete cache even when the name is already present."""
    engine, _store, image_provider, container_provider = lifecycle_engine
    image_provider.present = AsyncMock(
        side_effect=ChimeraError(
            code="image_cache_incomplete",
            message="Image cache exists but is not a completed read-only image.",
            status=409,
        )
    )

    with pytest.raises(ChimeraError) as caught:
        await engine.create_container(image="ubuntu", name="demo", start=True)

    assert caught.value.code == "image_cache_incomplete"
    assert container_provider.present_count == 0
    assert "demo" not in container_provider.materialized


@pytest.mark.asyncio
async def test_disk_pull_allows_product_custom_files(lifecycle_engine):
    """Pulling a disk image does not apply guest custom_files."""
    engine, store, image_provider, _container_provider = lifecycle_engine
    engine.config_manager.get_product_policy.side_effect = (
        lambda source_name, product_key: ImageProductPolicy(
            custom_files=[CustomFileSpec(path="etc/example", ensure="absent")]
        )
    )
    result = await engine.pull_image("ubuntu", image_source="ubuntu", image_artifact="disk")
    assert result["pulled"] is True
    assert result["artifact_kind"] == "disk"
    image_provider.present.assert_awaited()
    assert store.records() == []


@pytest.mark.asyncio
async def test_disk_create_rejects_cloud_init_before_durable_state(lifecycle_engine):
    """Disk plus cloud-init is rejected before a container record is stored."""
    engine, store, _image_provider, _container_provider = lifecycle_engine
    engine.config_manager.cloud_init_templates = {"base_minimal": {"user_data": "#cloud-config\n"}}
    with pytest.raises(ChimeraError, match="cloud-init"):
        await engine.create_container(
            image="ubuntu",
            name="demo",
            image_artifact="disk",
            cloud_init_template="base_minimal",
        )
    assert not store.contains("demo")


@pytest.mark.asyncio
async def test_disk_create_rejects_custom_files_before_durable_state(lifecycle_engine):
    """Disk plus product-policy custom_files is rejected before durable mutation."""
    engine, store, _image_provider, _container_provider = lifecycle_engine
    engine.config_manager.get_product_policy.side_effect = (
        lambda source_name, product_key: ImageProductPolicy(
            custom_files=[CustomFileSpec(path="etc/example", ensure="absent")]
        )
    )
    with pytest.raises(ChimeraError, match="custom_files"):
        await engine.create_container(image="ubuntu", name="demo", image_artifact="disk")
    assert not store.contains("demo")


@pytest.mark.asyncio
async def test_disk_create_allows_nspawn_parameters(lifecycle_engine):
    """Disk containers may still receive host nspawn kernel parameters."""
    engine, store, _image_provider, _container_provider = lifecycle_engine
    engine.config_manager.get_product_policy.side_effect = (
        lambda source_name, product_key: ImageProductPolicy(nspawn_parameters=["fstab=no"])
    )
    await engine.create_container(image="ubuntu", name="demo", image_artifact="disk")
    assert store.contains("demo")
    assert store.get("demo").spec.image_artifact == "disk"


@pytest.mark.asyncio
async def test_cloud_init_fingerprint_survives_store_round_trip(lifecycle_engine):
    """Reloaded template-only cloud-init specs keep the original creation fingerprint."""
    engine, store, _image_provider, _container_provider = lifecycle_engine
    engine.config_manager.cloud_init_templates = {
        "base_minimal": {
            "user_data": "#cloud-config\nusers:\n  - name: chimera\n",
            "meta_data": {"instance-id": "iid-demo"},
        }
    }
    await engine.create_container(image="ubuntu", name="demo", cloud_init_template="base_minimal")
    first = store.get("demo").provisioning_fingerprint
    assert first
    store.load()
    resolved = engine._resolve_for_provisioning(store.get("demo").spec)
    assert engine._creation_fingerprint(resolved) == first
    await engine.reconcile()
    assert store.get("demo").provisioning_state == "complete"
    assert store.get("demo").last_error is None
