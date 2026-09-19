# Contributing

## Primary Debian-family development

Use the distribution Python interpreter and APT packages. Do not create a
virtual environment or install dependencies with pip for this workflow.

```sh
sudo apt update
sudo apt install -y \
    python3 \
    python3-pytest \
    python3-pytest-asyncio \
    python3-aiohttp \
    python3-dbus-next \
    python3-httpx \
    python3-jinja2 \
    python3-pydantic \
    python3-rich \
    python3-ruamel.yaml \
    python3-typer \
    python3-watchfiles \
    python3-websockets \
    python3-mypy \
    python3-flake8 \
    black \
    openssl
```

From the repository root, with user-site packages disabled:

```sh
PYTHONNOUSERSITE=1 PYTHONPATH=src /usr/bin/python3 -s -m pytest
PYTHONNOUSERSITE=1 /usr/bin/python3 -s -m mypy src
black --check src tests
/usr/bin/python3 -m flake8 src tests
```

Required quality tools come from APT. Installation or execution failure of
Black, strict mypy, or Flake8 fails the native quality gate. Ruff is not in
the Ubuntu 24.04, Ubuntu 26.04, or Debian 13 official archives; do not install
it from PyPI for primary work. Optional Ruff metadata remains for the separate
developer workflow only.

Canonical policy: `docs/development/python-dependency-policy.md`.
The optional non-Debian source-install guide is
`docs/development/optional-source-install.md`.

Do not change version numbers, license identifiers, or authorship as incidental
cleanup. Preserve comments that explain lifecycle, ownership, and distro
compatibility. Report executed versus unexecuted validation honestly.
Dependency changes require source-use evidence.

GitHub CI runs static source quality, a reduced non-privileged runtime/source
suite, and a Debian metadata smoke check.
Privileged systemd/nspawn checks require an explicitly designated disposable
testbed and a built package path; root or systemd availability alone is not
permission. See `tests/system/` and the reboot procedure in
`docs/development/release-qualification.md`.

## Test policy

Tests protect enduring core product contracts. A regression test belongs only
when its behavior can be stated independently as a current product invariant;
do not retain tests solely because they once reproduced a bug. Do not test
obsolete compatibility, deleted features, historical internal formats,
implementation sequencing, repository layout, CI text, or documentation text.
Prefer representative boundary and behavior tests over permutations and
implementation-detail microtests. When a feature or compatibility promise is
intentionally removed, remove tests that exist solely for that contract.
