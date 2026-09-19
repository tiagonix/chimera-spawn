"""Local doctor tests that do not depend on a server connection."""

import json
from unittest.mock import patch

from chimera.cli.client import ClientError
from chimera.cli.diagnostics import diagnose_client_error, render_error


@patch("chimera.cli.diagnostics._server_start_suggestion")
def test_remote_failures_do_not_probe_local_server(mock_suggest):
    """Remote transport errors must not consult the workstation server service."""
    mock_suggest.side_effect = AssertionError("local server probe")
    error = ClientError(
        "server_unreachable",
        "The remote Chimera server could not be reached over TLS.",
        suggestion="Run 'chimeractl doctor' and inspect the server journal.",
        transport="tls",
    )
    diagnosed = diagnose_client_error(error)
    mock_suggest.assert_not_called()
    assert "systemctl start chimera-server" not in (diagnosed.suggestion or "")
    assert "local chimera-server" in (diagnosed.suggestion or "")
    rendered = render_error(error, "json")
    mock_suggest.assert_not_called()
    assert "systemctl start chimera-server" not in rendered
    payload = json.loads(rendered)
    assert payload["error"]["code"] == "server_unreachable"
