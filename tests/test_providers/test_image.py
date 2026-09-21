"""Safety contracts for image provider observation, import, and catalog semantics."""

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from chimera.errors import ChimeraError
from chimera.images.identity import effective_from_source
from chimera.images.resolver import ResolvedImageArtifact
from chimera.models.image import ImageSourceSpec
from chimera.providers.base import ProviderStatus
from chimera.providers.image import ImageProvider
from chimera.utils.systemd import CommandResult


def _source_image(*, artifact_kind: str = "rootfs"):
    source = ImageSourceSpec(
        name="example",
        url="https://images.example/releases/",
        metadata_verify="tls",
    )
    return effective_from_source(source, "example:server:24.04:amd64", None, artifact_kind)


def _artifact(*, digest: str, size: int, ftype: str = "root.tar.xz", kind: str = "rootfs"):
    return ResolvedImageArtifact(
        url=f"https://images.example/releases/{ftype}",
        sha256=digest,
        size=size,
        serial="20240202.10",
        product="example:server:24.04:amd64",
        architecture="amd64",
        source="example",
        ftype=ftype,
        artifact_kind=kind,
    )


def _commands_containing(run_command: AsyncMock, token: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for call in run_command.await_args_list:
        cmd = call.args[0]
        if isinstance(cmd, list) and token in cmd:
            commands.append(cmd)
    return commands


@pytest.mark.asyncio
async def test_image_observation_error_cannot_trigger_pull(tmp_path):
    """An unavailable machinectl result must not be treated as a missing image."""
    provider = ImageProvider()
    provider.machines_dir = tmp_path
    provider.status = AsyncMock(return_value=ProviderStatus.ERROR)

    with (
        patch("chimera.providers.image.run_command", new_callable=AsyncMock) as run_command,
        pytest.raises(ChimeraError, match="Could not determine"),
    ):
        await provider.present(_source_image())

    run_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_checksum_mismatch_prevents_machinectl_import(tmp_path, monkeypatch):
    """A SimpleStreams digest mismatch must never reach local image import."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    provider = ImageProvider()
    provider.machines_dir = tmp_path
    provider.status = AsyncMock(return_value=ProviderStatus.ABSENT)
    payload = b"rootfs-bytes"
    artifact = _artifact(digest="00" * 32, size=len(payload))

    async def fake_chunks(url, *, source_base=None, timeout=None, chunk_size=None):
        yield payload

    with (
        patch("chimera.providers.image.resolve_image_artifact", AsyncMock(return_value=artifact)),
        patch("chimera.images.resolver.fetch_https_chunks", fake_chunks),
        patch("chimera.providers.image.native_debian_architecture", return_value="amd64"),
        patch("chimera.providers.image.run_command", new_callable=AsyncMock) as run_command,
        pytest.raises(ChimeraError) as caught,
    ):
        await provider.present(_source_image())

    assert caught.value.code == "image_integrity_failed"
    assert _commands_containing(run_command, "import-tar") == []


@pytest.mark.asyncio
async def test_size_mismatch_prevents_import(tmp_path, monkeypatch):
    """Advertised size is checked after a matching digest."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    provider = ImageProvider()
    provider.machines_dir = tmp_path
    provider.status = AsyncMock(return_value=ProviderStatus.ABSENT)
    payload = b"rootfs-bytes"
    artifact = _artifact(digest=hashlib.sha256(payload).hexdigest(), size=len(payload) + 1)

    async def fake_chunks(url, *, source_base=None, timeout=None, chunk_size=None):
        yield payload

    with (
        patch("chimera.providers.image.resolve_image_artifact", AsyncMock(return_value=artifact)),
        patch("chimera.images.resolver.fetch_https_chunks", fake_chunks),
        patch("chimera.providers.image.native_debian_architecture", return_value="amd64"),
        patch("chimera.providers.image.run_command", new_callable=AsyncMock) as run_command,
        pytest.raises(ChimeraError) as caught,
    ):
        await provider.present(_source_image())

    assert caught.value.code == "image_integrity_failed"
    assert _commands_containing(run_command, "import-tar") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("importctl_available", [True, False])
async def test_verified_tar_reaches_exactly_one_local_import(
    tmp_path, monkeypatch, importctl_available
):
    """A digest-matching SimpleStreams tar is imported once through systemd."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    provider = ImageProvider()
    provider.machines_dir = tmp_path
    provider.status = AsyncMock(return_value=ProviderStatus.ABSENT)
    payload = b"rootfs-bytes"
    artifact = _artifact(digest=hashlib.sha256(payload).hexdigest(), size=len(payload))
    image = _source_image()

    async def fake_chunks(url, *, source_base=None, timeout=None, chunk_size=None):
        yield payload

    async def fake_run(cmd, **kwargs):
        return CommandResult(returncode=0, stdout="ReadOnly=yes\n")

    def which(name: str) -> str | None:
        if name == "importctl":
            return "/usr/bin/importctl" if importctl_available else None
        return f"/usr/bin/{name}"

    with (
        patch("chimera.providers.image.resolve_image_artifact", AsyncMock(return_value=artifact)),
        patch("chimera.images.resolver.fetch_https_chunks", fake_chunks),
        patch("chimera.providers.image.native_debian_architecture", return_value="amd64"),
        patch("chimera.providers.image.shutil.which", side_effect=which),
        patch(
            "chimera.providers.image.run_command", new_callable=AsyncMock, side_effect=fake_run
        ) as run_command,
    ):
        await provider.present(image)

    imports = _commands_containing(run_command, "import-tar")
    assert len(imports) == 1
    command = imports[0]
    assert command[-1] == image.local_image_name
    if importctl_available:
        assert command[:4] == ["importctl", "--class=machine", "--read-only", "import-tar"]
    else:
        assert command[:3] == ["machinectl", "--read-only", "import-tar"]


@pytest.mark.asyncio
@pytest.mark.parametrize("importctl_available", [True, False])
async def test_verified_squashfs_reaches_read_only_and_unmounts(
    tmp_path, monkeypatch, importctl_available
):
    """Successful squashfs import finishes read-only and unmounts the source."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    provider = ImageProvider()
    provider.machines_dir = tmp_path
    provider.status = AsyncMock(return_value=ProviderStatus.ABSENT)
    payload = b"squashfs-bytes"
    artifact = _artifact(
        digest=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        ftype="squashfs",
    )
    image = _source_image()

    async def fake_chunks(url, *, source_base=None, timeout=None, chunk_size=None):
        yield payload

    read_only = False

    async def fake_run(cmd, **kwargs):
        nonlocal read_only
        if isinstance(cmd, list) and "read-only" in cmd:
            read_only = True
            return CommandResult(returncode=0, stdout="")
        if isinstance(cmd, list) and "show-image" in cmd:
            stdout = "ReadOnly=yes\n" if read_only else "ReadOnly=no\n"
            return CommandResult(returncode=0, stdout=stdout)
        return CommandResult(returncode=0, stdout="")

    def which(name: str) -> str | None:
        if name == "importctl":
            return "/usr/bin/importctl" if importctl_available else None
        return f"/usr/bin/{name}"

    with (
        patch("chimera.providers.image.resolve_image_artifact", AsyncMock(return_value=artifact)),
        patch("chimera.images.resolver.fetch_https_chunks", fake_chunks),
        patch("chimera.providers.image.native_debian_architecture", return_value="amd64"),
        patch("chimera.providers.image.normalize_nspawn_machine_id"),
        patch("chimera.providers.image.shutil.which", side_effect=which),
        patch(
            "chimera.providers.image.run_command", new_callable=AsyncMock, side_effect=fake_run
        ) as run_command,
    ):
        await provider.present(image)

    mounts = _commands_containing(run_command, "mount")
    assert any(cmd[:3] == ["mount", "-t", "squashfs"] for cmd in mounts)
    imports = _commands_containing(run_command, "import-fs")
    assert len(imports) == 1
    if importctl_available:
        assert imports[0][:3] == ["importctl", "--class=machine", "import-fs"]
    else:
        assert imports[0][:2] == ["machinectl", "import-fs"]
    assert any(
        isinstance(call.args[0], list)
        and call.args[0][:3] == ["machinectl", "read-only", image.local_image_name]
        for call in run_command.await_args_list
    )
    assert any(
        isinstance(call.args[0], list) and call.args[0][0] == "umount"
        for call in run_command.await_args_list
    )


@pytest.mark.asyncio
async def test_import_failure_still_unmounts_squashfs(tmp_path, monkeypatch):
    """Mount cleanup runs when filesystem import fails."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    provider = ImageProvider()
    provider.machines_dir = tmp_path
    provider.status = AsyncMock(return_value=ProviderStatus.ABSENT)
    payload = b"squashfs-bytes"
    artifact = _artifact(
        digest=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        ftype="squashfs",
    )

    async def fake_chunks(url, *, source_base=None, timeout=None, chunk_size=None):
        yield payload

    async def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and "import-fs" in cmd:
            raise ChimeraError(code="host_operation_failed", message="import failed", status=502)
        return CommandResult(returncode=0, stdout="")

    with (
        patch("chimera.providers.image.resolve_image_artifact", AsyncMock(return_value=artifact)),
        patch("chimera.images.resolver.fetch_https_chunks", fake_chunks),
        patch("chimera.providers.image.native_debian_architecture", return_value="amd64"),
        patch("chimera.providers.image.shutil.which", return_value="/usr/bin/importctl"),
        patch(
            "chimera.providers.image.run_command", new_callable=AsyncMock, side_effect=fake_run
        ) as run_command,
        pytest.raises(ChimeraError, match="import failed"),
    ):
        await provider.present(_source_image())

    assert any(
        isinstance(call.args[0], list) and call.args[0][0] == "umount"
        for call in run_command.await_args_list
    )
    assert _commands_containing(run_command, "remove") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "code"),
    [
        ("ReadOnly=yes\n", None),
        ("ReadOnly=no\n", "image_cache_incomplete"),
        ("Name=cache\n", "host_observation_failed"),
    ],
)
async def test_existing_cache_is_accepted_only_when_read_only(tmp_path, stdout, code):
    """A pre-existing cache is reused only when systemd reports ReadOnly=yes."""
    provider = ImageProvider()
    provider.machines_dir = tmp_path
    image = _source_image()
    (tmp_path / image.local_image_name).mkdir()

    async def fake_run(cmd, **kwargs):
        return CommandResult(returncode=0, stdout=stdout)

    with (
        patch("chimera.providers.image.resolve_image_artifact", AsyncMock()) as resolve,
        patch(
            "chimera.providers.image.run_command", new_callable=AsyncMock, side_effect=fake_run
        ) as run_command,
    ):
        if code is None:
            await provider.present(image)
        else:
            with pytest.raises(ChimeraError) as caught:
                await provider.present(image)
            assert caught.value.code == code
    resolve.assert_not_awaited()
    assert _commands_containing(run_command, "read-only") == []
    assert _commands_containing(run_command, "remove") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "code"),
    [
        ("ReadOnly=no\n", "image_cache_incomplete"),
        ("Name=cache\n", "host_observation_failed"),
    ],
)
async def test_new_import_requires_completed_read_only_cache(tmp_path, monkeypatch, stdout, code):
    """A new import is kept only when systemd then reports ReadOnly=yes."""
    monkeypatch.setenv("TMPDIR", str(tmp_path / "downloads"))
    (tmp_path / "downloads").mkdir()
    provider = ImageProvider()
    provider.machines_dir = tmp_path / "machines"
    provider.machines_dir.mkdir()
    payload = b"rootfs-bytes"
    artifact = _artifact(digest=hashlib.sha256(payload).hexdigest(), size=len(payload))
    image = _source_image()

    async def fake_chunks(url, *, source_base=None, timeout=None, chunk_size=None):
        yield payload

    async def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and "import-tar" in cmd:
            (provider.machines_dir / image.local_image_name).mkdir()
            return CommandResult(returncode=0, stdout="")
        if isinstance(cmd, list) and "show-image" in cmd:
            return CommandResult(returncode=0, stdout=stdout)
        return CommandResult(returncode=0, stdout="")

    with (
        patch("chimera.providers.image.resolve_image_artifact", AsyncMock(return_value=artifact)),
        patch("chimera.images.resolver.fetch_https_chunks", fake_chunks),
        patch("chimera.providers.image.native_debian_architecture", return_value="amd64"),
        patch("chimera.providers.image.shutil.which", return_value="/usr/bin/importctl"),
        patch(
            "chimera.providers.image.run_command", new_callable=AsyncMock, side_effect=fake_run
        ) as run_command,
        pytest.raises(ChimeraError) as caught,
    ):
        await provider.present(image)

    assert caught.value.code == code
    assert any(
        isinstance(call.args[0], list)
        and call.args[0] == ["machinectl", "remove", image.local_image_name]
        for call in run_command.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ftype", "kind", "token"),
    [
        ("root.tar.xz", "rootfs", "import-tar"),
        ("squashfs", "rootfs", "import-fs"),
        ("disk-kvm.img", "disk", "import-raw"),
    ],
)
async def test_failed_import_removes_materialized_cache(tmp_path, monkeypatch, ftype, kind, token):
    """An import that leaves a new cache is rolled back and keeps the primary error."""
    monkeypatch.setenv("TMPDIR", str(tmp_path / "downloads"))
    (tmp_path / "downloads").mkdir()
    provider = ImageProvider()
    provider.machines_dir = tmp_path / "machines"
    provider.machines_dir.mkdir()
    payload = b"artifact-bytes"
    artifact = _artifact(
        digest=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        ftype=ftype,
        kind=kind,
    )
    image = _source_image(artifact_kind=kind)

    async def fake_chunks(url, *, source_base=None, timeout=None, chunk_size=None):
        yield payload

    async def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and token in cmd:
            (provider.machines_dir / image.local_image_name).mkdir()
            raise ChimeraError(code="host_operation_failed", message="import failed", status=502)
        return CommandResult(returncode=0, stdout="")

    with (
        patch("chimera.providers.image.resolve_image_artifact", AsyncMock(return_value=artifact)),
        patch("chimera.images.resolver.fetch_https_chunks", fake_chunks),
        patch("chimera.providers.image.native_debian_architecture", return_value="amd64"),
        patch("chimera.providers.image.shutil.which", return_value="/usr/bin/importctl"),
        patch(
            "chimera.providers.image.run_command", new_callable=AsyncMock, side_effect=fake_run
        ) as run_command,
        pytest.raises(ChimeraError, match="import failed"),
    ):
        await provider.present(image)

    assert any(
        isinstance(call.args[0], list)
        and call.args[0] == ["machinectl", "remove", image.local_image_name]
        for call in run_command.await_args_list
    )


@pytest.mark.asyncio
async def test_squashfs_unmount_failure_on_success_is_operation_failure(tmp_path, monkeypatch):
    """A failed unmount fails the import and rolls back the new cache."""
    monkeypatch.setenv("TMPDIR", str(tmp_path / "downloads"))
    (tmp_path / "downloads").mkdir()
    provider = ImageProvider()
    provider.machines_dir = tmp_path / "machines"
    provider.machines_dir.mkdir()
    payload = b"squashfs-bytes"
    artifact = _artifact(
        digest=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        ftype="squashfs",
    )
    image = _source_image()

    async def fake_chunks(url, *, source_base=None, timeout=None, chunk_size=None):
        yield payload

    async def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and "import-fs" in cmd:
            (provider.machines_dir / image.local_image_name).mkdir()
            return CommandResult(returncode=0, stdout="")
        if isinstance(cmd, list) and cmd and cmd[0] == "umount":
            raise ChimeraError(code="host_operation_failed", message="umount failed", status=502)
        if isinstance(cmd, list) and "show-image" in cmd:
            return CommandResult(returncode=0, stdout="ReadOnly=yes\n")
        return CommandResult(returncode=0, stdout="")

    with (
        patch("chimera.providers.image.resolve_image_artifact", AsyncMock(return_value=artifact)),
        patch("chimera.images.resolver.fetch_https_chunks", fake_chunks),
        patch("chimera.providers.image.native_debian_architecture", return_value="amd64"),
        patch("chimera.providers.image.normalize_nspawn_machine_id"),
        patch("chimera.providers.image.shutil.which", return_value="/usr/bin/importctl"),
        patch(
            "chimera.providers.image.run_command", new_callable=AsyncMock, side_effect=fake_run
        ) as run_command,
        pytest.raises(ChimeraError, match="umount failed"),
    ):
        await provider.present(image)

    assert any(
        isinstance(call.args[0], list)
        and call.args[0] == ["machinectl", "remove", image.local_image_name]
        for call in run_command.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("importctl_available", [True, False])
async def test_verified_disk_uses_import_raw(tmp_path, monkeypatch, importctl_available):
    """Disk artifacts use native systemd import-raw."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    provider = ImageProvider()
    provider.machines_dir = tmp_path
    provider.status = AsyncMock(return_value=ProviderStatus.ABSENT)
    payload = b"qcow2-bytes"
    artifact = _artifact(
        digest=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        ftype="disk-kvm.img",
        kind="disk",
    )
    image = _source_image(artifact_kind="disk")

    async def fake_chunks(url, *, source_base=None, timeout=None, chunk_size=None):
        yield payload

    async def fake_run(cmd, **kwargs):
        return CommandResult(returncode=0, stdout="ReadOnly=yes\n")

    def which(name: str) -> str | None:
        if name == "importctl":
            return "/usr/bin/importctl" if importctl_available else None
        return f"/usr/bin/{name}"

    with (
        patch("chimera.providers.image.resolve_image_artifact", AsyncMock(return_value=artifact)),
        patch("chimera.images.resolver.fetch_https_chunks", fake_chunks),
        patch("chimera.providers.image.native_debian_architecture", return_value="amd64"),
        patch("chimera.providers.image.shutil.which", side_effect=which),
        patch(
            "chimera.providers.image.run_command", new_callable=AsyncMock, side_effect=fake_run
        ) as run_command,
    ):
        await provider.present(image)

    imports = _commands_containing(run_command, "import-raw")
    assert len(imports) == 1
    command = imports[0]
    assert command[-1] == image.local_image_name
    if importctl_available:
        assert command[:4] == ["importctl", "--class=machine", "--read-only", "import-raw"]
    else:
        assert command[:3] == ["machinectl", "--read-only", "import-raw"]
