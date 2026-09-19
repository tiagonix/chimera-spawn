"""
Tests for container provider.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from unittest.mock import AsyncMock, patch

import pytest

from chimera.errors import ChimeraError
from chimera.models.container import ContainerSpec
from chimera.models.image import CustomFileSpec, ImageSpec
from chimera.providers.base import ProviderStatus
from chimera.providers.container import ContainerProvider


@pytest.mark.asyncio
async def test_custom_file_rejects_intermediate_symlink_escape(tmp_path):
    """An intermediate symlink cannot redirect a requested link into the host."""
    provider = ContainerProvider()
    provider.machines_dir = tmp_path / "machines"
    root = provider.machines_dir / "demo"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "etc").symlink_to(outside)

    with pytest.raises(ChimeraError, match="intermediate symlink"):
        await provider._apply_custom_files(
            "demo", [CustomFileSpec(path="etc/inside", ensure="link", target="/dev/null")]
        )

    assert not (outside / "inside").exists()


@pytest.mark.asyncio
async def test_provision_rootfs_refuses_a_running_container(tmp_path):
    """Creation-time mutation is not applied while the container is running."""
    provider = ContainerProvider()
    provider.machines_dir = tmp_path / "machines"
    provider.nspawn_dir = tmp_path / "nspawn"
    provider.system_dir = tmp_path / "system"
    spec = ContainerSpec(name="demo", image="ubuntu")
    spec._image_spec = ImageSpec(
        name="ubuntu",
        type="tar",
        source="https://example.invalid/base.tar",
        custom_files=[CustomFileSpec(path="etc/example", ensure="absent")],
    )
    provider.is_running = AsyncMock(return_value=True)
    provider._apply_custom_files = AsyncMock()

    with pytest.raises(ChimeraError, match="while 'demo' is running"):
        await provider.provision_rootfs(spec)

    provider._apply_custom_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_observation_error_cannot_create_or_remove_host_artifacts(tmp_path):
    """An unknown host state never authorizes clone, remove, or cleanup."""
    provider = ContainerProvider()
    provider.machines_dir = tmp_path / "machines"
    provider.nspawn_dir = tmp_path / "nspawn"
    provider.system_dir = tmp_path / "system"
    spec = ContainerSpec(name="demo", image="ubuntu")
    provider.inspect_materialization = AsyncMock(return_value=(ProviderStatus.ERROR, None))
    provider.status = AsyncMock(return_value=ProviderStatus.ERROR)

    with patch("chimera.providers.container.run_command", new_callable=AsyncMock) as run_command:
        with pytest.raises(ChimeraError, match="Could not determine"):
            await provider.present(spec)
        with pytest.raises(ChimeraError, match="Could not determine"):
            await provider.absent(spec)

    run_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_rejects_online_name_without_local_artifacts(tmp_path):
    """An online machine rooted outside Chimera storage is still a create conflict."""
    provider = ContainerProvider()
    provider.machines_dir = tmp_path / "machines"
    provider.nspawn_dir = tmp_path / "nspawn"
    provider.system_dir = tmp_path / "system"
    provider.machines_dir.mkdir()
    provider.nspawn_dir.mkdir()
    provider.system_dir.mkdir()
    provider.status = AsyncMock(return_value=ProviderStatus.ABSENT)
    provider.systemd_dbus = AsyncMock()
    provider.systemd_dbus.list_machines = AsyncMock(
        return_value=[{"name": "foreign", "class": "container", "service": "", "object_path": ""}]
    )
    spec = ContainerSpec(name="foreign", image="ubuntu")

    with pytest.raises(ChimeraError, match="unmanaged host resource") as error:
        await provider.preflight_create(spec)
    assert "online machine" in (error.value.detail or "")


@pytest.mark.asyncio
async def test_preflight_inventory_failure_is_not_empty(tmp_path):
    """A broken machine inventory cannot be treated as permission to create."""
    provider = ContainerProvider()
    provider.machines_dir = tmp_path / "machines"
    provider.nspawn_dir = tmp_path / "nspawn"
    provider.system_dir = tmp_path / "system"
    provider.machines_dir.mkdir()
    provider.nspawn_dir.mkdir()
    provider.system_dir.mkdir()
    provider.status = AsyncMock(return_value=ProviderStatus.ABSENT)
    provider.systemd_dbus = AsyncMock()
    provider.systemd_dbus.list_machines = AsyncMock(side_effect=RuntimeError("inventory failed"))
    spec = ContainerSpec(name="demo", image="ubuntu")

    with pytest.raises(ChimeraError, match="online machines"):
        await provider.preflight_create(spec)
