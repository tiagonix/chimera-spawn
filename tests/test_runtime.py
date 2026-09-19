"""Tests for cwd-independent runtime path resolution."""

from chimera.runtime import (
    DEFAULT_CATALOG_DIR,
    DEFAULT_CONFIG_DIR,
    DEFAULT_SOCKET_PATH,
    DEFAULT_STATE_DIR,
    resolve_runtime_paths,
)


def test_defaults_use_fhs_paths(monkeypatch):
    """Installed execution never derives paths from the current directory."""
    monkeypatch.delenv("CHIMERA_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CHIMERA_STATE_DIR", raising=False)
    monkeypatch.delenv("CHIMERA_SOCKET", raising=False)
    monkeypatch.delenv("CHIMERA_CATALOG_DIR", raising=False)

    paths = resolve_runtime_paths()

    assert paths.config_dir == DEFAULT_CONFIG_DIR
    assert paths.state_dir == DEFAULT_STATE_DIR
    assert paths.socket_path == DEFAULT_SOCKET_PATH
    assert paths.catalog_dir == DEFAULT_CATALOG_DIR
