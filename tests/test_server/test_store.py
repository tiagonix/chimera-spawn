"""Tests for durable managed-container state."""

import json
import os
import stat
from unittest.mock import patch

import pytest

from chimera.errors import ChimeraError, StoreCorruptionError
from chimera.models.container import ContainerRecord, ContainerSpec
from chimera.server.store import ContainerStore


def make_record(name: str = "demo") -> ContainerRecord:
    """Create a minimal valid durable container record."""
    return ContainerRecord(spec=ContainerSpec(name=name, image="ubuntu"))


def test_store_round_trip_preserves_current_durable_state(tmp_path):
    """A complete record retains its durable lifecycle state across reload."""
    path = tmp_path / "state" / "state.json"
    store = ContainerStore(path)
    store.load()
    store.add(
        ContainerRecord(
            spec=ContainerSpec(name="demo", image="ubuntu"),
            provisioning_state="complete",
            provisioning_fingerprint="creation-fingerprint",
            host_config_fingerprint="host-fingerprint",
            materialization_id="device:inode",
        )
    )

    reloaded = ContainerStore(path)
    reloaded.load()

    record = reloaded.get("demo")
    assert record.name == "demo"
    assert record.provisioning_state == "complete"
    assert record.provisioning_fingerprint == "creation-fingerprint"
    assert record.host_config_fingerprint == "host-fingerprint"
    assert record.materialization_id == "device:inode"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".state.json.*"))


def test_store_fails_closed_on_invalid_document(tmp_path):
    """An invalid store remains untouched instead of being silently replaced."""
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(StoreCorruptionError):
        ContainerStore(path).load()

    assert path.read_text(encoding="utf-8") == "{not-json"


def test_import_is_all_or_nothing(tmp_path):
    """Import validation leaves state alone if any record conflicts."""
    store = ContainerStore(tmp_path / "state.json")
    store.load()
    store.add(make_record("existing"))

    with pytest.raises(ChimeraError, match="duplicate"):
        store.import_records([make_record("new"), make_record("existing")])

    assert [record.name for record in store.records()] == ["existing"]


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (lambda store: store.add(make_record("new")), ["existing"]),
        (
            lambda store: store.replace(
                ContainerRecord(spec=ContainerSpec(name="existing", image="replacement"))
            ),
            ["existing"],
        ),
        (lambda store: store.remove("existing"), ["existing"]),
        (lambda store: store.import_records([make_record("new")]), ["existing"]),
    ],
)
def test_failed_mutation_never_publishes_candidate_to_memory_or_disk(tmp_path, operation, expected):
    """A pre-publication write failure leaves both authorities at the old registry."""
    path = tmp_path / "state.json"
    store = ContainerStore(path)
    store.load()
    store.add(make_record("existing"))
    before = path.read_text(encoding="utf-8")

    with (
        patch.object(
            store,
            "_write",
            side_effect=ChimeraError("state_write_failed", "simulated write failure"),
        ),
        pytest.raises(ChimeraError, match="simulated write failure"),
    ):
        operation(store)

    assert [record.name for record in store.records()] == expected
    assert path.read_text(encoding="utf-8") == before


def test_store_refuses_state_json_symlink(tmp_path):
    """A leaf symlink is not an empty store and is not followed."""
    path = tmp_path / "state.json"
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(StoreCorruptionError) as error:
        ContainerStore(path).load()
    assert "symlink" in (error.value.detail or "")


def test_store_writes_through_locked_directory_descriptor(tmp_path):
    """Replacing the state directory path cannot redirect writes after the lock is held."""
    from chimera.server.lock import StateLock

    state_dir = tmp_path / "state"
    lock = StateLock(state_dir)
    lock.acquire()
    try:
        store = ContainerStore(
            state_dir / "state.json",
            state_dir_identity=lock.directory_identity,
            directory_fd=lock.directory_fd,
        )
        store.load()
        store.add(make_record())
        os.rename(state_dir, tmp_path / "old")
        state_dir.mkdir()
        store.add(make_record("second"))
        assert not (state_dir / "state.json").exists()
        payload = json.loads((tmp_path / "old" / "state.json").read_text(encoding="utf-8"))
        assert "second" in payload["containers"]
        assert "demo" in payload["containers"]
    finally:
        lock.release()
