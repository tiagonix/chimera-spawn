"""Process-level ContainerStore writer lock tests."""

import pytest

from chimera.errors import ChimeraError
from chimera.server.lock import StateLock


def test_second_state_lock_is_denied(tmp_path):
    """Two servers cannot own the same state directory."""
    first = StateLock(tmp_path)
    first.acquire()
    try:
        second = StateLock(tmp_path)
        with pytest.raises(ChimeraError, match="already owns this state directory"):
            second.acquire()
        assert second.held() is False
    finally:
        first.release()

    successor = StateLock(tmp_path)
    successor.acquire()
    successor.release()
    assert not successor.held()


def test_lock_refuses_symlink_state_directory(tmp_path):
    """A symlink at the state directory is not followed into another tree."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    lock = StateLock(link)
    with pytest.raises(ChimeraError, match="symlink"):
        lock.acquire()
    assert lock.held() is False
