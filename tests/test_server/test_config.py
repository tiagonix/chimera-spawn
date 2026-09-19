"""
Tests for server configuration management.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import pytest

from chimera.server.config import ConfigManager


@pytest.fixture
def config_dir(tmp_path):
    """Create a temporary config directory structure."""
    (tmp_path / "images").mkdir()
    (tmp_path / "profiles").mkdir()
    (tmp_path / "cloud-init").mkdir()

    # Create main config
    (tmp_path / "chimera.yaml").write_text("""
systemd:
  machines_dir: /tmp/machines
""")
    return tmp_path


@pytest.mark.asyncio
class TestConfigManager:
    """Test ConfigManager async operations."""

    async def test_local_catalog_overrides_packaged_default(self, config_dir, tmp_path):
        """The site catalog must override a same-name packaged definition."""
        packaged = tmp_path / "catalog"
        (packaged / "images").mkdir(parents=True)
        (packaged / "images" / "base.yaml").write_text(
            "ubuntu:\n  type: tar\n  source: https://example.invalid/default.tar\n"
        )
        (config_dir / "images" / "local.yaml").write_text(
            "ubuntu:\n  type: tar\n  source: https://example.invalid/local.tar\n"
        )

        manager = ConfigManager(config_dir, packaged)
        await manager.load()

        assert manager.get_image_spec("ubuntu").source.endswith("/local.tar")

    @pytest.mark.parametrize(
        "changed_config",
        [
            "systemd:\n  machines_dir: /different\n",
            "proxy:\n  http_proxy: http://proxy.invalid\n",
            "server:\n  admin_group: different-admin\n",
            "server:\n  log_level: DEBUG\n",
            "server:\n  host: 127.0.0.1\n  tls:\n    certificate: /tmp/a.crt\n"
            "    private_key: /tmp/a.key\n    client_ca: /tmp/ca.crt\n",
        ],
    )
    async def test_restart_required_change_does_not_partially_apply(
        self, config_dir, changed_config
    ):
        """Captured provider/security settings stay coherent until a restart."""
        manager = ConfigManager(config_dir)
        await manager.load()
        active = manager.snapshot
        (config_dir / "chimera.yaml").write_text(changed_config)

        candidate = await manager.build_snapshot()
        with pytest.raises(Exception, match="require a server restart"):
            manager.apply_snapshot(candidate)

        assert manager.snapshot is active
