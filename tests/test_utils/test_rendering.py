"""Fingerprint rendering includes proxy context and ignores unused catalog noise."""

from chimera.models.container import CloudInitSpec, stable_fingerprint
from chimera.models.image import ImageSpec
from chimera.models.profile import ProfileSpec
from chimera.utils.rendering import creation_render_payload, host_config_render_payload


def test_irrelevant_image_source_does_not_change_creation_fingerprint():
    """Image download URL is not applied during rootfs initialization."""
    cloud_init = CloudInitSpec(user_data="#cloud-config\n")
    left = creation_render_payload(
        container_name="demo",
        image=ImageSpec(name="ubuntu", type="tar", source="https://example.invalid/a.tar"),
        cloud_init=cloud_init,
        proxy=None,
    )
    right = creation_render_payload(
        container_name="demo",
        image=ImageSpec(name="ubuntu", type="tar", source="https://example.invalid/b.tar"),
        cloud_init=cloud_init,
        proxy=None,
    )
    assert stable_fingerprint(left) == stable_fingerprint(right)


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
