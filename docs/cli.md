# CLI guide

Chimera Spawn provides `chimeractl` as the administrative client. It talks to
`chimera-server` over a local Unix socket or over mutually authenticated TLS.
`--socket` / `-s` and `--host` / `-H` are mutually exclusive. If neither is
supplied, the installed Unix socket is used.

## Local installed

```sh
# Use a configured repository if one exists, otherwise install local .deb files.
sudo apt install chimera-spawn
sudo usermod -aG chimera-admin "$USER"
# Start a new login session after changing group membership.
sudo systemctl enable --now chimera-server
chimeractl doctor
chimeractl image list
chimeractl profile list
chimeractl status
```

A client-only workstation installs `chimera-spawn-client` and does not need
`systemd-container`. A container host installs `chimera-spawn-server`. The
metapackage `chimera-spawn` installs both.

## Local explicit socket

Use command-local `--socket` / `-s` for a non-default local socket:

```sh
chimeractl status --socket /path/server.sock
chimeractl status -s /path/server.sock
```

There is no top-level `chimeractl --socket`; each command names an explicit
local socket when one is needed.

## Remote TLS

```sh
chimeractl status \
    --host server.example.net:8080 \
    --tls-ca ~/.config/chimera/ca.crt \
    --tls-cert ~/.config/chimera/admin.crt \
    --tls-key ~/.config/chimera/admin.key
```

Host syntax is `HOST`, `HOST:PORT`, or `[IPv6]:PORT`. The default port is
8080. Schemes, paths, and query strings are rejected. The client always uses
`https://` and `wss://`.

Equivalent environment defaults:

- `CHIMERA_HOST`
- `CHIMERA_TLS_CA`
- `CHIMERA_TLS_CERT`
- `CHIMERA_TLS_KEY`

`--host` without usable TLS material fails before connecting and does not fall
back to the Unix socket. A trusted client certificate is root-equivalent remote
administration; protect the files accordingly.

Remote streams use the same identity:

```sh
chimeractl exec demo \
    --host server.example.net:8080 \
    --tls-ca ~/.config/chimera/ca.crt \
    --tls-cert ~/.config/chimera/admin.crt \
    --tls-key ~/.config/chimera/admin.key \
    -- uname -a

chimeractl shell demo \
    --host server.example.net:8080 \
    --tls-ca ~/.config/chimera/ca.crt \
    --tls-cert ~/.config/chimera/admin.crt \
    --tls-key ~/.config/chimera/admin.key
```

## Lifecycle

`create IMAGE NAME` materializes a stopped container and `launch IMAGE
NAME` materializes and starts one. Both default to the `standard` catalog
profile when `--profile` is omitted. Creation-time custom-files and cloud-init
run on a pending materialization before the first start and are not reapplied
on later reconcile. Host-side profile configuration can update while the
container is stopped; `restart` of a running container with no pending host
changes performs a real unit restart. Pending host configuration is applied
after an operator-requested stop. A normal lifecycle is:

```sh
chimeractl launch IMAGE demo
chimeractl info demo
chimeractl exec demo -- uname -a
chimeractl stop demo
chimeractl restart demo
chimeractl delete demo
```

`start`, `stop`, and `restart` apply durable desired state. Retrying the same
create or launch request resumes the identical intent after a lost response or
partial host failure. `delete` removes only a Chimera-managed record; a missing
record is reported as already absent.

### Systemd-native runtime controls

`create`, `launch`, and deprecated `spawn` accept the same workload options.
They are stored in the container record and return after stop/start, server
restart, and state reload. Chimera renders mounts and ports into the native
`.nspawn` file, and renders resource limits into the container's systemd
service override:

- `--bind SOURCE:DEST[:OPTIONS]` for a writable host bind
- `--bind-ro SOURCE:DEST[:OPTIONS]` for a read-only host bind
- `--tmpfs DEST[:OPTIONS]` for a temporary filesystem
- `--publish SPEC` for a native systemd-nspawn `Port=` forward
- `--memory-high SIZE`, `--memory-max SIZE`, and `--memory-swap-max SIZE`
- `--tasks-max N`, `--cpu-quota PERCENT`, `--cpu-weight N`, and `--io-weight N`

All mount and publish options are repeatable. Bind source and destination paths
must be absolute. Escape a literal colon in a path as `\:`. Bind sources are
paths on the server host and must exist there; remote clients do not validate
their own filesystem. The optional bind option string is passed to
systemd-nspawn, so operators may explicitly select `noidmap`, `idmap`,
`rootidmap`, `owneridmap`, `rbind`, or `norbind`.

Use a read-only bind for a source tree and reserve a writable bind for a
deliberate output location. A `tmpfs` is scratch space inside the container; it
does not persist in the guest rootfs. Chimera deletes only its container and
configuration, never a bind source on the server host.

Examples:

```sh
chimeractl create IMAGE build \
    --bind-ro /srv/source:/workspace/source:rootidmap \
    --bind /srv/output:/workspace/output:rootidmap \
    --tmpfs /workspace/tmp:size=2G,mode=1777
```

Publish forms normalize as follows:

- `18080` becomes `tcp:18080:18080`
- `18080:8080` becomes `tcp:18080:8080`
- `tcp:18080:8080` and `udp:15353:5353` retain their protocol

Ports must be in `1..65535`. systemd-nspawn remains responsible for whether
the selected profile/network supports `Port=`. Resource values render as
systemd service properties. `--cpu-quota 600` means `CPUQuota=600%`;
CPU and IO weights must be `1..10000`, and tasks/quota must be positive.
Memory values such as `4096M`, `8G`, `50%`, `infinity`, and `0` are passed to
systemd's native parser after input-safety checks.

## Profiles

`chimeractl profile list` shows catalog names and their YAML `description`
metadata. Descriptions are optional; operators may overlay additional profiles
under `/etc/chimera-spawn/profiles/`.

| Profile | Meaning |
| --- | --- |
| `standard` | Default for ordinary system containers: UID/GID namespace isolation and shared host networking. |
| `compat` | Use when host UID/GID mapping is required for compatibility; networking remains shared with the host. |
| `privileged` | Use only for trusted workloads that need host UID/GID mapping and full Linux capabilities; networking remains shared with the host. |
| `offline` | Use for workloads that need a separate network namespace with loopback only and no external network. |
| `private` | Use for a separate network namespace with systemd-nspawn `Zone=chimera`; host systemd-networkd supplies DHCP, routing, and masquerading when its stock container-zone support is available. Do not assume a fixed subnet or a single /24. |

## Status, troubleshooting, and streams

`status` reports the server. During the compatibility window, `status NAME`
prints a deprecation warning on stderr and behaves as `info NAME`. `server
status` is the explicit server-status form.

Local `doctor` inspects the client's Unix socket and, when installed, the local
`chimera-server.service`. Remote `doctor --host ...` does not inspect the
client's local systemd unit, Unix socket, or `/var/lib/machines`. It reports
target host/port, TLS file readability, handshake outcome, and the remote
server's catalog and provider checks. JSON remains structured. Doctor never
recommends running doctor as its own remedy.

`exec NAME -- COMMAND` and `shell NAME` require a managed, present, running
container. Their preflight preserves diagnostics for permission denied, absent,
stopped, timeout, and host failures before the WebSocket terminal opens.

`--timeout` applies to HTTP setup and the WebSocket handshake. It does not
stop a legitimate long-running guest command after the session has started.

Terminal bytes are streamed while the remote process is still running. Typed
and piped stdin are forwarded. Closing local stdin sends an EOF control
message; that is not process completion, and later output is still delivered.
On a TTY session, that control is a terminal EOF byte. On a pipe or file
session, the server closes the guest stdin so read-to-end programs finish.
Resize events update the session PTY. Control messages (start, resize, stdin
EOF, complete, error) are not shown as terminal output. A stalled client may
fail the session; it does not report success after dropping undelivered output.

`exec` uses `systemd-run --machine --quiet --wait --pty|--pipe --collect
--service-type=exec --expand-environment=no --unit=chimera-exec-<id>.service`
so argv is preserved literally and the guest command's exit status is
available. Interactive terminals use `--pty`. Redirected stdin uses `--pipe`
so close is a real EOF. A TTY `exec` or `shell` session also forwards the
client's `TERM` value as stream-start metadata and applies it with a single
`--setenv=TERM=...` argument. Redirected sessions do not invent a terminal
type from the caller's environment, and Chimera does not forward arbitrary
process environment. A known nonzero guest status is returned as the CLI
status. If completion evidence is missing, or guest-unit cleanup cannot be
confirmed, the CLI exits 1 with a stream/protocol error rather than reporting
success. Disconnect, cancellation, or server shutdown stops that exact guest
unit and then reaps the local `systemd-run` waiter. Unrelated units are not
stopped.

`shell` uses `machinectl shell`, which does not provide a guest status; a
completed shell session exits 0 and a transport failure exits 1. On a TTY it
receives the same validated `TERM` metadata as interactive `exec`.

### Journal logs

`chimeractl logs NAME` streams the guest journal from the server host:

```sh
chimeractl logs NAME
chimeractl logs NAME -u systemd-networkd.service
chimeractl logs NAME --follow
chimeractl logs NAME --supervisor
```

Options:

- `-u UNIT` / `--unit UNIT` selects a guest unit
- `-n N` / `--lines N` selects initial records (default 200)
- `-f` / `--follow` remains attached
- `--supervisor` reads `systemd-nspawn@NAME.service` on the host

Guest mode runs the equivalent of `journalctl -M NAME -b --no-pager -n N` and
requires a running managed container. Supervisor mode may read records for a
stopped managed container. `--unit` and `--supervisor` cannot be combined.
No log database is maintained: bytes come directly from journald. Disconnect,
Ctrl-C, and server shutdown terminate and reap the server's `journalctl` child.
The command uses the same WSS/mTLS transport as remote exec and shell.

Remote doctor and remote command errors report the remote target, credential
readability, TLS handshake/authentication, API reachability, and remote
application failures when those can be distinguished. They do not inspect the
workstation's `chimera-server` service, Unix socket, or `/var/lib/machines`,
and they do not fall back to a local server.

## Output and confirmation

Most commands accept `--format table` (the default) or `--format json`. JSON is
strictly machine-readable on stdout; warnings and human diagnostics use stderr.
`delete` and the deprecated `remove` alias ask for confirmation only on an
interactive table terminal. JSON or noninteractive deletion must include
`--force`.

Use `--timeout SECONDS` for operations that need a different client timeout.
Common remedies are `chimeractl doctor`, `chimeractl info NAME`, and the
`chimera-server` system journal.

## Raw images

Raw images are supported for clone/start lifecycle, but this release does not
mount or modify them. A raw image cannot use catalog `custom_files` and cannot
be launched with `--cloud-init`; Chimera rejects those requests before durable
state is created. Choose a root-filesystem tar image for those provisioning
features.

Generated `.nspawn` files receive a host-appropriate `ResolvConf=` unless the
profile already sets one. Image `nspawn_parameters` are the usual mechanism for
image-specific systemd runtime masks such as `systemd.mask=`. `custom_files`
remain for genuine guest-rootfs transformations.
`chimeractl image list` wraps long source URLs instead of truncating them.
JSON includes the complete source string unchanged.
