"""Tests for user-visible CLI help, success output, and diagnostics."""

import json
from unittest.mock import patch

from typer.testing import CliRunner

from chimera.cli.client import ClientError
from chimera.cli.main import app

runner = CliRunner()


@patch("chimera.cli.main.ChimeraClient.request")
def test_json_error_is_machine_readable(mock_request):
    """Structured API errors remain structured at the CLI boundary."""
    mock_request.side_effect = ClientError(
        "not_found",
        "Container 'demo' is not managed by Chimera Spawn.",
        suggestion="Create it first.",
    )

    result = runner.invoke(app, ["info", "demo", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["suggestion"] == "Create it first."


def test_legacy_import_dry_run_validates_records(tmp_path):
    """Migration dry runs must validate records before promising an import."""
    node_file = tmp_path / "nodes.yaml"
    node_file.write_text(
        "containers:\n  ../unsafe:\n    image: ubuntu\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["config", "import-nodes", str(node_file), "--dry-run", "--format", "json"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "invalid_configuration"
