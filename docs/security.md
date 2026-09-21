# Security model

Chimera Spawn runs a privileged server because systemd-nspawn, machinectl, and
host container storage require root. The control plane is a client/server API
with two transports to the same application.

This document describes trust boundaries. It does not claim immunity against an
attacker who already has root-equivalent access on the server host or possession
of a trusted administrative client certificate.

## Local Unix transport

The installed Unix-domain socket is `/run/chimera-spawn/server.sock`. The
runtime directory is provided by systemd (`RuntimeDirectory=chimera-spawn`) and
is the trust boundary for the socket pathname. Chimera does not invent a
separate path-race framework around that directory.

The socket is `0660 root:chimera-admin`. Root and members of `chimera-admin`
may perform privileged operations; membership is root-equivalent and must be
granted deliberately by an administrator. Chimera never enrolls users
automatically. If the group is unavailable, the socket falls back to root-only
permissions.

The server reads Linux `SO_PEERCRED` for each Unix connection and authorizes the
actual peer UID and supplementary groups. Client-supplied UID/GID values and
HTTP headers are ignored. Read commands remain available to any peer that can
open the socket; privileged commands require root or `chimera-admin`. If peer
credentials cannot be established, privileged commands fail closed.

Successful privileged Unix commands are audit logged as
`transport=unix uid=... pid=...`. Denials include the same identity fields.
Private keys, certificates, command payloads, and terminal contents are not
logged.

## Remote TLS transport

Remote access is disabled by default (`server.host: null`). Enabling a remote
listener without complete TLS configuration fails startup. There is no
plaintext TCP administration, no `http://` or `ws://` remote API, and no
insecure client bypass.

When configured, the server listens with HTTPS and WSS only:

- TLS 1.2 or newer
- server certificate and private key
- client CA
- `ssl.CERT_REQUIRED` (mutual TLS)

The server private key must be a regular file, not a symlink, with restrictive
permissions. Administrators own PKI: package installation does not generate a
CA, server key, or client identity.

A client certificate trusted by the configured administrative CA is
root-equivalent remote administration. The first remote implementation does not
map certificates onto additional roles. Hostname verification uses certificate
SAN rules; IP-address targets require a matching IP SAN.

Successful privileged remote commands are audit logged as
`transport=tls cert_sha256=... subject=...`. The SHA-256 fingerprint is of the
peer certificate. Full certificates and private keys are never logged.

## Host ownership boundary

A ContainerStore record grants Chimera ownership of that exact container name.
Without a record, create and launch reject same-named online machines, raw
image files, container directories, `.nspawn` files, and systemd override
directories. An unavailable machine inventory is an observation failure, not an
empty list. Delete of an unknown record is a no-op and never targets an
unmanaged machine. The sole intentional adoption path is the operator-requested
legacy `import-nodes` command.

Provider observations fail closed. A machinectl or systemd failure is reported
as an observation error; it is never interpreted as a missing or stopped
container and cannot trigger clone, removal, recursive deletion, or image
replacement.

Only one server may own a ContainerStore directory. The kernel-held
`server.lock` flock is the authority, not a PID file. The lock file is opened
without following unexpected terminal symlinks and is never chmod'd through a
symlink. Durable reads and writes use the locked directory descriptor.
`state.json` must be a regular file in that directory; a leaf symlink or
unexpected type is rejected.

Cooperating starters that choose the same socket pathname are serialized.
Before replacing a socket path, the server proves it is a Unix socket and that
no live listener remains. Shutdown unlinks only the socket this instance bound.
Regular files, directories, symlinks, and live sockets are left untouched.
Cloud-init seed writes use the same contained, no-follow walk as custom-files.

## Filesystem boundaries

Container names are constrained for filesystem, systemd instance, and
machinectl safety. Custom-file paths are lexical relative paths with a real
leaf component; empty paths, `.`, absolute paths, parent traversal, and
backslashes are rejected. Provider mutation opens the container root with
`O_DIRECTORY | O_NOFOLLOW` and walks intermediate components with
directory-FD relative `lstat`/`openat`/`mkdirat`/`unlinkat`/`symlinkat`
operations. Intermediate symlinks are rejected rather than followed. The final
leaf is inspected and replaced without following it, so removing a container
symlink does not affect its target. Creation-time custom-file mutation is not
applied while the container is running.

## Workload exposure

The authority boundary is intentionally direct: a Chimera administrator is
already a privileged host operator. Local `chimera-admin` membership and a
trusted remote mTLS client certificate both authorize control over container
lifecycle and workload intent. Treat the administrative Unix socket and the
remote TLS key material accordingly.

Bind mounts deliberately expose server-host paths to a guest. A writable
`--bind` intentionally grants the guest the corresponding host filesystem
access; use it only for a deliberate writable location such as build output.
Prefer `--bind-ro` for source trees. Deleting a container removes
Chimera-owned container storage and configuration only. It does not remove,
truncate, recursively chown, or otherwise mutate a bind source.

`rootidmap` can map guest root to the host owner when the backing filesystem
and kernel support ID-mapped mounts. Support and resulting ownership are
filesystem-dependent. Chimera does not force an ID-mapping mode or emulate one
with recursive ownership changes; the administrator explicitly selects the
native nspawn mount option.

The `privileged` profile is for trusted workloads: it deliberately grants full
Linux capabilities and uses host UID/GID mapping. It is not a containment
boundary for untrusted code.

Published ports intentionally expose a guest service through the host according
to native systemd-nspawn networking and firewall semantics. Apply the same
address-binding, firewall, and service-hardening policy used for any
host-exposed service. systemd-nspawn remains authority for whether `Port=` is
compatible with the selected network configuration.

Purging `chimera-spawn-server` does not delete `/var/lib/chimera-spawn/state.json`,
managed machines, bind sources, or administrator-created TLS material.

## Image fetch authority

Image sources are administrator-controlled configuration. The privileged
server may fetch only from a configured HTTPS SimpleStreams source. CLI and
remote API clients may select a configured source name, a published image
reference, and an artifact kind (`rootfs` or `disk`). They cannot supply an
arbitrary source URL, artifact URL, keyring path, metadata verification
override, SimpleStreams index, product metadata override, or SHA override.

SimpleStreams source URLs must use HTTPS without embedded credentials.
Malformed host or port syntax in configured or redirect URLs is a stable
image-source error, not an uncaught parser exception.
Metadata and artifact fetches remain within that configured source origin,
including every followed redirect hop and the final response URL. Private or
internal administrator-configured HTTPS sources are allowed; the authority
boundary is the configured source origin, not public-Internet classification.
SimpleStreams metadata paths must remain relative to that configured source
base.

These trust modes are not identical:

- `ubuntu` (`metadata_verify: signature`): HTTPS, same-origin metadata, detached
  GPG verification of each authoritative JSON document against the packaged
  keyring `/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg`, then SHA-256
  (and optional advertised size) of the downloaded artifact. Signed sources
  never fall back to unsigned metadata.
- `images` (`metadata_verify: tls`): HTTPS, same-origin metadata, then SHA-256
  (and optional advertised size) of the downloaded artifact. This is not
  cryptographic publisher-signature verification. The `images` remote hosts
  unofficial convenience/test images, not official artifacts from each included
  distribution.

Artifacts are streamed to a private temporary file and checked against the
advertised SHA-256 (and size, when present) before systemd import. A digest or
size mismatch deletes the temporary material and does not call systemd import.
The `products:` section in local image configuration can provide
`custom_files` and `nspawn_parameters`. SimpleStreams metadata cannot supply
or override those fields. Local images generated in the reserved
`chimera-src-` remote-cache namespace are Chimera-owned base-image cache
objects, not unmanaged host resources. Only the exact generated digest shape is
classified as an owned cache object.
