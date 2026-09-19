# Chimera Spawn

A modern systemd-nspawn container orchestration system providing LXD-like usability with deep systemd integration.

## Overview

Chimera Spawn embodies the biological concept of a chimera - one organism with multiple DNA sets - translated to infrastructure as one server running multiple operating systems in isolated containers. It provides sophisticated container management while leveraging native systemd features.

### Why systemd-nspawn?

The default `standard` profile keeps containers on **the host's network namespace** — no virtual ethernet, no separate container IP, no bridge dependencies:

```ini
[Network]
Private=no
VirtualEthernet=no
```

That shared stack is still the usual reason to choose Chimera Spawn over tools that always invent a guest interface. Catalog profiles `offline` and `private` opt into a separate network namespace when that is what the workload needs.

### Why not LXD?

LXD is excellent for most use cases, but it intentionally does **not** support "host network mode" (sharing the host network namespace). While LXD supports various networking modes (bridge, macvlan, ipvlan), all of these run containers in separate network namespaces with their own interfaces. Chimera Spawn exists specifically for cases where you need **true host networking**.

If your environment supports bridges/OVN/macvlan cleanly, LXD is usually the better default.

### Why not Docker/Kubernetes?

Docker and Kubernetes excel at application containers, but Chimera Spawn targets **system containers** - full OS environments with systemd as PID 1. While Docker *can* run systemd, it requires extra privileges and cgroup configuration that goes against the typical "one process per container" model. Chimera Spawn provides a cleaner solution for running traditional systemd services in containers.

## Features

- **State-Driven Design**: Durable CLI-owned lifecycle intent with automatic reconciliation
- **LXD-Inspired CLI**: Intuitive commands for container management
- **Deep systemd Integration**: Native integration via DBus APIs
- **Cloud-Init Support**: Automatic container initialization
- **Profile-Based Configuration**: Reusable catalog profiles (`standard` by default)
- **Async Architecture**: High-performance async/await implementation
- **Client/Server Control Plane**: Local Unix HTTP/WebSocket and optional
  mutually authenticated HTTPS/WSS
- **Native Package Workflow**: Build and install Debian-family packages for
  normal operation
- **Native Workload Configuration**: Durable bind/tmpfs mounts, port forwarding,
  and systemd resource controls rendered into `.nspawn` and service overrides
- **Journal Access**: Guest and nspawn-supervisor logs streamed directly from
  journald, locally or through the remote mTLS client

## Runtime architecture

```text
                         chimeractl
                             |
                  Unix socket / HTTPS+mTLS
                             |
                             v
                     +----------------+
                     | Chimera server |
                     +--------+--------+
                             |
                 durable workload intent
                             |
             +---------------+---------------+
             |                               |
             v                               v
       .nspawn config                systemd override
             |                               |
             v                               v
      systemd-nspawn                   systemd/cgroups
             |                               |
             +---------------+---------------+
                             |
                             v
                         container
                             |
                             v
                          journald
                             |
                             v
                     chimeractl logs
```

```text
image + profile + per-container workload intent
    -> native systemd configuration
    -> disposable system container

optional host bind mounts
    -> durable artifacts that survive container deletion
```

See `docs/architecture.md` for the full authority and configuration model.

## Requirements

- Ubuntu 26.04 (primary development host), Ubuntu 24.04, or Debian 13
- Python 3.12+
- systemd-container on the server/container host (not required on a remote
  workstation that only installs `chimera-spawn-client`)
- System Python packages (installed via apt)

## Installation

Chimera Spawn is a client/server system. Binary package names:

```bash
# Administrative workstation (CLI only; no systemd-nspawn)
# Use this form when those names are available from a configured APT repository.
sudo apt install chimera-spawn-client

# Managed container host (privileged server)
sudo apt install chimera-spawn-server

# All-in-one host (client and server together)
sudo apt install chimera-spawn
```

This project does not currently publish a public APT repository. When you
build locally, install the matching `.deb` artifacts instead of assuming
`apt install chimera-spawn` will resolve from the archive.

`python3-chimera-spawn` is the shared Python implementation. It is pulled in
automatically and is not a day-to-day operator package.

Production hosts should install the native Debian packages and, on a server
host, the systemd service. Do not use pip, a virtual environment, or a
production `PYTHONPATH` for that path.

Installing the server does not enable remote network access. The remote TLS
listener stays disabled until an administrator sets `server.host` and provides
certificate files. Package installation does not generate a CA, server key, or
client identity.

```bash
# Clone the repository first; dpkg-buildpackage runs from the source tree
git clone https://github.com/tiagonix/chimera-spawn.git
cd chimera-spawn

# Install packaging and runtime build dependencies
sudo apt update
sudo apt install -y \
    build-essential \
    debhelper \
    dh-python \
    pybuild-plugin-pyproject \
    python3-all \
    python3-setuptools \
    python3-wheel \
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
    openssl \
    systemd-container

# Build and install the native packages from the local artifacts
dpkg-buildpackage -us -uc -b
sudo apt install \
    ../python3-chimera-spawn_*_all.deb \
    ../chimera-spawn-client_*_all.deb \
    ../chimera-spawn-server_*_all.deb \
    ../chimera-spawn_*_all.deb
sudo systemctl enable --now chimera-server
```

The package installs a root-owned local server. Members of `chimera-admin` can
operate containers; that group is root-equivalent for Chimera administration.
Add an operator, then start a new login session:

```bash
sudo usermod -aG chimera-admin "$USER"
```

Installation does not pull container images and does not enroll users into
`chimera-admin` automatically.

```bash
# For source-suite development checks
sudo apt install -y \
    python3-pytest \
    python3-pytest-asyncio \
    python3-mypy \
    python3-flake8 \
    black
```

The optional non-Debian source-install workflow is documented separately in
`docs/development/optional-source-install.md`.

## Quick Start

### 1. Start the Server (as root)

Installed package:

```bash
# Check server status
sudo systemctl status chimera-server
```

The server will:
- Load configuration from the configured catalog directories
- Start reconciliation loop
- Listen on its Unix socket for commands
- Listen on TLS only when `server.host` and complete TLS files are configured

### 2. Use the CLI

Installed all-in-one host (default Unix socket, `chimeractl` from the package):

```bash
# Check server status
chimeractl doctor
chimeractl status

# List catalog image definitions (not proof that a rootfs is downloaded)
chimeractl image list

# Create and start a container (uses the standard profile)
chimeractl launch ubuntu-24.04-cloud-tar demo

# Inspect desired vs observed state
chimeractl info demo

# Open shell in container
chimeractl shell demo

# Execute command in container
chimeractl exec demo -- apt update

# Stop container
chimeractl stop demo
```

After joining `chimera-admin` and starting a new login session, those commands
do not need `sudo`. `apt` package installation and host `journalctl` of
systemd units remain ordinary administrative operations.

Note: Some commands require `sudo` (or run as `root`) for container operations.
Installed operators need `chimera-admin` membership and a new login session to
run lifecycle commands without `sudo`.

`spawn` remains a deprecated alias for `launch IMAGE NAME`. `remove` remains a
deprecated alias for `delete`. Legacy `spawn --all` is gone: import node YAML
first, then operate per container.

### Remote client workstation

On a machine that only needs `chimeractl`, install `chimera-spawn-client`. The
client talks to a local Unix socket by default, or to a remote server over
HTTPS/WSS when `--host` / `-H` is set. `--socket` / `-s` and `--host` / `-H`
are mutually exclusive. Remote URLs are always `https://` and `wss://`.

A client certificate trusted by the server's administrative CA is
root-equivalent remote administration. Protect those files like root
credentials. There is no plaintext remote API and no `--insecure` mode.

```bash
chimeractl status \
    --host server.example.net:8080 \
    --tls-ca ~/.config/chimera/ca.crt \
    --tls-cert ~/.config/chimera/admin.crt \
    --tls-key ~/.config/chimera/admin.key
```

Environment defaults for repeated administration: `CHIMERA_HOST`,
`CHIMERA_TLS_CA`, `CHIMERA_TLS_CERT`, `CHIMERA_TLS_KEY`. Default remote port
is 8080. On the server host, set `server.host` plus `server.tls.certificate`,
`server.tls.private_key`, and `server.tls.client_ca`, then restart
`chimera-server`. Changing those values requires a server restart.

Remote `chimeractl doctor --host ...` reports client TLS files, handshake
outcome, and the remote server's catalog/provider checks. It does not judge the
remote host by looking at the client's local `chimera-server.service` or
`/var/lib/machines`.

## AI and build workspaces

This pattern gives an AI coding or build agent controlled source access,
durable output, disposable rootfs state, private networking, and native
resource limits:

```bash
mkdir -p "$PWD/.chimera-output"
chimeractl launch ubuntu-26.04-cloud-tar agent-build \
    --profile private \
    --bind-ro "$PWD:/workspace/source:rootidmap" \
    --bind "$PWD/.chimera-output:/workspace/output:rootidmap" \
    --tmpfs "/workspace/tmp:size=2G,mode=1777" \
    --publish tcp:18080:8080 \
    --memory-high 6G \
    --memory-max 8G \
    --tasks-max 4096

chimeractl logs agent-build --follow
chimeractl delete agent-build
```

The host source tree is a read-only bind at `/workspace/source`; the
operator-provided output directory is a writable persistent bind at
`/workspace/output`; and `/workspace/tmp` is a temporary `tmpfs`. The
`private` profile supplies an isolated network namespace, while the memory and
task flags become native systemd cgroup limits. `chimeractl logs` reads the
guest journal through journald, not a separate Chimera log store.

The container rootfs is disposable. Deleting the container does not delete the
operator-provided output directory or its artifacts. `rootidmap` is useful on
filesystems that support ID-mapped mounts because guest root maps to the host
owner of the bind source. It is an explicit operator choice; omit it when
unsupported.

Port declarations are durable. For example,
`--publish 18080:8080` is normalized to `tcp:18080:8080` and rendered as
`Port=tcp:18080:8080`. View the guest journal with `chimeractl logs NAME`, a
specific guest unit with `chimeractl logs NAME -u UNIT`, or the host-side nspawn
service with `chimeractl logs NAME --supervisor`.

## Configuration

Configuration files are stored in the `configs/` directory:

- `chimera.yaml` - Main server configuration
- `images/*.yaml` - Image definitions
- `profiles/*.yaml` - Container profiles
- `cloud-init/*.yaml` - Cloud-init templates
- `nodes/*.yaml` - Legacy migration input only, not runtime container authority

### Configuration Structure

```
configs/
├── chimera.yaml         # Main configuration
├── images/              # Image definitions
│   ├── ubuntu.yaml
│   ├── debian.yaml
│   └── rocky.yaml
├── profiles/            # Container profiles
│   ├── standard.yaml
│   ├── compat.yaml
│   ├── privileged.yaml
│   ├── offline.yaml
│   └── private.yaml
├── cloud-init/          # Cloud-init templates
│   ├── ubuntu.yaml
│   └── base.yaml
└── nodes/               # Legacy migration input only
    ├── dev-node1.yaml
    └── test-node1.yaml
```

Installed paths:

- site configuration: `/etc/chimera-spawn/chimera.yaml`
- packaged catalogs: `/usr/share/chimera-spawn/catalog`
- site catalog overlays: `/etc/chimera-spawn/{images,profiles,cloud-init}`
- durable container state: `/var/lib/chimera-spawn/state.json`
- server lock: `/var/lib/chimera-spawn/server.lock`
- runtime socket: `/run/chimera-spawn/server.sock`

Built-in profiles (`chimeractl profile list`):

| Profile | Use |
| --- | --- |
| `standard` | Default. UID/GID namespace isolation, shared host networking. |
| `compat` | Host UID/GID mapping, shared host networking. |
| `privileged` | Full Linux capabilities for trusted workloads, host UID/GID mapping, shared host networking. |
| `offline` | Separate network namespace, loopback only, no external network. |
| `private` | Separate network namespace via systemd-nspawn `Zone=chimera`. DHCP, routing, and masquerading come from host systemd-networkd when its stock container-zone support is present. Do not assume a fixed subnet. |

Site overlays under `/etc/chimera-spawn/profiles/` can add or replace catalog profiles without changing the packaged YAML.

Rendered `.nspawn` files include a host-aware `ResolvConf=` default: the
resolved uplink file when `/run/systemd/resolve/resolv.conf` is a usable
regular file, otherwise the host's ordinary resolver file. A profile that
already sets `ResolvConf=` in `[Exec]` keeps that value. Image
`nspawn_parameters` are the usual mechanism for image-specific systemd runtime
masks such as `systemd.mask=`. `custom_files` remain for genuine guest-rootfs
transformations. `chimeractl image list` wraps long source URLs instead of
truncating them.

### Example Container Configuration

Node YAML is migration input. Preview and import it; do not treat editing
`nodes/*.yaml` as the live creation workflow:

```yaml
# configs/nodes/dev-node1.yaml
containers:
  ubuntu2404-dev:
    ensure: present
    state: running
    image: ubuntu-24.04-cloud-tar
    profile: standard
    cloud_init:
      template: ubuntu_base
      meta_data:
        purpose: development
```

```bash
chimeractl config import-nodes configs/nodes/dev-node1.yaml --dry-run
chimeractl config import-nodes configs/nodes/dev-node1.yaml
```

### Common Tasks

#### Add a New Container

1. Launch from a catalog image (this persists CLI-owned intent):
```bash
chimeractl launch ubuntu-24.04-cloud-tar my-new-container
```

2. Inspect the result:
```bash
chimeractl info my-new-container
```

To migrate an existing node-YAML declaration instead, import it as shown above.

#### Pull a New Image

1. Check catalog image definitions:
```bash
chimeractl image list
```

2. Pull an image:
```bash
chimeractl image pull ubuntu-24.04-cloud-tar
```

#### Use with Proxy

Templated `.nspawn` profile content and cloud-init `user-data` can consume
Chimera proxy settings. Image downloads go through `machinectl pull-*` and are
not driven by `config.proxy`.

Edit `configs/chimera.yaml`:
```yaml
proxy:
  http_proxy: http://proxy.company.com:3128
  https_proxy: http://proxy.company.com:3128
  no_proxy: localhost,127.0.0.1
```

## CLI Commands

```bash
# Container Management
chimeractl create IMAGE NAME   # Materialize stopped
chimeractl launch IMAGE NAME   # Create and start container
chimeractl start NAME          # Start container
chimeractl stop NAME           # Stop container
chimeractl restart NAME        # Restart container
chimeractl info NAME           # Desired vs observed state
chimeractl delete NAME         # Remove container
chimeractl exec NAME -- <cmd>  # Execute command
chimeractl shell NAME          # Interactive shell in container
chimeractl logs NAME           # Stream guest journal records

# Image Management
chimeractl image pull NAME     # Pull image
chimeractl image list          # List catalog image definitions

# System Operations
chimeractl doctor              # Local or remote diagnostics
chimeractl status              # System status
chimeractl status -H HOST      # Remote status (HTTPS, mutual TLS)
chimeractl config validate     # Validate configuration
chimeractl config import-nodes # Import legacy node YAML
```

`status NAME` remains a compatibility alias for `info NAME`.

Most commands accept `--format table` (default) or `--format json`. JSON is
machine-readable on stdout; human diagnostics and warnings go to stderr.
Failures use structured codes (`permission_denied`, `not_found`, and similar)
rather than unstructured tracebacks. `create` stores a stopped container;
`launch` stores and starts one. `start`/`stop`/`restart` persist desired state
across server restarts. `delete` does not recreate the container.

## Architecture

Chimera Spawn uses a distributed client/server architecture:

- **Server**: Runs as root, manages state and system operations
- **CLI**: Can run as regular user, communicates with the server via Unix socket
- **Providers**: Modular handlers for images, containers, profiles, cloud-init
- **State Engine**: Detects drift and reconciles to desired state

This separation allows unprivileged users to query status while only root performs system changes.

Local and remote clients use the same server API. The Unix socket authenticates
with Linux `SO_PEERCRED`. Remote access uses HTTPS and WSS with mutual TLS.

```text
                      +-------------------+
    Local CLI ------> |                   |
    Unix HTTP/WS      |  Chimera Server   |
                      |                   |
    Remote CLI -----> |                   |
    HTTPS/WSS mTLS    +---------+---------+
                                |
                         CommandService
                                |
                           StateEngine
                                |
                    +-----------+-----------+
                    |                       |
              ContainerStore            Providers
```

Images, profiles, and cloud-init remain static catalogs. `image pull` is an
explicit host action. ContainerStore holds managed containers only. Ordinary
lifecycle operations never adopt unmanaged host resources;
`config import-nodes` is the explicit legacy-adoption path.

Deeper design, including creation-time provisioning and host configuration, is
in `docs/architecture.md`. Security boundaries are in `docs/security.md`.
CLI endpoint modes are in `docs/cli.md`. Migration details are in
`docs/migration.md`.

## Troubleshooting

Run `chimeractl doctor` first. It works without a functioning server.

### Server Won't Start
- Inspect `journalctl -u chimera-server -e` for installed mode
- Confirm any explicit `--config-dir`, `--state-dir`, and `--socket` paths
- A second server using the same state directory is rejected
- Do not delete an arbitrary file at the socket path

### Container Won't Start
- Confirm the catalog defines the image: `chimeractl image list`
- Pull the image if it has not been downloaded: `chimeractl image pull NAME`
- Check systemd logs: `sudo journalctl -u systemd-nspawn@container-name`
- Verify profile exists: `chimeractl profile list`

### Connection Refused
- Ensure the server is running
- Check socket path matches between server and CLI (`--socket` / `-s` is command-local)
- Verify permissions on socket file
- Installed operators need `chimera-admin` membership and a new login session

## Development

Primary development uses APT packages. See `CONTRIBUTING.md` and
`docs/development/python-dependency-policy.md`.

```bash
# Run tests
PYTHONNOUSERSITE=1 PYTHONPATH=src /usr/bin/python3 -s -m pytest

# Type checking
PYTHONNOUSERSITE=1 /usr/bin/python3 -s -m mypy src/

# Format code
black --check src/ tests/

# Lint
/usr/bin/python3 -m flake8 src/ tests/
```

## Production Deployment

For production use, install the native packages. The server package ships this
systemd unit:

```ini
# /usr/lib/systemd/system/chimera-server.service
[Unit]
Description=Chimera Spawn Server
Documentation=https://github.com/tiagonix/chimera-spawn
After=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/chimera-spawn/chimera.yaml

[Service]
Type=simple
User=root
Group=chimera-admin
UMask=0007
StateDirectory=chimera-spawn
StateDirectoryMode=0700
RuntimeDirectory=chimera-spawn
RuntimeDirectoryMode=0750
ExecStart=/usr/bin/chimera-server
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

Enable it with `sudo systemctl enable --now chimera-server`. Do not deploy a
handwritten `/opt` `PYTHONPATH` service.

## License

AGPL-3.0-only. See `LICENSE.txt`.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests and linting
5. Submit a pull request

See `CONTRIBUTING.md` for the APT-native development workflow.

## Support

- Documentation: `docs/architecture.md`, `docs/security.md`, `docs/cli.md`, `docs/migration.md`
- Dependency policy: `docs/development/python-dependency-policy.md`
- Issues: GitHub Issues
- Discussions: GitHub Discussions
- Community: Discussions forum
