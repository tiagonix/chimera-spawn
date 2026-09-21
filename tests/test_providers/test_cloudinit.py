"""Cloud-init seed writes reuse contained filesystem operations."""

import pytest

from chimera.errors import ChimeraError
from chimera.models.container import CloudInitSpec, ContainerSpec
from chimera.providers.cloudinit import CloudInitProvider


@pytest.mark.asyncio
async def test_prepare_writes_seed_files_inside_the_container_root(tmp_path):
    """Rendered nocloud files land under the verified directory materialization."""
    root = tmp_path / "demo"
    root.mkdir()
    provider = CloudInitProvider()
    provider.machines_dir = tmp_path
    spec = ContainerSpec(
        name="demo",
        image="ubuntu",
        image_source="ubuntu",
        cloud_init=CloudInitSpec(user_data="#cloud-config\npackages: []\n"),
    )
    await provider.prepare(spec)
    seed = root / "var/lib/cloud/seed/nocloud"
    assert (seed / "user-data").read_text(encoding="utf-8") == "#cloud-config\npackages: []\n"
    assert "local-hostname" in (seed / "meta-data").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_prepare_refuses_symlink_root_and_intermediate_links(tmp_path):
    """Cloud-init must not follow a fake container root or intermediate links."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "demo"
    link.symlink_to(real)
    provider = CloudInitProvider()
    provider.machines_dir = tmp_path
    spec = ContainerSpec(
        name="demo",
        image="ubuntu",
        image_source="ubuntu",
        cloud_init=CloudInitSpec(user_data="#cloud-config\n"),
    )
    with pytest.raises(ChimeraError, match="not a writable root filesystem"):
        await provider.prepare(spec)

    container = tmp_path / "guest"
    container.mkdir()
    (container / "var").symlink_to(tmp_path / "escape")
    (tmp_path / "escape").mkdir()
    spec = ContainerSpec(
        name="guest",
        image="ubuntu",
        image_source="ubuntu",
        cloud_init=CloudInitSpec(user_data="#cloud-config\n"),
    )
    with pytest.raises(ChimeraError, match="intermediate symlink"):
        await provider.prepare(spec)
    assert list((tmp_path / "escape").iterdir()) == []
