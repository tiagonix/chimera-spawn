"""Unix socket inspection and pathname reservation."""

import socket

import pytest

from chimera.errors import ChimeraError
from chimera.utils.unixsocket import SocketPathLock, inspect_unix_socket


def test_inspect_does_not_follow_a_socket_path_symlink(tmp_path):
    """A symlink at the user-supplied socket path is not a live Chimera socket."""
    target = tmp_path / "real.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(target))
    listener.listen(1)
    try:
        alias = tmp_path / "alias.sock"
        alias.symlink_to(target)
        assert inspect_unix_socket(alias) == "not_socket"
        assert inspect_unix_socket(target) == "live"
    finally:
        listener.close()


def test_socket_path_lock_is_exclusive_and_leaves_the_file(tmp_path):
    """Cooperating starters serialize on the same socket pathname."""
    socket_path = tmp_path / "server.sock"
    first = SocketPathLock(socket_path)
    first.acquire()
    try:
        second = SocketPathLock(socket_path)
        with pytest.raises(ChimeraError, match="starting or listening"):
            second.acquire()
        assert first.lock_path.exists()
        assert oct(first.lock_path.stat().st_mode & 0o777) == "0o600"
    finally:
        first.release()
    assert first.lock_path.exists()
    successor = SocketPathLock(socket_path)
    successor.acquire()
    successor.release()
