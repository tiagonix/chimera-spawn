# Chimera Spawn

A modern systemd-nspawn container orchestration system providing LXD-like usability with deep systemd integration.

## Overview

Chimera Spawn embodies the biological concept of a chimera - one organism with multiple DNA sets - translated to infrastructure as one server running multiple operating systems in isolated containers. It provides sophisticated container management while leveraging native systemd features.

### Why systemd-nspawn?

Chimera Spawn targets environments where containers must run with **the host's network namespace** - no virtual ethernet, no separate container IP, no bridge dependencies. This is achieved with:

```ini
[Network]
Private=no
VirtualEthernet=no
```

This enables `systemd-containers` to share the exact same network stack as the host, perfect for environments where network bridges aren't feasible or desired.

### Why not LXD?

LXD is excellent for most use cases, but it intentionally does **not** support "host network mode" (sharing the host network namespace). While LXD supports various networking modes (bridge, macvlan, ipvlan), all of these run containers in separate network namespaces with their own interfaces. Chimera Spawn exists specifically for cases where you need **true host networking**.

If your environment supports bridges/OVN/macvlan cleanly, LXD is usually the better default.

### Why not Docker/Kubernetes?

Docker and Kubernetes excel at application containers, but Chimera Spawn targets **system containers** - full OS environments with systemd as PID 1. While Docker *can* run systemd, it requires extra privileges and cgroup configuration that goes against the typical "one process per container" model. Chimera Spawn provides a cleaner solution for running traditional systemd services in containers.

## Features

- **State-Driven Design**: Declarative configuration with automatic reconciliation
- **LXD-Inspired CLI**: Intuitive commands for container management
- **Deep systemd Integration**: Native integration via DBus APIs
- **Cloud-Init Support**: Automatic container initialization
- **Profile-Based Configuration**: Reusable container configurations
- **Async Architecture**: High-performance async/await implementation
- **Repository Execution**: Run directly from git clone without installation

## Requirements

- Ubuntu 24.04 LTS
- Python 3.11+
- systemd-container package
- System Python packages (installed via apt)

## Installation

```bash
# Install system dependencies
sudo apt update
sudo apt install -y \
    systemd-container \
    python3-pydantic \
    python3-typer \
    python3-rich \
    python3-dbus-next \
    python3-systemd \
    python3-ruamel.yaml \
    python3-jinja2 \
    python3-watchfiles \
    python3-psutil \
    python3-aiofiles \
    python3-httpx \
    python3-aiohttp \
    python3-websockets

# For running full Pytest suite
sudo apt install -y \
    python3-pytest-asyncio \
    python3-pytest-cov

# Clone the repository
git clone https://github.com/tiagonix/chimera-spawn.git
cd chimera-spawn
```

## Quick Start

### 1. Start the Agent (as root)

```bash
cd chimera-spawn
export PYTHONPATH=$PWD/src:$PYTHONPATH
sudo -E python3 -m chimera.agent
```

The agent will:
- Load configuration from `configs/` directory
- Start reconciliation loop
- Listen on Unix socket for commands

### 2. Use the CLI (as regular user)

In another terminal:
```bash
cd chimera-spawn
export PYTHONPATH=$PWD/src:$PYTHONPATH

# Check agent status
python3 -m chimera.cli status

# List available images
python3 -m chimera.cli list images

# Create and start a container
sudo python3 -m chimera.cli spawn ubuntu-privileged

# List running containers
python3 -m chimera.cli list containers

# Open shell in container
sudo python3 -m chimera.cli shell ubuntu-privileged

# Execute command in container
sudo python3 -m chimera.cli exec ubuntu-privileged -- apt update

# Stop container
python3 -m chimera.cli stop ubuntu-privileged
```

Note: Some commands require `sudo` (or run as `root`) for container operations.

## Configuration

Configuration files are stored in the `configs/` directory:

- `config.yaml` - Main agent configuration
- `images/*.yaml` - Image definitions
- `profiles/*.yaml` - Container profiles
- `cloud-init/*.yaml` - Cloud-init templates
- `nodes/*.yaml` - Container configurations per node

### Configuration Structure

```
configs/
├── config.yaml          # Main configuration
├── images/              # Image definitions
│   ├── ubuntu.yaml
│   ├── debian.yaml
│   └── rocky.yaml
├── profiles/            # Container profiles
│   ├── isolated.yaml
│   ├── privileged.yaml
│   └── non_isolated.yaml
├── cloud-init/          # Cloud-init templates
│   ├── ubuntu.yaml
│   └── base.yaml
└── nodes/               # Container configurations
    ├── dev-node1.yaml
    └── test-node1.yaml
```

### Example Container Configuration

```yaml
# configs/nodes/dev-node1.yaml
containers:
  ubuntu2404-dev:
    ensure: present
    state: running
    image: ubuntu-24.04-cloud-tar
    profile: isolated
    cloud_init:
      template: ubuntu_base
      meta_data:
        purpose: development
```

### Common Tasks

#### Add a New Container

1. Edit `configs/nodes/dev-node1.yaml`:
```yaml
containers:
  my-new-container:
    ensure: present
    state: running
    image: ubuntu-24.04-cloud-tar
    profile: isolated
    cloud_init:
      template: ubuntu_base
```

2. Reload agent configuration:
```bash
python3 -m chimera.cli agent reload
```

3. Spawn the container:
```bash
sudo python3 -m chimera.cli spawn my-new-container
```

#### Pull a New Image

1. Check available images:
```bash
python3 -m chimera.cli list images
```

2. Pull an image:
```bash
sudo python3 -m chimera.cli image pull ubuntu-24.04-cloud-tar
```

#### Use with Proxy

Edit `configs/config.yaml`:
```yaml
proxy:
  http_proxy: http://proxy.company.com:3128
  https_proxy: http://proxy.company.com:3128
  no_proxy: localhost,127.0.0.1
```

## CLI Commands

```bash
# Container Management
chimeractl list                # List all resources
chimeractl spawn <n>           # Create and start container
chimeractl start <n>           # Start container
chimeractl stop <n>            # Stop container
chimeractl restart <n>         # Restart container
chimeractl remove <n>          # Remove container
chimeractl exec <n> -- <cmd>   # Execute command
chimeractl shell <n>           # Interactive shell in container

# Image Management
chimeractl image pull <n>      # Pull image
chimeractl image list          # List images

# System Operations
chimeractl status              # System status
chimeractl config validate     # Validate configuration
```

Note: `chimeractl` is an alias for `python3 -m chimera.cli`

## Architecture

Chimera Spawn uses a distributed agent/client architecture:

- **Agent**: Runs as root, manages state and system operations
- **CLI**: Can run as regular user, communicates with agent via Unix socket
- **Providers**: Modular handlers for images, containers, profiles, cloud-init
- **State Engine**: Detects drift and reconciles to desired state

This separation allows unprivileged users to query status while only root performs system changes.

## Troubleshooting

### Agent Won't Start
- Check permissions (needs root with PYTHONPATH)
- Check if socket already exists: `rm ./state/chimera-agent.sock`
- Check logs for configuration errors

### Container Won't Start
- Check image is pulled: `python3 -m chimera.cli list images`
- Check systemd logs: `sudo journalctl -u systemd-nspawn@container-name`
- Verify profile exists: `python3 -m chimera.cli profile list`

### Connection Refused
- Ensure agent is running
- Check socket path matches between agent and CLI
- Verify permissions on socket file

## Development

```bash
# Run tests
python3 -m pytest

# Type checking
python3 -m mypy src/

# Format code
python3 -m black src/ tests/

# Lint
python3 -m ruff src/ tests/
```

### AI-Assisted Development

This project was developed with AI assistance to accelerate drafting and exploration. All code has been reviewed, tested, and validated by the author. AI tools were used as a development accelerator while maintaining full human oversight over design decisions, code quality, and security.

## Production Deployment

For production use, create a systemd service:

```ini
# /etc/systemd/system/chimera-agent.service
[Unit]
Description=Chimera Spawn Agent
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m chimera.agent
Restart=always
RestartSec=10
User=root
Environment="PYTHONPATH=/opt/chimera-spawn/src"
WorkingDirectory=/opt/chimera-spawn

[Install]
WantedBy=multi-user.target
```

## License

Apache License - see LICENSE file for details.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests and linting
5. Submit a pull request

## Support

- Documentation: See docs/ directory
- Issues: GitHub Issues
- Community: Discussions forum
