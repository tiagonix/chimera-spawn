"""Core image-source and product-policy contracts."""

import pytest
from pydantic import ValidationError

from chimera.models.image import (
    CustomFileSpec,
    ImageProductPolicy,
    ImageSourceSpec,
)


class TestImageSourceSpec:
    """Administrator-configured source URLs are the fetch trust boundary."""

    def test_ubuntu_source_normalizes_https_url(self):
        """A source name identifies a SimpleStreams catalog URL."""
        spec = ImageSourceSpec(
            name="ubuntu",
            url="https://cloud-images.example/releases",
            metadata_verify="tls",
        )
        assert spec.name == "ubuntu"
        assert spec.url == "https://cloud-images.example/releases/"
        assert spec.metadata_verify == "tls"
        assert spec.keyring is None

    def test_signature_mode_requires_absolute_keyring(self):
        """Signed metadata is bound to an administrator-configured keyring path."""
        spec = ImageSourceSpec(
            name="ubuntu",
            url="https://cloud-images.example/releases/",
            metadata_verify="signature",
            keyring="/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg",
        )
        assert spec.metadata_verify == "signature"
        assert spec.keyring == "/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg"
        with pytest.raises(ValidationError, match="keyring"):
            ImageSourceSpec(
                name="ubuntu",
                url="https://cloud-images.example/releases/",
                metadata_verify="signature",
            )
        with pytest.raises(ValidationError, match="absolute"):
            ImageSourceSpec(
                name="ubuntu",
                url="https://cloud-images.example/releases/",
                metadata_verify="signature",
                keyring="relative.gpg",
            )

    def test_tls_mode_forbids_keyring(self):
        """TLS metadata trust does not take a GnuPG keyring."""
        with pytest.raises(ValidationError, match="must not set a keyring"):
            ImageSourceSpec(
                name="images",
                url="https://images.example/",
                metadata_verify="tls",
                keyring="/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg",
            )

    @pytest.mark.parametrize(
        "url",
        [
            "http://cloud-images.example/releases/",
            "ftp://cloud-images.example/releases/",
            "file:///tmp/streams/",
            "https://user:pass@cloud-images.example/releases/",
            "https://cloud-images.example/releases/?q=1",
            "https://cloud-images.example/releases/#frag",
        ],
    )
    def test_simplestreams_url_rejects_unsafe_bases(self, url):
        """SimpleStreams sources require credential-free HTTPS without query or fragment."""
        with pytest.raises(ValidationError):
            ImageSourceSpec(name="ubuntu", url=url, metadata_verify="tls")

    def test_source_owns_exact_product_policy(self):
        """Product policy defaults empty and accepts only non-empty canonical keys."""
        bare = ImageSourceSpec(
            name="images",
            url="https://images.example/",
            metadata_verify="tls",
        )
        assert bare.products == {}
        spec = ImageSourceSpec(
            name="ubuntu",
            url="https://cloud-images.example/releases/",
            metadata_verify="tls",
            products={"com.ubuntu.cloud:server:24.04:amd64": {"nspawn_parameters": ["fstab=no"]}},
        )
        assert spec.products["com.ubuntu.cloud:server:24.04:amd64"].nspawn_parameters == [
            "fstab=no"
        ]
        with pytest.raises(ValidationError, match="product keys"):
            ImageSourceSpec(
                name="ubuntu",
                url="https://cloud-images.example/releases/",
                metadata_verify="tls",
                products={"": {}},
            )

    def test_product_policy_rejects_whitespace_nspawn_tokens(self):
        """Kernel command-line tokens cannot contain spaces."""
        with pytest.raises(ValidationError):
            ImageProductPolicy(nspawn_parameters=["systemd.mask=ssh.socket extra"])


class TestCustomFileSpec:
    """Test CustomFileSpec model."""

    def test_file_removal(self):
        """Test file removal spec."""
        spec = CustomFileSpec(path="etc/fstab", ensure="absent")

        assert spec.path == "etc/fstab"
        assert spec.ensure == "absent"
        assert spec.target is None

    def test_symlink_creation(self):
        """Test symlink creation spec."""
        spec = CustomFileSpec(
            path="etc/systemd/system/test.service", ensure="link", target="/dev/null"
        )

        assert spec.path == "etc/systemd/system/test.service"
        assert spec.ensure == "link"
        assert spec.target == "/dev/null"

    def test_invalid_ensure_value(self):
        """Test validation of ensure field."""
        with pytest.raises(ValidationError) as exc_info:
            CustomFileSpec(path="test", ensure="maybe")  # Invalid value

        assert "ensure" in str(exc_info.value)
