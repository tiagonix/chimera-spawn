"""Privileged disposable-system lifecycle gates.

These tests require an explicitly designated disposable systemd/nspawn testbed
and a built package path. Root or systemd availability alone is not permission.
Without opt-in they skip. With opt-in, missing prerequisites are setup failures.

Reboot is a documented manual gate, not implemented automation. The procedure
is recorded in docs/development/release-qualification.md.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import time
import uuid
from pathlib import Path

import pytest

from chimera.server.api import interpret_guest_unit_show

pytestmark = pytest.mark.privileged

PREFIX = f"chimera-test-r5-{os.getpid()}-{uuid.uuid4().hex[:8]}"

COMMAND_TIMEOUT = 600

_STRIP_ENV = (
    "PYTHONPATH",
    "PYTHONHOME",
    "CHIMERA_HOST",
    "CHIMERA_SOCKET",
    "CHIMERA_TLS_CA",
    "CHIMERA_TLS_CERT",
    "CHIMERA_TLS_KEY",
)


def _installed_env() -> dict[str, str]:
    """Environment for installed chimeractl/chimera-server."""
    env = os.environ.copy()
    for key in _STRIP_ENV:
        env.pop(key, None)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["NEEDRESTART_SUSPEND"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["TERM"] = "xterm-256color"
    return env


def _opt_in() -> bool:
    return os.environ.get("CHIMERA_PRIVILEGED_TESTBED") == "1"


def _require_testbed() -> Path:
    if not _opt_in():
        pytest.skip("Privileged disposable-system gates require CHIMERA_PRIVILEGED_TESTBED=1")
    missing: list[str] = []
    if os.environ.get("CHIMERA_TESTBED_CONFIRMED") != "1":
        missing.append("CHIMERA_TESTBED_CONFIRMED=1")
    deb = os.environ.get("CHIMERA_TEST_DEB")
    if not deb:
        missing.append("CHIMERA_TEST_DEB")
    if missing:
        pytest.fail(
            "Privileged testbed requested but required setup is missing: " + ", ".join(missing)
        )
    path = Path(deb)
    if not path.exists():
        pytest.fail(f"CHIMERA_TEST_DEB is not a file or directory: {path}")
    return path


def _deb_fields(path: Path) -> tuple[str, str]:
    name = subprocess.check_output(["dpkg-deb", "-f", str(path), "Package"], text=True).strip()
    version = subprocess.check_output(["dpkg-deb", "-f", str(path), "Version"], text=True).strip()
    return name, version


def _select_role_debs(path: Path) -> list[Path]:
    """Pick matching common/client/server/meta artifacts by package metadata."""
    if path.is_file():
        candidates = sorted(path.parent.glob("*.deb"))
        wanted_version = _deb_fields(path)[1]
    else:
        candidates = sorted(path.glob("*.deb"))
        wanted_version = None
    by_name: dict[str, list[tuple[str, Path]]] = {}
    for deb in candidates:
        try:
            name, version = _deb_fields(deb)
        except subprocess.CalledProcessError:
            continue
        by_name.setdefault(name, []).append((version, deb))
    required = (
        "python3-chimera-spawn",
        "chimera-spawn-client",
        "chimera-spawn-server",
        "chimera-spawn",
    )
    selected: list[Path] = []
    versions: set[str] = set()
    for name in required:
        matches = by_name.get(name, [])
        if wanted_version:
            matches = [item for item in matches if item[0] == wanted_version]
        if not matches:
            pytest.fail(f"No {name} artifact matching {path}")
        matches.sort()
        version, deb = matches[-1]
        versions.add(version)
        selected.append(deb)
    if len(versions) != 1:
        pytest.fail(f"Role artifacts must share one version, got {versions}")
    return selected


def _install_packages(deb: Path) -> list[str]:
    files = _select_role_debs(deb)
    recorded = [str(path) for path in files]
    _run(["apt-get", "install", "-y", "--reinstall", *recorded])
    return recorded


def _run(
    command: list[str], *, require: bool = True, timeout: int = COMMAND_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Run a host command. Stdin is inherited; this is not a non-TTY exec oracle."""
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_installed_env(),
    )
    result.command = command  # type: ignore[attr-defined]
    if require and result.returncode != 0:
        pytest.fail(
            f"command failed ({result.returncode}): {command}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _run_nontty(
    command: list[str],
    *,
    stdin: int | object | None = None,
    input_text: str | None = None,
    require: bool = True,
    timeout: int = COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run with non-TTY stdin. Captured stdout is not enough to select --pipe."""
    if input_text is not None and stdin is not None:
        raise ValueError("stdin and input_text are mutually exclusive")
    kwargs: dict[str, object] = {
        "args": command,
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": _installed_env(),
    }
    if input_text is not None:
        kwargs["input"] = input_text
    else:
        kwargs["stdin"] = subprocess.DEVNULL if stdin is None else stdin
    result = subprocess.run(**kwargs)  # type: ignore[arg-type]
    result.command = command  # type: ignore[attr-defined]
    if require and result.returncode != 0:
        pytest.fail(
            f"command failed ({result.returncode}): {command}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _assert_exec_does_not_inject_term(argv: list[str]) -> None:
    """Guest must not see the client TERM for /dev/null stdin or a real pipe."""
    expected = "TERM=<xterm-256color>"
    closed = _run_nontty(argv, stdin=subprocess.DEVNULL)
    assert expected not in closed.stdout.replace("\r", ""), closed.stdout
    piped = _run_nontty(argv, input_text="")
    assert expected not in piped.stdout.replace("\r", ""), piped.stdout


def _qualification_image() -> str:
    return os.environ.get("CHIMERA_TEST_IMAGE", "resolute")


def _qualification_source() -> str:
    return os.environ.get("CHIMERA_TEST_IMAGE_SOURCE", "ubuntu")


def _ctl(
    *args: str, expect_failure: bool = False, timeout: int = COMMAND_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Run chimeractl, optionally expecting a nonzero status."""
    command = ["chimeractl", *args]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_installed_env(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        pytest.fail(f"command timed out: {command}\nstdout:\n{stdout}\nstderr:\n{stderr}")
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    result.pid = process.pid  # type: ignore[attr-defined]
    if expect_failure:
        if result.returncode == 0:
            pytest.fail(f"expected failure, succeeded: {command}\nstdout:\n{stdout}")
        return result
    if result.returncode != 0:
        pytest.fail(
            f"command failed ({result.returncode}): {command}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    return result


def _info(name: str) -> dict[str, object]:
    payload = json.loads(_ctl("info", name, "--format", "json").stdout)
    return payload["containers"][name]


class GuestObservationError(RuntimeError):
    """Raised when the guest manager cannot authoritatively report unit state."""


def _observe_guest_unit(machine: str, unit: str) -> str:
    """Return absent/inactive/active/transitional, or raise if unobservable."""
    result = _run(
        [
            "systemctl",
            f"--machine={machine}",
            "show",
            unit,
            "-p",
            "ActiveState",
            "-p",
            "LoadState",
        ],
        require=False,
    )
    observed = interpret_guest_unit_show(result.returncode, result.stdout, result.stderr)
    if observed.status == "unknown":
        raise GuestObservationError(
            f"cannot observe {unit} in {machine}: exit {result.returncode} "
            f"status={observed.status} detail={observed.detail}\n"
            f"{result.stderr or result.stdout}"
        )
    return observed.status


def _guest_exec_units(machine: str) -> list[str]:
    """List Chimera exec units visible inside a running machine."""
    result = _run(
        [
            "systemctl",
            f"--machine={machine}",
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "--full",
        ],
        require=False,
    )
    if result.returncode != 0:
        raise GuestObservationError(
            f"cannot list units in {machine}: exit {result.returncode}\n{result.stderr}"
        )
    units: list[str] = []
    for line in result.stdout.splitlines():
        name = line.split(None, 1)[0] if line.strip() else ""
        if name.startswith("chimera-exec-") and name.endswith(".service"):
            units.append(name)
    return units


def _active_guest_exec_units(machine: str) -> list[str]:
    """Return Chimera exec units that are still active or transitional."""
    active: list[str] = []
    for unit in _guest_exec_units(machine):
        state = _observe_guest_unit(machine, unit)
        if state in {"active", "transitional"}:
            active.append(unit)
    return active


def _argv_sequence_present(text: str, parts: list[str]) -> bool:
    """True when printf-style output contains the exact argument sequence."""
    lines = text.replace("\r", "").split("\n")
    length = len(parts)
    return any(lines[index : index + length] == parts for index in range(len(lines)))


def _pty_exec(
    argv: list[str], *, answer: bytes | None = None, timeout: float = 30
) -> tuple[str, int, bool]:
    """Drive an interactive chimeractl exec through a local PTY."""
    master, slave = pty.openpty()
    process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=_installed_env())
    os.close(slave)
    collected = bytearray()
    deadline = time.monotonic() + timeout
    answered = False
    first_while_running = False
    helper_killed = False
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    collected.extend(chunk)
                    if b"FIRST" in collected and process.poll() is None:
                        first_while_running = True
                    if answer is not None and not answered and b"PROMPT" in collected:
                        os.write(master, answer if answer.endswith(b"\n") else answer + b"\n")
                        answered = True
            if process.poll() is not None:
                drain_until = time.monotonic() + 1
                while time.monotonic() < drain_until:
                    ready, _, _ = select.select([master], [], [], 0.05)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    collected.extend(chunk)
                break
        if process.poll() is None:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                helper_killed = True
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        else:
            process.wait(timeout=5)
    finally:
        if process.poll() is None:
            helper_killed = True
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        os.close(master)
    text = collected.decode("utf-8", errors="replace")
    if helper_killed:
        pytest.fail(
            "PTY helper timed out and sent SIGKILL; that is not evidence of the expected "
            f"session outcome.\ncommand: {argv}\noutput:\n{text}"
        )
    return text, process.returncode or 0, first_while_running


def _drive_native_shell(
    argv: list[str], unique_name: str, privileged_package: dict[str, object]
) -> float:
    """Drive installed chimeractl shell; return Ctrl-C recovery seconds."""
    token = uuid.uuid4().hex[:8]
    one = f"RESULT:{token}-one"
    two = f"RESULT:{token}-two"
    after = f"AFTER_INT_{token}"
    keep_unit = f"chimera-keep-{uuid.uuid4().hex[:8]}.service"
    keeper = _run(
        [
            "systemd-run",
            f"--machine={unique_name}",
            "--quiet",
            "--collect",
            f"--unit={keep_unit}",
            "sleep",
            "120",
        ],
        require=False,
    )
    if keeper.returncode != 0:
        pytest.fail(f"could not start unrelated guest unit: {keeper.stderr}")
    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    process = subprocess.Popen(
        argv,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=_installed_env(),
    )
    os.close(slave)
    collected = bytearray()
    deadline = time.monotonic() + 45
    stage = "wait_prompt"
    first_while_running = False
    helper_killed = False
    interrupt_at: float | None = None
    interrupt_mark = 0
    interrupt_elapsed: float | None = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    collected.extend(chunk)
            if one.encode() in collected and process.poll() is None:
                first_while_running = True
            if stage == "wait_prompt" and (b"# " in collected or b"$ " in collected):
                os.write(master, f"printf 'RESULT:%s\\n' {token}-one\n".encode())
                stage = "one"
            elif stage == "one" and one.encode() in collected:
                os.write(master, f"printf 'RESULT:%s\\n' {token}-two\n".encode())
                stage = "two"
            elif stage == "two" and two.encode() in collected:
                os.write(master, b"printf 'TERM=<%s>\\n' \"$TERM\"\n")
                stage = "term"
            elif stage == "term" and b"TERM=<xterm-256color>" in collected.replace(b"\r", b""):
                os.write(master, b"printf PROMPT; read line; printf 'ECHO:%s\\n' \"$line\"\n")
                stage = "prompt"
            elif stage == "prompt" and b"PROMPT" in collected:
                os.write(master, b"pong\n")
                stage = "echo"
            elif stage == "echo" and b"ECHO:pong" in collected:
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 31, 92, 0, 0))
                os.kill(process.pid, signal.SIGWINCH)
                time.sleep(0.5)
                os.write(master, b"stty size\n")
                stage = "size"
            elif stage == "size" and b"31 92" in collected:
                os.write(
                    master,
                    (
                        "if ! command -v tput >/dev/null 2>&1; then "
                        f"printf 'TPUT%s:%s\\n' ABSENT {token}; "
                        "elif tput clear >/dev/null 2>&1 && tput cup 1 1 >/dev/null 2>&1; then "
                        f'printf \'TPUT%s:%s:%s:%s\\n\' OK {token} "$(tput cols)" "$(tput lines)"; '
                        f"else printf 'TPUT%s:%s\\n' FAIL {token}; fi\n"
                    ).encode(),
                )
                stage = "tput"
            elif stage == "tput" and (
                f"TPUTOK:{token}:".encode() in collected.replace(b"\r", b"")
                or f"TPUTABSENT:{token}".encode() in collected.replace(b"\r", b"")
                or f"TPUTFAIL:{token}".encode() in collected.replace(b"\r", b"")
            ):
                os.write(master, b"sleep 30\n")
                stage = "sleep"
            elif stage == "sleep" and process.poll() is None:
                time.sleep(0.4)
                if process.poll() is not None:
                    pytest.fail("shell exited before Ctrl-C was sent")
                os.write(master, b"\x03")
                interrupt_at = time.monotonic()
                interrupt_mark = len(collected)
                stage = "recover"
            elif stage == "recover":
                if interrupt_at is None:
                    pytest.fail("interrupt timestamp missing")
                elapsed = time.monotonic() - interrupt_at
                suffix = bytes(collected[interrupt_mark:]).replace(b"\r", b"")
                recovered = b"# " in suffix or b"$ " in suffix
                if recovered:
                    if elapsed > 5:
                        pytest.fail(
                            f"prompt after Ctrl-C took {elapsed:.3f}s; that is not interrupt proof"
                        )
                    interrupt_elapsed = elapsed
                    os.write(master, f"printf 'AFTER_INT_%s\\n' {token}; exit\n".encode())
                    stage = "exit"
                elif process.poll() is not None:
                    pytest.fail(
                        f"shell exited {elapsed:.3f}s after Ctrl-C before a recovered prompt"
                    )
                elif elapsed > 5:
                    text = collected.decode("utf-8", errors="replace")
                    pytest.fail(f"no prompt within 5s of Ctrl-C (waited {elapsed:.3f}s)\n{text}")
            if process.poll() is not None:
                break
        if process.poll() is None:
            os.write(master, b"exit\n")
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                helper_killed = True
                process.kill()
                process.wait(timeout=5)
        text = collected.decode("utf-8", errors="replace")
        if helper_killed:
            pytest.fail(
                "shell helper timed out and sent SIGKILL; that is not session evidence.\n" + text
            )
        if interrupt_elapsed is None:
            pytest.fail(f"Ctrl-C recovery was not observed before session end.\n{text}")
        assert one in text
        assert first_while_running
        assert two in text
        assert "TERM=<xterm-256color>" in text.replace("\r", "")
        normalized = text.replace("\r", "")
        assert f"TPUTFAIL:{token}" not in normalized
        if f"TPUTABSENT:{token}" not in normalized:
            assert f"TPUTOK:{token}:92:31" in normalized
        assert "ECHO:pong" in text
        assert "31 92" in text.replace("\r", "")
        assert after in text
        assert process.returncode == 0
        assert not _active_guest_exec_units(unique_name)
        assert _observe_guest_unit(unique_name, keep_unit) == "active"
        log = privileged_package["log"]
        assert isinstance(log, list)
        log.append(("interrupt_s", unique_name, interrupt_elapsed))
        return interrupt_elapsed
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        os.close(master)
        _run(
            ["systemctl", f"--machine={unique_name}", "stop", keep_unit],
            require=False,
        )


def _pty_exec_ctrl_c(argv: list[str]) -> tuple[str, int, float]:
    """Send Ctrl-C after WAITING; fail if the helper is killed or sleep finishes."""
    master, slave = pty.openpty()
    process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=_installed_env())
    os.close(slave)
    collected = bytearray()
    deadline = time.monotonic() + 15
    interrupt_at: float | None = None
    helper_killed = False
    exited_at: float | None = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    collected.extend(chunk)
                    if interrupt_at is None and b"WAITING" in collected and process.poll() is None:
                        time.sleep(0.4)
                        if process.poll() is not None:
                            break
                        os.write(master, b"\x03")
                        interrupt_at = time.monotonic()
            if process.poll() is not None:
                exited_at = time.monotonic()
                drain_until = time.monotonic() + 1
                while time.monotonic() < drain_until:
                    ready, _, _ = select.select([master], [], [], 0.05)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    collected.extend(chunk)
                break
        if process.poll() is None:
            helper_killed = True
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        text = collected.decode("utf-8", errors="replace")
        if helper_killed:
            pytest.fail(
                "PTY helper timed out and sent SIGKILL; that is not Ctrl-C evidence.\n" + text
            )
        if interrupt_at is None:
            pytest.fail(f"WAITING never appeared, so Ctrl-C was not sent.\n{text}")
        elapsed = (exited_at or time.monotonic()) - interrupt_at
        if elapsed > 5:
            pytest.fail(f"exec did not exit within 5s of Ctrl-C ({elapsed:.3f}s)\n{text}")
        assert "WAITING" in text
        assert "INTERRUPTED" in text
        assert "NOT_REACHED" not in text
        return text, process.returncode or 0, elapsed
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        os.close(master)


@pytest.fixture(scope="module")
def privileged_package():
    """Install the supplied package on a designated disposable testbed."""
    deb = _require_testbed()
    names: list[str] = []
    try:
        recorded = _install_packages(deb)
        _run(["systemctl", "enable", "--now", "chimera-server"])
        yield {"deb": deb, "log": [("package", recorded, 0)], "names": names}
    finally:
        for name in list(reversed(names)):
            _run(["chimeractl", "delete", name, "--force"], require=False)


@pytest.fixture
def unique_name(privileged_package):
    """Allocate one container name created by this module and delete it later."""
    name = f"{PREFIX}-{uuid.uuid4().hex[:6]}"
    privileged_package["names"].append(name)
    try:
        yield name
    finally:
        _run(["chimeractl", "delete", name, "--force"], require=False)


def test_exec_literal_argv_incremental_output_and_status(privileged_package, unique_name):
    """Production systemd-run exec preserves argv, streams output, and returns status."""
    image = _qualification_image()
    launch = _ctl("launch", image, unique_name, "--source", _qualification_source())
    privileged_package["log"].append(("container", unique_name, launch.returncode))
    printed = _ctl(
        "exec",
        unique_name,
        "--",
        "printf",
        "%s\n",
        "$HOME",
        "a b",
        "'quotes'",
        "back\\slash",
        "ünicode",
        "",
        "--dashed",
    )
    text = printed.stdout.replace("\r", "")
    assert _argv_sequence_present(
        text, ["$HOME", "a b", "'quotes'", "back\\slash", "ünicode", "", "--dashed"]
    )
    failed = _ctl("exec", unique_name, "--", "sh", "-c", "exit 7", expect_failure=True)
    assert failed.returncode == 7
    guest = "echo FIRST; sleep 1; echo SECOND; echo PROMPT; read line; echo ECHO:$line; exit 0"
    streamed, status, first_while_running = _pty_exec(
        ["chimeractl", "exec", unique_name, "--", "sh", "-c", guest],
        answer=b"pong\n",
    )
    assert "FIRST" in streamed
    assert first_while_running
    assert "ECHO:pong" in streamed
    assert status == 0
    payload = bytes(range(256)) * 1024
    expected = hashlib.sha256(payload).hexdigest()
    large = subprocess.run(
        ["chimeractl", "exec", unique_name, "--", "sha256sum"],
        input=payload,
        capture_output=True,
        timeout=60,
        check=False,
        env=_installed_env(),
    )
    assert large.returncode == 0, large.stderr.decode("utf-8", errors="replace")
    digest_line = large.stdout.decode("utf-8", errors="replace").replace("\r", "").split()
    assert digest_line and digest_line[0] == expected
    empty = hashlib.sha256(b"").hexdigest()
    with open("/dev/null", "rb") as handle:
        null_run = subprocess.run(
            ["chimeractl", "exec", unique_name, "--", "sha256sum"],
            stdin=handle,
            capture_output=True,
            timeout=30,
            check=False,
            env=_installed_env(),
        )
    assert null_run.returncode == 0, null_run.stderr.decode("utf-8", errors="replace")
    assert null_run.stdout.decode("utf-8", errors="replace").replace("\r", "").split()[0] == empty
    no_nl = b"abc"
    no_nl_run = subprocess.run(
        ["chimeractl", "exec", unique_name, "--", "sha256sum"],
        input=no_nl,
        capture_output=True,
        timeout=30,
        check=False,
        env=_installed_env(),
    )
    assert no_nl_run.returncode == 0, no_nl_run.stderr.decode("utf-8", errors="replace")
    assert (
        no_nl_run.stdout.decode("utf-8", errors="replace").replace("\r", "").split()[0]
        == hashlib.sha256(no_nl).hexdigest()
    )


def test_native_shell_session(privileged_package, unique_name):
    """Production machinectl shell preserves multi-command, prompt, resize, and interrupt."""
    image = _qualification_image()
    launch = _ctl("launch", image, unique_name, "--source", _qualification_source())
    privileged_package["log"].append(("container", unique_name, launch.returncode))
    elapsed = _drive_native_shell(
        ["chimeractl", "shell", unique_name], unique_name, privileged_package
    )
    print(f"INTERRUPT_ELAPSED local_shell={elapsed:.4f}s", flush=True)
    assert elapsed <= 5


def test_stop_start_and_server_restart_persistence(privileged_package, unique_name):
    """Desired stopped state survives server restart and is not recreated running."""
    image = _qualification_image()
    launch = _ctl("launch", image, unique_name, "--source", _qualification_source())
    privileged_package["log"].append(("container", unique_name, launch.returncode))
    stopped = _ctl("stop", unique_name)
    assert stopped.returncode == 0
    info = _info(unique_name)
    assert info["desired_state"] == "stopped"
    assert info["observed_state"] in {"stopped", "offline", "poweroff"}
    _run(["systemctl", "restart", "chimera-server"])
    deadline = time.monotonic() + 20
    info = None
    last_output = ""
    while time.monotonic() < deadline:
        result = _run(
            ["chimeractl", "info", unique_name, "--format", "json"],
            require=False,
        )
        last_output = result.stdout + result.stderr
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            info = payload["containers"][unique_name]
            if info["desired_state"] == "stopped" and info["observed_state"] in {
                "stopped",
                "offline",
                "poweroff",
            }:
                break
        time.sleep(0.5)
    else:
        pytest.fail(
            "container was not observed stopped after server restart: " f"{info!r}\n{last_output}"
        )
    time.sleep(2)
    later = _info(unique_name)
    assert later["desired_state"] == "stopped"
    assert later["observed_state"] in {"stopped", "offline", "poweroff"}
    started = _ctl("start", unique_name)
    assert started.returncode == 0
    running = _info(unique_name)
    assert running["desired_state"] == "running"
    assert running["observed_state"] in {"running", "online"}


@pytest.fixture
def second_user():
    """Create a disposable user and remove it even if a later assertion fails."""
    name = f"ctu{os.getpid() % 100000}{uuid.uuid4().hex[:6]}"
    _run(["useradd", "--create-home", "--shell", "/bin/sh", name])
    try:
        yield name
    finally:
        _run(["gpasswd", "-d", name, "chimera-admin"], require=False)
        _run(["userdel", "-r", name], require=False)


def test_second_user_denied_until_group_enrollment(privileged_package, second_user):
    """An unenrolled second user is denied, then succeeds after chimera-admin."""
    denied = _run(
        ["runuser", "-u", second_user, "--", "chimeractl", "status"],
        require=False,
    )
    assert denied.returncode != 0
    _run(["usermod", "-aG", "chimera-admin", second_user])
    allowed = _run(
        [
            "runuser",
            "-u",
            second_user,
            "--",
            "sg",
            "chimera-admin",
            "-c",
            "chimeractl status",
        ],
        require=False,
    )
    assert allowed.returncode == 0, allowed.stderr or allowed.stdout


def test_remote_tls_lifecycle_when_explicitly_enabled(privileged_package, unique_name, tmp_path):
    """Optional remote gate uses disposable PKI and a real interactive exec."""
    if os.environ.get("CHIMERA_TEST_REMOTE") != "1":
        pytest.skip("Remote privileged TLS gate requires CHIMERA_TEST_REMOTE=1")
    from tests.support.tls import generate_tls_bundle

    bundle = generate_tls_bundle(tmp_path / "pki")
    tls_dir = Path("/etc/chimera-spawn/tls-test")
    tls_dir.mkdir(parents=True, exist_ok=True)
    server_cert = tls_dir / "server.crt"
    server_key = tls_dir / "server.key"
    client_ca = tls_dir / "client-ca.crt"
    server_cert.write_bytes(bundle.server_cert.read_bytes())
    server_key.write_bytes(bundle.server_key.read_bytes())
    client_ca.write_bytes(bundle.ca_cert.read_bytes())
    server_key.chmod(0o600)
    config_path = Path("/etc/chimera-spawn/chimera.yaml")
    original = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        "\n".join(
            [
                "server:",
                "  reconciliation_interval: 30",
                "  log_level: INFO",
                "  admin_group: chimera-admin",
                "  host: 127.0.0.1",
                "  port: 18080",
                "  tls:",
                f"    certificate: {server_cert}",
                f"    private_key: {server_key}",
                f"    client_ca: {client_ca}",
                "systemd:",
                "  machines_dir: /var/lib/machines",
                "  nspawn_dir: /etc/systemd/nspawn",
                "  system_dir: /etc/systemd/system",
                "",
            ]
        ),
        encoding="utf-8",
    )
    remote = [
        "--host",
        "localhost:18080",
        "--tls-ca",
        str(bundle.ca_cert),
        "--tls-cert",
        str(bundle.client_cert),
        "--tls-key",
        str(bundle.client_key),
    ]
    try:
        _run(["systemctl", "restart", "chimera-server"])
        time.sleep(1)
        status = _run(["chimeractl", "status", *remote])
        assert status.returncode == 0, status.stderr
        image = _qualification_image()
        launch = _run(
            [
                "chimeractl",
                "launch",
                image,
                unique_name,
                "--source",
                _qualification_source(),
                *remote,
            ]
        )
        privileged_package["log"].append(("container", unique_name, launch.returncode))
        assert launch.returncode == 0, launch.stderr
        journal = _run(["journalctl", "-u", "chimera-server", "-n", "200", "--no-pager"])
        assert "transport=tls cert_sha256=" in journal.stdout
        denied = _run(
            [
                "chimeractl",
                "status",
                "--host",
                "localhost:18080",
                "--tls-ca",
                str(bundle.ca_cert),
                "--tls-cert",
                str(bundle.other_client_cert),
                "--tls-key",
                str(bundle.other_client_key),
            ],
            require=False,
        )
        assert denied.returncode != 0
        guest = "echo FIRST; sleep 1; echo SECOND; echo PROMPT; read line; echo ECHO:$line; exit 7"
        argv = ["chimeractl", "exec", unique_name, *remote, "--", "sh", "-c", guest]
        text, status, first_while_running = _pty_exec(argv, answer=b"pong\n")
        assert "FIRST" in text
        assert first_while_running
        assert "ECHO:pong" in text
        assert status == 7
        printed = _run(["chimeractl", "exec", unique_name, *remote, "--", "printf", "%s", "$HOME"])
        assert "$HOME" in printed.stdout.replace("\r", "")
        term_text, term_status, _ = _pty_exec(
            [
                "chimeractl",
                "exec",
                unique_name,
                *remote,
                "--",
                "sh",
                "-c",
                "printf 'TERM=<%s>\\n' \"$TERM\"",
            ]
        )
        assert "TERM=<xterm-256color>" in term_text.replace("\r", "")
        assert term_status == 0
        _assert_exec_does_not_inject_term(
            [
                "chimeractl",
                "exec",
                unique_name,
                *remote,
                "--",
                "sh",
                "-c",
                "printf 'TERM=<%s>\\n' \"${TERM-}\"",
            ]
        )
        guest_int = "trap 'echo INTERRUPTED; exit 130' INT; echo WAITING; sleep 30; echo NOT_REACHED; exit 1"
        _int_text, _int_status, int_elapsed = _pty_exec_ctrl_c(
            ["chimeractl", "exec", unique_name, *remote, "--", "sh", "-c", guest_int]
        )
        print(
            f"INTERRUPT_ELAPSED remote_exec={int_elapsed:.4f}s status={_int_status}",
            flush=True,
        )
        assert int_elapsed <= 5, f"remote exec Ctrl-C took {int_elapsed:.3f}s"
        log = privileged_package["log"]
        log.append(("remote_exec_interrupt_s", unique_name, int_elapsed))
        remote_shell_elapsed = _drive_native_shell(
            ["chimeractl", "shell", unique_name, *remote], unique_name, privileged_package
        )
        print(
            f"INTERRUPT_ELAPSED remote_shell={remote_shell_elapsed:.4f}s",
            flush=True,
        )
        assert remote_shell_elapsed <= 5, f"remote shell Ctrl-C took {remote_shell_elapsed:.3f}s"
        sleeper = subprocess.Popen(
            ["chimeractl", "exec", unique_name, *remote, "--", "sleep", "60"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_installed_env(),
        )
        try:
            remote_deadline = time.monotonic() + 15
            remote_units: list[str] = []
            while time.monotonic() < remote_deadline:
                remote_units = _active_guest_exec_units(unique_name)
                if remote_units:
                    break
                if sleeper.poll() is not None:
                    pytest.fail("remote exec exited before the guest unit appeared")
                time.sleep(0.2)
            assert remote_units
            session_unit = remote_units[0]
            sleeper.terminate()
            sleeper.wait(timeout=10)
            gone = time.monotonic() + 15
            last_state = "active"
            while time.monotonic() < gone:
                last_state = _observe_guest_unit(unique_name, session_unit)
                if last_state in {"absent", "inactive"}:
                    break
                time.sleep(0.2)
            else:
                pytest.fail(f"remote disconnect left {session_unit} in state {last_state}")
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=5)
        if (
            os.environ.get("CHIMERA_PRIVILEGED_APT") == "1"
            or os.environ.get("CHIMERA_TEST_APT") == "1"
        ):
            apt = _run(
                ["chimeractl", "exec", unique_name, *remote, "--", "apt-get", "update"],
                timeout=300,
            )
            assert apt.returncode == 0, apt.stderr
    finally:
        config_path.write_text(original, encoding="utf-8")
        _run(["systemctl", "restart", "chimera-server"], require=False)
        for path in (server_cert, server_key, client_ca):
            path.unlink(missing_ok=True)
        tls_dir.rmdir()
