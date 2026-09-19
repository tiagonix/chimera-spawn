# Release qualification

This document describes current release qualification. It is not a historical
validation record.

## Supported environments

- Ubuntu 24.04
- Ubuntu 26.04
- Debian 13

## Source quality

Run Black, strict mypy, and Flake8 with the Debian-family tools described in
`CONTRIBUTING.md`. Run the reduced current core pytest suite with the
distribution Python interpreter and user-site packages disabled.

## GitHub CI

GitHub CI has three current jobs:

- `quality`: Black, strict mypy, and Flake8 source-quality checks.
- `runtime`: the reduced non-privileged pytest suite on Ubuntu 24.04, Ubuntu
  26.04, and Debian 13.
- `package-smoke`: lightweight Debian metadata and changelog parsing.

## Privileged systemd-nspawn qualification

Use only a designated disposable testbed with an installed package environment.
Set all of the following before invoking the privileged gate:

- `CHIMERA_PRIVILEGED_TESTBED=1`
- `CHIMERA_TESTBED_CONFIRMED=1`
- `CHIMERA_TEST_DEB=/path/to/chimera-spawn.deb`

Root access or systemd availability alone is not authorization to run these
tests.

## Remote mTLS qualification

Remote mTLS qualification requires the privileged-test prerequisites and:

- `CHIMERA_TEST_REMOTE=1`

Use configured server and client TLS material on the designated disposable
testbed.

## Manual reboot qualification

This is a manual release qualification procedure, not pytest automation.

1. On the designated disposable testbed, create and retain uniquely named
   managed containers with desired states `running` and `stopped`.
2. Record `systemctl is-active chimera-server`, `chimeractl status --format json`,
   and `chimeractl info` for both containers.
3. Reboot the testbed.
4. Confirm `chimera-server` becomes active after boot.
5. Confirm the desired-running container returns to observed `running` without
   recreation, and the desired-stopped container remains stopped.
6. Inspect `journalctl -u chimera-server -b -e` and relevant container/service
   journal evidence.
7. Delete the retained test containers only after qualification is complete.
