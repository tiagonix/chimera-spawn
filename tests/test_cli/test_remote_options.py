"""Remote endpoint parsing and CLI option validation."""

import pytest

from chimera.cli.client import ChimeraClient, ClientError
from chimera.endpoint import parse_remote_endpoint
from tests.support.tls import generate_tls_bundle


def test_host_port_and_ipv6_forms():
    assert parse_remote_endpoint("server.example.net:8443") == ("server.example.net", 8443)
    assert parse_remote_endpoint("[::1]:8080") == ("::1", 8080)
    assert parse_remote_endpoint("[::1]") == ("::1", 8080)


def test_socket_and_host_are_mutually_exclusive(tmp_path):
    with pytest.raises(ClientError) as error:
        ChimeraClient(socket_path=str(tmp_path / "server.sock"), host="localhost:8080")
    assert error.value.code == "invalid_argument"


def test_remote_client_uses_https_and_wss(tmp_path):
    """Remote URI construction never produces http:// or ws://."""
    bundle = generate_tls_bundle(tmp_path / "pki")
    client = ChimeraClient(
        host="localhost",
        tls_ca=str(bundle.ca_cert),
        tls_cert=str(bundle.client_cert),
        tls_key=str(bundle.client_key),
    )
    assert client.base_url.startswith("https://")
    assert client.ws_base_url.startswith("wss://")
    assert "http://" not in client.base_url
