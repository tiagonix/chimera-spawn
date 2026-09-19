"""Contained writes must not follow unexpected links out of a container root."""

import pytest

from chimera.errors import ChimeraError
from chimera.utils.fs import write_contained_text


def test_write_contained_text_refuses_parent_escape(tmp_path):
    """Parent-directory parts are rejected before any mutation."""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ChimeraError, match="contained relative path"):
        write_contained_text(root, "../outside", "nope")
    assert not (tmp_path / "outside").exists()


def test_write_contained_text_refuses_intermediate_symlink(tmp_path):
    """An intermediate symlink is not followed into an unrelated tree."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "var").symlink_to(outside)
    with pytest.raises(ChimeraError, match="intermediate symlink"):
        write_contained_text(root, "var/lib/cloud/seed/user-data", "secret")
    assert list(outside.iterdir()) == []
