# Dependency map

Evidence-based mapping of Chimera Spawn Python dependencies. Runtime API floors
are constrained by Ubuntu 24.04, Ubuntu 26.04, and Debian 13 archive packages.

| Import / distribution | Consumers | Debian/Ubuntu package | Floor | Notes |
| --- | --- | --- | --- | --- |
| pydantic | models, CLI, server | python3-pydantic | >=1.10.14,<3 | 24.04 ships Pydantic 1; 26.04 ships 2. Compatibility layer in `pydantic_compat.py`. |
| typer | CLI | python3-typer | >=0.9.0 | |
| rich | CLI rendering | python3-rich | >=13.7.1 | |
| dbus_next | `utils/systemd.py` | python3-dbus-next | >=0.2.3 | |
| ruamel.yaml | config, cloud-init YAML | python3-ruamel.yaml | >=0.17.21 | |
| jinja2 | template rendering | python3-jinja2 | >=3.1.2 | |
| watchfiles | server catalog watch | python3-watchfiles | >=0.21.0 | |
| httpx | CLI Unix and HTTPS client | python3-httpx | >=0.26.0 | Client package. |
| aiohttp | server HTTP/WebSocket | python3-aiohttp | >=3.9.1 | Server package. |
| websockets | CLI Unix and WSS streams | python3-websockets | >=10.4 | Client package. Async `websockets.connect` / `unix_connect` is intentional so Ubuntu 24.04, Ubuntu 26.04, and Debian 13 keep one CLI stream implementation. |
| pytest | tests | python3-pytest | >=7.4.4 | Quality/test class, not runtime. |
| pytest-asyncio | tests | python3-pytest-asyncio | >=0.20.3 | Quality/test class. |
| black | formatting | `black` | APT when available | Quality tool. |
| mypy | typing | python3-mypy | APT when available | Quality tool. |
| ruff | lint | *not in Ubuntu 24.04/26.04 or Debian 13 archives* | n/a | Missing distro package; do not fetch from PyPI for primary CI. Use `flake8` when present as a compatible equivalent. |
| python-systemd | none | python3-systemd | unused | Removed as unused-dependency cleanup. No src/tests import. Not a migration away from journald. |
| aiofiles | none | python3-aiofiles | unused | Removed as unused-dependency cleanup. Filesystem work uses `asyncio.to_thread`. |

`dbus_next` and `asyncio.to_thread` remain the live D-Bus and filesystem
adapters. There was no migration off python-systemd or aiofiles; they were
unused.

No `py.typed` marker is shipped. Typing metadata is not advertised beyond the
source tree's mypy configuration.
