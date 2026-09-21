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


@patch("chimera.cli.main.ChimeraClient.request")
def test_image_list_requires_source(mock_request):
    """image list without --source fails before contacting the server."""
    result = runner.invoke(app, ["image", "list", "--format", "json"])
    assert result.exit_code == 1
    mock_request.assert_not_called()
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "invalid_argument"


@patch("chimera.cli.main.ChimeraClient.request")
def test_image_list_source_option_is_command_local(mock_request):
    """--source names a configured SimpleStreams source, not a URL."""
    mock_request.return_value = {"images": {}}
    result = runner.invoke(app, ["image", "list", "--source", "ubuntu"])
    assert result.exit_code == 0
    assert mock_request.call_args.args[1]["image_source"] == "ubuntu"


@patch("chimera.cli.main.ChimeraClient.request")
def test_launch_omits_image_source_unless_explicit(mock_request):
    """Omitting --source leaves unqualified resolution to the server."""
    mock_request.return_value = {
        "name": "demo",
        "image": "com.ubuntu.cloud:server:24.04:amd64",
        "desired_state": "running",
    }
    result = runner.invoke(app, ["launch", "noble", "demo"])
    assert result.exit_code == 0
    payload = mock_request.call_args.args[1]
    assert "image_source" not in payload
    assert payload["image_artifact"] == "rootfs"
    mock_request.reset_mock()
    mock_request.return_value = {
        "name": "demo2",
        "image": "com.ubuntu.cloud:server:24.04:amd64",
        "desired_state": "running",
    }
    result = runner.invoke(
        app, ["launch", "noble", "demo2", "--source", "ubuntu", "--artifact", "disk"]
    )
    assert result.exit_code == 0
    assert mock_request.call_args.args[1]["image_source"] == "ubuntu"
    assert mock_request.call_args.args[1]["image_artifact"] == "disk"
