"""Tests for server configuration management."""

import pytest

from chimera.errors import ChimeraError
from chimera.server.config import ConfigManager

PACKAGED_UBUNTU = """\
ubuntu:
  url: https://images.example/default/
  metadata_verify: signature
  keyring: /usr/share/keyrings/ubuntu-cloudimage-keyring.gpg
  products:
    "com.example:product-a:amd64":
      nspawn_parameters:
        - fstab=no
    "com.example:product-b:amd64":
      custom_files:
        - path: etc/example
          ensure: absent
      nspawn_parameters:
        - systemd.mask=ssh.socket
"""

SITE_UBUNTU = """\
ubuntu:
  url: https://images.example/local/
  products:
    "com.example:product-b:amd64":
      nspawn_parameters:
        - systemd.mask=ssh.service
"""

SITE_COMPANY = """\
company:
  url: https://images.example/company/
  metadata_verify: tls
"""


@pytest.fixture
def config_dir(tmp_path):
    """Create a temporary config directory structure."""
    (tmp_path / "profiles").mkdir()
    (tmp_path / "cloud-init").mkdir()
    (tmp_path / "chimera.yaml").write_text("""
systemd:
  machines_dir: /tmp/machines
""")
    return tmp_path


@pytest.mark.asyncio
class TestConfigManager:
    """Test ConfigManager async operations."""

    async def test_site_image_source_overlays_packaged_source(self, config_dir, tmp_path):
        """Site scalars and exact product policies overlay one packaged source."""
        packaged = tmp_path / "catalog" / "images"
        packaged.mkdir(parents=True)
        (packaged / "ubuntu.yaml").write_text(PACKAGED_UBUNTU)
        site = config_dir / "images"
        site.mkdir()
        (site / "ubuntu.yaml").write_text(SITE_UBUNTU)
        (site / "company.yaml").write_text(SITE_COMPANY)

        manager = ConfigManager(config_dir, tmp_path / "catalog")
        await manager.load()

        ubuntu = manager.get_image_source_spec("ubuntu")
        assert ubuntu is not None
        assert ubuntu.url.endswith("/local/")
        assert ubuntu.metadata_verify == "signature"
        assert ubuntu.keyring == "/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg"
        assert manager.get_product_policy(
            "ubuntu", "com.example:product-a:amd64"
        ).nspawn_parameters == ["fstab=no"]
        replaced = manager.get_product_policy("ubuntu", "com.example:product-b:amd64")
        assert replaced.nspawn_parameters == ["systemd.mask=ssh.service"]
        assert replaced.custom_files == []
        assert (
            manager.get_product_policy(
                "ubuntu", "com.example:not-published:amd64"
            ).nspawn_parameters
            == []
        )
        company = manager.get_image_source_spec("company")
        assert company is not None
        assert company.metadata_verify == "tls"
        assert company.products == {}
        listed = manager.list_image_sources()
        assert listed["ubuntu"]["url"].endswith("/local/")
        assert listed["ubuntu"]["metadata_verify"] == "signature"
        assert "products" not in listed["ubuntu"]

    async def test_inconsistent_site_trust_rejects_candidate(self, config_dir, tmp_path):
        """A site trust change that leaves an inherited keyring rejects the candidate."""
        packaged = tmp_path / "catalog" / "images"
        packaged.mkdir(parents=True)
        (packaged / "ubuntu.yaml").write_text(PACKAGED_UBUNTU)
        manager = ConfigManager(config_dir, tmp_path / "catalog")
        await manager.load()
        active = manager.snapshot
        site = config_dir / "images"
        site.mkdir()
        (site / "ubuntu.yaml").write_text("ubuntu:\n  metadata_verify: tls\n")

        with pytest.raises(ChimeraError) as caught:
            await manager.build_snapshot()

        assert caught.value.code == "invalid_configuration"
        assert manager.snapshot is active

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
