"""TLS material validation tests."""

import pytest

from chimera.errors import ChimeraError
from chimera.models.config import ServerTlsConfig
from chimera.tls import validate_tls_material
from tests.support.tls import generate_tls_bundle


def test_private_key_symlink_is_rejected(tmp_path):
    bundle = generate_tls_bundle(tmp_path / "pki")
    linked = tmp_path / "linked.key"
    linked.symlink_to(bundle.server_key)
    tls = ServerTlsConfig(
        certificate=str(bundle.server_cert),
        private_key=str(linked),
        client_ca=str(bundle.ca_cert),
    )
    with pytest.raises(ChimeraError) as error:
        validate_tls_material(tls)
    assert "symlink" in error.value.message.lower()


def test_world_readable_private_key_is_rejected(tmp_path):
    bundle = generate_tls_bundle(tmp_path / "pki")
    bundle.server_key.chmod(0o644)
    tls = ServerTlsConfig(
        certificate=str(bundle.server_cert),
        private_key=str(bundle.server_key),
        client_ca=str(bundle.ca_cert),
    )
    with pytest.raises(ChimeraError) as error:
        validate_tls_material(tls)
    assert "permissions" in error.value.message.lower()
    assert "0640" not in error.value.detail
    assert "expected 0600" in error.value.detail
