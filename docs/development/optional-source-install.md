# Optional source installation

This workflow is **opt-in**. It is not the supported Debian/Ubuntu installation
or development path, and it is not Debian-native release evidence.

Primary development uses APT packages and `/usr/bin/python3`. See
`docs/development/python-dependency-policy.md` and `CONTRIBUTING.md`.

If you are on a non-Debian system or explicitly want a portable virtual
environment, you may use modern Python packaging tools against `pyproject.toml`.
That environment must not raise mandatory runtime API requirements beyond the
libraries available on Ubuntu 24.04, Ubuntu 26.04, and Debian 13.

Example (opt-in only; never used by CI, Debian packaging, or primary docs):

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Do not use that environment's results to qualify a Chimera release.
