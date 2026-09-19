# Python dependency policy

This is the canonical Chimera Spawn dependency policy.

## Primary Debian/Ubuntu workflows

Production installation, normal Debian-family development, CI, and release
qualification must use:

- the distribution's Python interpreter (`/usr/bin/python3`)
- runtime libraries from the supported distribution's official archives
- APT-provided build, test, lint, formatting, and typing tools

Do not use or bootstrap pip, pipx, uv, venv, virtualenv, Poetry, PDM, Conda, or
equivalent environment workflows on these paths. There is no exception for
`pip --no-deps`, `pip install -e .`, `venv --system-site-packages`, or
`--break-system-packages`.

Do not vendor third-party runtime dependencies, silently fetch replacements,
mix another distribution's packages into a compatibility test, or repackage
PyPI libraries merely to evade official-distro availability.

A missing or incompatible distro dependency is an explicit compatibility or
tooling issue. Report it; do not fetch a replacement.

## Optional developer / non-Debian source workflow

A separate opt-in source-install workflow exists for developers or non-Debian
environments. It is documented in
`docs/development/optional-source-install.md`. It must not be invoked as
Debian-native evidence, must not raise mandatory runtime API requirements
beyond supported distro packages, and must not replace the primary
installation or CI path.

## Python build metadata

`pyproject.toml`, setuptools/backend requirements, package discovery, entry
points, and standard distribution metadata are kept. APT-provided PEP 517
tooling and locally generated intermediate wheels used by Debian packaging are
permitted. They are not PyPI dependency downloads or a private deployed
runtime.

## Safety

Never delete operator environments or user data as part of enforcing this
policy.
