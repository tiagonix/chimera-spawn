"""Rich table rendering preserves complete long values."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from chimera.cli.commands import print_resources

PRODUCT = "com.ubuntu.cloud:server:26.04:amd64"

IMAGE_RESPONSE = {
    "images": {
        PRODUCT: {
            "references": ["26.04", "resolute"],
            "release": "resolute",
            "variant": "default",
            "architecture": "amd64",
            "product": PRODUCT,
            "artifacts": ["rootfs", "disk"],
            "policy": "packaged",
        }
    },
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


def test_wide_image_table_shows_complete_canonical_product():
    output = _render(240)
    assert PRODUCT in output
    assert "rootfs,disk" in output
    assert "…" not in output
    assert "..." not in output
