"""Fingerprint rendering includes composed nspawn parameters."""

from chimera.models.container import stable_fingerprint
from chimera.models.profile import ProfileSpec
from chimera.utils.rendering import host_config_render_payload


def test_host_config_fingerprint_includes_composed_nspawn_parameters():
    """Image kernel parameters are part of the applied host-config contract."""
    profile = ProfileSpec(
        name="isolated",
        nspawn_config_content="[Exec]\nBoot=true\n",
        systemd_override_content="[Service]\n",
    )
    left = host_config_render_payload(container_name="demo", profile=profile, proxy=None)
    right = host_config_render_payload(
        container_name="demo",
        profile=profile,
        proxy=None,
        extra_parameters=["systemd.mask=ssh.socket"],
    )
    assert right["nspawn"]["content"].count("Parameters=") == 1
    assert "systemd.mask=ssh.socket" in right["nspawn"]["content"]
    assert "Boot=true" in right["nspawn"]["content"]
    assert stable_fingerprint(left) != stable_fingerprint(right)
