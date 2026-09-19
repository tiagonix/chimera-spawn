"""Rich table rendering preserves complete long values."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from chimera.cli.commands import print_resources

UBUNTU_26_SOURCE = (
    "https://cloud-images.ubuntu.com/releases/26.04/release/"
    "ubuntu-26.04-server-cloudimg-amd64-root.tar.xz"
)

IMAGE_RESPONSE = {
    "images": {
        "ubuntu-26.04-cloud-tar": {
            "name": "ubuntu-26.04-cloud-tar",
            "type": "tar",
            "verify": "signature",
            "source": UBUNTU_26_SOURCE,
        }
    }
}


def _render(width: int) -> str:
    buffer = StringIO()
    console = Console(
        file=buffer,
        width=width,
        height=24,
        force_terminal=True,
        color_system=None,
        highlight=False,
        legacy_windows=False,
    )
    print_resources(IMAGE_RESPONSE, "table", console_out=console)
    return buffer.getvalue()


def test_wide_image_table_shows_complete_source_url():
    output = _render(240)
    assert UBUNTU_26_SOURCE in output
    assert "ubuntu-26.04-server-cloudimg-amd64-root.tar.xz" in output
    assert "…" not in output
    assert "..." not in output
