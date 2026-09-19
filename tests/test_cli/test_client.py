"""Client-side diagnostics before a socket connection is attempted."""

from unittest.mock import patch

import pytest

from chimera.cli.client import ChimeraClient, ClientError


def test_missing_socket_is_a_server_availability_error(tmp_path):
    """A first-run missing server has a distinct, stable error code."""
    client = ChimeraClient(socket_path=str(tmp_path / "server.sock"))

    with pytest.raises(ClientError) as error:
        client.request("status")

    assert error.value.code == "server_unavailable"


def test_inaccessible_socket_is_a_permission_error(tmp_path):
    """Permission diagnostics do not depend on root's special os.access behavior."""
    socket_path = tmp_path / "server.sock"
    socket_path.touch()
    client = ChimeraClient(socket_path=str(socket_path))
    with (
        patch("chimera.cli.client.os.access", return_value=False),
        pytest.raises(ClientError) as error,
    ):
        client.request("status")

    assert error.value.code == "permission_denied"
