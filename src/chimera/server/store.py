"""Crash-safe persistence for CLI-managed container lifecycle intent.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import errno
import json
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chimera.errors import ChimeraError, StoreCorruptionError
from chimera.models.container import ContainerRecord
from chimera.pydantic_compat import model_copy, model_dump_json, model_validate

STORE_SCHEMA_VERSION = 1
CURRENT_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "spec",
        "desired_state",
        "deleting",
        "last_error",
        "provisioning_state",
        "provisioning_fingerprint",
        "host_config_fingerprint",
        "materialization_id",
        "updated_at",
    }
)
CURRENT_SPEC_FIELDS = frozenset(
    {
        "name",
        "ensure",
        "state",
        "image",
        "profile",
        "cloud_init",
        "autostart",
        "bind_mounts",
        "tmpfs_mounts",
        "port_forwards",
        "resource_controls",
    }
)


class _PostPublicationWriteError(Exception):
    """Carry a durability error raised after the replacement became visible."""

    def __init__(self, error: ChimeraError):
        self.error = error
        super().__init__(str(error))


class ContainerStore:
    """Persist managed containers with atomic replace semantics.

    The server is the sole writer. This class intentionally remains synchronous:
    writes are short local filesystem operations and are always called while the
    engine's mutation lock is held.
    """

    def __init__(
        self,
        path: Path,
        *,
        state_dir_identity: tuple[int, int] | None = None,
        directory_fd: int | None = None,
    ):
        self.path = Path(path)
        self.state_dir_identity = state_dir_identity
        self._directory_fd = directory_fd
        self._records: dict[str, ContainerRecord] = {}

    def load(self) -> None:
        """Load the registry, failing closed when an existing file is invalid."""
        try:
            with self._parent_directory_fd(create=False) as parent_fd:
                payload = self._read_state_payload(parent_fd)
        except FileNotFoundError:
            self._records = {}
            return
        if payload is None:
            self._records = {}
            return

        if not isinstance(payload, dict) or payload.get("schema_version") != STORE_SCHEMA_VERSION:
            raise StoreCorruptionError(
                f"unsupported or missing schema_version (expected {STORE_SCHEMA_VERSION})"
            )

        containers = payload.get("containers")
        if not isinstance(containers, dict):
            raise StoreCorruptionError("'containers' must be an object")

        records: dict[str, ContainerRecord] = {}
        try:
            for name, value in containers.items():
                record = self._validate_current_record(value)
                if name != record.name:
                    raise StoreCorruptionError(
                        f"container key '{name}' does not match its embedded name '{record.name}'"
                    )
                records[name] = record
        except ValidationError as error:
            raise StoreCorruptionError(str(error)) from error

        self._records = records

    @staticmethod
    def _validate_current_record(value: Any) -> ContainerRecord:
        """Validate one persisted record against the current durable shape."""
        if not isinstance(value, dict):
            raise StoreCorruptionError("container record must be an object")
        ContainerStore._require_current_fields(
            value, CURRENT_RECORD_FIELDS, description="container record"
        )
        spec = value["spec"]
        if not isinstance(spec, dict):
            raise StoreCorruptionError("container record spec must be an object")
        ContainerStore._require_current_fields(
            spec, CURRENT_SPEC_FIELDS, description="container record spec"
        )
        return model_validate(ContainerRecord, value)

    @staticmethod
    def _require_current_fields(
        value: dict[str, Any], expected: frozenset[str], *, description: str
    ) -> None:
        """Reject persisted fields that do not exactly match the current schema."""
        fields = set(value)
        missing = sorted(expected - fields)
        unexpected = sorted(fields - expected)
        if missing or unexpected:
            problems: list[str] = []
            if missing:
                problems.append(f"missing {', '.join(missing)}")
            if unexpected:
                problems.append(f"unexpected {', '.join(unexpected)}")
            raise StoreCorruptionError(f"{description} fields are invalid: {'; '.join(problems)}")

    def records(self) -> list[ContainerRecord]:
        """Return detached records ordered by name."""
        return [self._copy(record) for _, record in sorted(self._records.items())]

    def get(self, name: str) -> ContainerRecord:
        """Return a managed record or raise a stable not-found error."""
        record = self._records.get(name)
        if record is None:
            raise ChimeraError(
                code="not_found",
                message=f"Container '{name}' is not managed by Chimera Spawn.",
                suggestion=f"Create it with: chimeractl launch IMAGE {name}",
                status=404,
            )
        return self._copy(record)

    def contains(self, name: str) -> bool:
        """Report whether the durable registry owns a container name."""
        return name in self._records

    def add(self, record: ContainerRecord) -> None:
        """Add a unique container record and commit it atomically."""
        if record.name in self._records:
            raise ChimeraError(
                code="already_exists",
                message=f"Container '{record.name}' is already managed by Chimera Spawn.",
                suggestion=f"Inspect it with: chimeractl info {record.name}",
                status=409,
            )
        candidate = self._copy_records()
        candidate[record.name] = self._copy(record)
        self._commit(candidate)

    def replace(self, record: ContainerRecord) -> None:
        """Replace an existing record and commit it atomically."""
        if record.name not in self._records:
            raise ChimeraError(
                code="not_found",
                message=f"Container '{record.name}' is not managed by Chimera Spawn.",
                status=404,
            )
        candidate = self._copy_records()
        candidate[record.name] = self._copy(record)
        self._commit(candidate)

    def remove(self, name: str) -> None:
        """Remove an existing record and commit it atomically."""
        if name not in self._records:
            raise ChimeraError(
                code="not_found",
                message=f"Container '{name}' is not managed by Chimera Spawn.",
                status=404,
            )
        candidate = self._copy_records()
        del candidate[name]
        self._commit(candidate)

    def import_records(self, records: Iterable[ContainerRecord]) -> None:
        """Atomically add legacy records after validating the full batch."""
        incoming = list(records)
        names = [record.name for record in incoming]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        existing = sorted(set(names).intersection(self._records))
        if duplicates or existing:
            conflicts = ", ".join(duplicates + existing)
            raise ChimeraError(
                code="conflict",
                message=f"Cannot import duplicate managed containers: {conflicts}.",
                suggestion="Rename the conflicting records or remove them from the import file.",
                status=409,
            )

        candidate = self._copy_records()
        for record in incoming:
            candidate[record.name] = self._copy(record)
        self._commit(candidate)

    def _commit(self, candidate: dict[str, ContainerRecord]) -> None:
        """Publish a detached candidate, never mutating live memory first."""
        candidate = {name: self._copy(record) for name, record in candidate.items()}
        content = self._serialize(candidate)
        try:
            self._write(content)
        except _PostPublicationWriteError as error:
            # os.replace already made this exact candidate visible. Keeping the
            # prior map would make RAM and state.json disagree after the error.
            self._records = candidate
            raise error.error from error
        self._records = candidate

    @staticmethod
    def _serialize(records: dict[str, ContainerRecord]) -> str:
        """Serialize a complete candidate registry before any disk mutation."""
        payload = {
            "schema_version": STORE_SCHEMA_VERSION,
            "containers": {
                name: json.loads(model_dump_json(record))
                for name, record in sorted(records.items())
            },
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def _write(self, content: str) -> None:
        """Write prepared content with atomic publication and directory fsync.

        Publication order is: write+fsync the temporary file, replace onto
        state.json using directory-relative names, then fsync the same
        directory so the new name survives a crash.
        """
        fd: int | None = None
        temporary_name: str | None = None
        published = False
        try:
            with self._parent_directory_fd(create=True) as parent_fd:
                try:
                    fd, temporary_name = self._create_temporary(parent_fd)
                    payload = content.encode("utf-8")
                    view = memoryview(payload)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                    os.fsync(fd)
                    os.close(fd)
                    fd = None
                    os.replace(
                        temporary_name,
                        self.path.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    published = True
                    temporary_name = None
                    os.fsync(parent_fd)
                finally:
                    if fd is not None:
                        with suppress(OSError):
                            os.close(fd)
                        fd = None
                    if temporary_name is not None:
                        with suppress(OSError):
                            os.unlink(temporary_name, dir_fd=parent_fd)
        except OSError as error:
            state_error = ChimeraError(
                code="state_write_failed",
                message="Could not persist the managed-container registry.",
                detail=str(error),
                suggestion="Check available disk space and permissions, then retry.",
                status=500,
            )
            if published:
                raise _PostPublicationWriteError(state_error) from error
            raise state_error from error

    def _create_temporary(self, parent_fd: int) -> tuple[int, str]:
        """Create a private regular file relative to the locked directory."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        for _ in range(32):
            name = f".{self.path.name}.{os.getpid()}.{os.urandom(4).hex()}"
            try:
                fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                continue
            os.fchmod(fd, 0o600)
            return fd, name
        raise OSError("could not create a unique temporary state file")

    def _read_state_payload(self, parent_fd: int) -> dict[str, Any] | None:
        """Open state.json relative to the locked directory without following a leaf symlink."""
        try:
            fd = os.open(
                self.path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.EMLINK}:
                raise StoreCorruptionError("state.json is a symlink") from error
            raise StoreCorruptionError(str(error)) from error
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise StoreCorruptionError("state.json is not a regular file")
            chunks: list[bytes] = []
            while True:
                data = os.read(fd, 65536)
                if not data:
                    break
                chunks.append(data)
            raw = b"".join(chunks)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise StoreCorruptionError(str(error)) from error
            if not isinstance(payload, dict):
                raise StoreCorruptionError("state document must be an object")
            return payload
        finally:
            os.close(fd)

    @contextmanager
    def _parent_directory_fd(self, *, create: bool) -> Iterator[int]:
        """Use the locked directory descriptor, or open the parent without following it."""
        if self._directory_fd is not None:
            self._assert_directory_identity(self._directory_fd)
            yield self._directory_fd
            return
        parent = self.path.parent
        if create:
            try:
                os.mkdir(parent, 0o700)
            except FileExistsError:
                pass
        try:
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            raise
        try:
            self._assert_directory_identity(parent_fd)
            yield parent_fd
        finally:
            os.close(parent_fd)

    def _assert_directory_identity(self, directory_fd: int) -> None:
        """Refuse to continue if the locked directory inode was replaced."""
        parent_stat = os.fstat(directory_fd)
        if (
            self.state_dir_identity is not None
            and (parent_stat.st_dev, parent_stat.st_ino) != self.state_dir_identity
        ):
            raise ChimeraError(
                code="state_write_failed",
                message="The Chimera state directory was replaced after it was locked.",
                suggestion="Restart the server and do not swap the state directory while it is in use.",
                status=500,
            )

    def _copy_records(self) -> dict[str, ContainerRecord]:
        """Return a detached copy of the current durable authority."""
        return {name: self._copy(record) for name, record in self._records.items()}

    @staticmethod
    def _copy(record: ContainerRecord) -> ContainerRecord:
        """Return a detached model so callers cannot mutate cached state."""
        return model_copy(record, deep=True)

    @staticmethod
    def now() -> datetime:
        """Return a timezone-aware timestamp for lifecycle transitions."""
        return datetime.now(UTC)
