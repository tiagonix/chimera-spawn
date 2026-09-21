# Architecture

Chimera Spawn is a client/server systemd-nspawn manager. Local and remote
clients share one server API:

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

## Scope and non-goals

Chimera is a single-host system-container manager, not a clustering platform.
It does not provide a Chimera-managed storage-pool or snapshot subsystem, or
an independent job subsystem. Authorization uses the current local
administrator-group and verified-client certificate model; Chimera does not
add certificate RBAC. Package installation does not generate PKI material.

Chimera uses systemd-nspawn and systemd-native networking, including shared and
offline networking, `Zone=`, `Port=`, and host systemd-networkd integration.
It does not implement an independent network control plane, subnet/IPAM
service, or firewall/NAT engine.

## Runtime model

Chimera is a control plane for native system containers. It records desired
state, validates it, and renders host configuration; systemd and
systemd-nspawn create, supervise, and enforce the resulting runtime.

```text
                                  OPERATOR / AI AGENT
                                          |
                                      chimeractl
                                          |
                         Unix socket / HTTPS + mTLS
                                          |
                                          v
+--------------------------------------------------------------------------------+
|                              CHIMERA SERVER                                    |
|                                                                                |
|     +------------+      +----------------+      +----------------+             |
|     | ApiServer  | ---> | CommandService | ---> |  StateEngine   |             |
|     +------------+      +----------------+      +-------+--------+             |
|                                                       |                        |
|                                             durable desired state              |
|                                                       |                        |
|                                               +-------v--------+               |
|                                               | ContainerStore |               |
|                                               +----------------+               |
|                                                                                |
|        catalogs / creation inputs                    runtime intent            |
|  +------------------------------+       +-------------------------------+      |
|  | ImageSourceSpec              |       | ProfileSpec                   |      |
|  | local product policy         |       |                               |      |
|  | cloud-init templates         |       | bind/tmpfs declarations       |      |
|  | genuine custom_files         |       | published ports               |      |
|  +--------------+---------------+       | resource controls             |      |
|                 |                       +---------------+---------------+      |
+-----------------|---------------------------------------|-----------------------+
                  |                                       |
       CREATION / ROOTFS PATH                   HOST / RUNTIME PATH
                  |                                       |
                  v                          +------------+------------+
       +-------------------------+           |                         |
       | rootfs materialization  |           v                         v
       | cloud-init              |   +------------------+     +------------------+
       | custom_files            |   | NAME.nspawn      |     | service override |
       +------------+------------+   |                  |     |                  |
                    |                | ResolvConf=      |     | CPUQuota=        |
                    |                | Parameters=      |     | CPUWeight=       |
                    |                | Bind=             |     | MemoryHigh=      |
                    |                | BindReadOnly=     |     | MemoryMax=       |
                    |                | Tmpfs            |     | MemorySwapMax=   |
                    |                | Zone= / Port=     |     | TasksMax=        |
                    |                +--------+---------+     | IOWeight=        |
                    |                         |               +---------+--------+
                    |                         |                         |
                    v                         +------------+------------+
       /var/lib/machines/NAME                              |
                    |                                      |
                    +------------------+-------------------+
                                       |
                                       v
                         systemd-nspawn@NAME.service
                                       |
                     +-----------------+------------------+
                     |                                    |
                     v                                    v
            systemd-nspawn runtime                 systemd / cgroups
        namespaces / mounts / network          supervision / resources
                     |                                    |
                     +-----------------+------------------+
                                       |
                                       v
                                  guest systemd
                                       |
                               NotifyReady=yes
                                       |
                                       v
                                   workload
```

### Authority boundaries

Chimera owns catalog selection, durable desired state and workload intent,
validation, rendering, lifecycle operations, the client/server API, and access
control. systemd and systemd-nspawn own process supervision, namespaces,
UID/GID isolation, mounts, private or shared networking, DNS integration,
native `Port=` forwarding, cgroup resource enforcement, guest-init readiness,
and journald.

**Chimera stores intent and composes native systemd configuration. systemd
executes and enforces the runtime.**

This boundary is deliberate. When systemd or systemd-nspawn directly expresses
a policy, Chimera prefers that native mechanism to a guest-rootfs mutation.
Current examples are `ResolvConf=`, `systemd.mask=`, `fstab=no`,
`NotifyReady=yes`, `Bind=`, `BindReadOnly=`, `TemporaryFileSystem=`, `Zone=`,
`Port=`, and systemd resource-control properties. `custom_files` remains for
genuine rootfs transformations that cannot be expressed as runtime policy.

### Creation and runtime configuration domains

Creation/rootfs configuration determines how the guest filesystem is
materialized: image materialization, cloud-init, and genuine `custom_files`
mutations. Its fingerprint governs provisioning and protects an existing
rootfs from being rewritten during ordinary reconciliation.

Host/runtime configuration determines how that rootfs runs: profile `.nspawn`
content, host-aware `ResolvConf=`, image `nspawn_parameters` including
`systemd.mask=` and `fstab=no`, mount directives, `Zone=`, `Port=`, profile
systemd overrides, and per-container resource controls. The final rendered
`.nspawn` file and service override are the host-configuration fingerprint
authority.

This split is why an operator can stop a container, apply changed runtime
configuration, and start it again without recreating its rootfs. A runtime
change to a running container remains pending until the operator stops or
restarts it; Chimera does not stop workloads in the background merely to apply
new host configuration.

### Observation and logs

Guest and supervisor logs retain their native systemd ownership:

```text
                    RUNNING CONTAINER
                           |
                  guest systemd journal
                           |
                  journalctl -M NAME
                           |
                           v
                  Chimera WebSocket stream
                           |
                           v
                    chimeractl logs
```

```text
              systemd-nspawn@NAME.service
                           |
                     host journal
                           |
       journalctl -u systemd-nspawn@NAME.service
                           |
                           v
                  Chimera WebSocket stream
                           |
                           v
             chimeractl logs --supervisor
```

Chimera does not maintain its own log database. journald remains the logging
authority; Chimera only validates the managed target and streams journal output
to the client.

Both transports terminate at `ApiServer`. Authorization identity differs by
transport; `CommandService` does not. Unix connections use Linux `SO_PEERCRED`.
TLS connections use the verified client certificate from the handshake. HTTP
headers never supply identity.

Both transports share one route set:

- `POST /api/v1/command`
- `GET /api/v1/stream/exec`
- `GET /api/v1/stream/shell`
- `GET /api/v1/stream/logs`

Interactive `exec` sessions are opened over WebSocket. The server launches
`systemd-run --machine --wait` with `--pty` for a terminal or `--pipe` when
the client has no TTY, plus `--service-type=exec`, `--expand-environment=no`,
and a unique `chimera-exec-<id>.service` unit. TTY sessions may include a
validated client `TERM` applied as `--setenv=TERM=...`; pipe sessions do not
inherit the caller's environment. Successful completion is sent
only after queued output has been delivered. Disconnect or shutdown stops that
exact guest unit, and the server reaps owned child processes on cancellation.
`shell` uses `machinectl shell` with the same TERM metadata on a TTY and does
not own a Chimera transient unit.

`CommandService` authorizes requests and exposes stable errors. `StateEngine`
serializes lifecycle mutations, validates requests, records desired state, and
reconciles the host. Providers perform the narrow `machinectl`, systemd,
cloud-init, and filesystem operations required for systemd-nspawn.

## Package topology

- Administrative workstation: `chimera-spawn-client`
- Managed container host: `chimera-spawn-server`
- All-in-one host: `chimera-spawn` (depends on both)
- Shared implementation: `python3-chimera-spawn`

Server installation does not require `chimeractl`.

## State and ownership

Image definitions, profiles, and cloud-init declarations are
operator catalogs. They are not container state. Local product policy is part
of each image source, not container state. Packaged profiles are `standard` (the create/launch default),
`compat`, `privileged`, `offline`, and `private`. Optional YAML `description`
fields are catalog metadata, not source-code constants. Site files under
`/etc/chimera-spawn/profiles/` overlay the packaged catalog. `private` uses
nspawn `Zone=chimera` and relies on host systemd-networkd container-zone
support for DHCP, routing, and masquerading; Chimera does not assign a subnet.
CLI-created containers are recorded in
`/var/lib/chimera-spawn/state.json`; that registry is the only mutable lifecycle
authority. A record stores desired `running` or `stopped` state and a deletion
marker. Its durable workload intent also stores `bind_mounts`, `tmpfs_mounts`,
`port_forwards`, and optional `resource_controls` (`memory_high`, `memory_max`,
`memory_swap_max`, `tasks_max`, `cpu_quota_percent`, `cpu_weight`, and
`io_weight`). Node-YAML `ensure`, `state`, and `autostart` fields are parsed
only by `config import-nodes` and never control runtime lifecycle.

`image pull` materializes a resolved SimpleStreams product and artifact as an
explicit host action. Images do not acquire ContainerStore desired-state
records.

### Image subsystem authority

Chimera has one managed image subsystem: SimpleStreams. Named packaged sources
are `ubuntu` (official Ubuntu cloud images, signed metadata) and `images`
(Canonical-hosted multi-distribution LXD convenience/test imagery, TLS
metadata).

- `ImageSourceSpec` declares source location and trust (`url`,
  `metadata_verify: signature|tls`, and a keyring path when signatures are
  required) plus local policies keyed by canonical products from that source.
  `products:` supplies `custom_files` and `nspawn_parameters`. A missing
  product policy is empty, not a source failure. SimpleStreams metadata is
  remote image discovery and artifact authority; it cannot supply or override
  those local fields.
- `artifact_kind` is `rootfs` (directory materialization, default) or `disk`
  (systemd raw image). Concrete SimpleStreams `ftype` values such as
  `root.tar.xz`, `squashfs`, `disk-kvm.img`, and `disk1.img` are storage
  formats, not durable identity.
- `EffectiveImage` is a transient composition of source, canonical product,
  artifact kind, local cache name, and policy. It is not persisted.
- `ProfileSpec` is reusable operator-selected runtime policy.

An unqualified image request queries configured SimpleStreams sources. Exactly
one match may be used. Multiple matches require `--source`. An unavailable
candidate source does not silently establish uniqueness. `--source` searches
only that configured source name. Clients cannot supply an arbitrary source or
artifact URL.

Remote create/launch canonicalizes the typed reference before persistence:
`image_source` plus the canonical product plus `image_artifact`. That
canonicalization resolves the request through the configured SimpleStreams
source, including a source-published alias. Local systemd image names are
derived from source, canonical product, and artifact kind, use the reserved
`chimera-src-` prefix, and do not include aliases, serials, URLs, or ftypes.
Generated cache objects match `chimera-src-` plus a 32-hex digest. Those cache
images are Chimera-owned and are not reported as unmanaged host resources.
After that resolution, a completed read-only cache for the same source,
canonical product, and artifact kind is reused and is not downloaded again. A
writable or unreadable cache is not accepted. Chimera does not refresh a
completed cache when a newer serial appears. User-facing lookup uses
source-published aliases or an exact product key; generic release/version
metadata is not treated as an invented alias. Ordinary stop/start/info/delete
of an already materialized container does not re-resolve the original alias.

```text
                              user image reference
                                      |
                          named SimpleStreams source
                                      |
                           source-published aliases
                              / canonical product
                                      |
                           requested artifact kind
                              rootfs or disk
                                      |
                            verified remote artifact
                              SHA-256 / size / ftype
                                      |
                           local product policy
                                      |
                                EffectiveImage
                                      |
                    local systemd image (chimera-src-<digest>)
                                      |
                              container clone
                                      |
         rootfs: custom_files / cloud-init / nspawn_parameters / profile
         disk:   nspawn_parameters / profile (no guest filesystem mutation)
```

Image sources are discovery authority for base-image import only. A new
create, launch, or pull resolves the reference through the source before cache
reuse. Newly published SimpleStreams serials are not container lifecycle
authority. A completed read-only cache is reused after that resolution;
Chimera does not contact the source to replace it. Existing containers and
completed provisioning are never rewritten because a source published a newer
build.

Requested create/launch identity includes image, profile, named cloud-init
template, mounts, port forwards, and resource controls. It is stored separately
from resolved/rendered inputs used to provision. Catalog expansion happens on
detached copies so a missing template cannot trap stop, delete, or inspection.

New create and launch requests validate catalog references, provisioning support,
reserved image names, and host-name collisions before a record is written. The
first durable create record has stopped intent and `pending` provisioning; the
first launch record has running intent and `pending` provisioning. When
materialization fails after persistence, intent and the recorded error remain so
reconciliation can retry safely.

Chimera never adopts an existing host machine, raw image, nspawn configuration,
or systemd override during ordinary lifecycle operations. A matching resource
without a ContainerStore record is a conflict. `config import-nodes` is the
single explicit, operator-requested node-YAML adoption route. Adoption of an
existing materialization is `unknown`; a declaration whose host materialization
is proven absent becomes `pending`. Public container names cannot use the
reserved `chimera-src-` image-cache prefix.

## Reconciliation and provisioning

The server holds a process-level exclusive state lock (`server.lock` via
`flock` on a verified regular file) plus an in-process mutation lock. A second
server using the same state directory cannot start, even with a different
socket. Cooperating starters that choose the same socket pathname are serialized
with a socket-path reservation. A live Unix socket is never unlinked. Shutdown
removes only this instance's bound socket.

Reconciliation converges only ContainerStore records. Provisioning states are
`unknown`, `pending`, and `complete`. Materialization checks provider
observations as `present`, `absent`, or `error`; an error never authorizes
create, replacement, or cleanup. Completion is tied to a materialization
identity (`device:inode`). If that filesystem disappears, completion is
invalidated before a replacement is cloned and initialized. `unknown`
identifies an explicitly adopted existing materialization; `pending` means
creation-time provisioning is incomplete; and `complete` means that
initialization finished for the recorded materialization identity.

Creation-time provisioning (image `custom_files` and cloud-init) runs only on a
verified stopped `pending` materialization. Fingerprints hash the rendered
output that will actually be applied, including proxy-dependent templates;
unused proxy settings do not cause drift. Ordinary reconcile does not rewrite a
live root filesystem. If the catalog definition later changes, Chimera reports
recreate-required drift instead of mutating in place. Unknown adopted
materializations are never automatically initialized, even while stopped.

Profile-driven `.nspawn` files and systemd overrides are host-side
configuration. Final `.nspawn` content combines profile policy, Chimera's
resolver default, image `nspawn_parameters`, and per-container
`Bind=`, `BindReadOnly=`, `TemporaryFileSystem=`, and `Port=` assignments.
Final service override content combines the profile with per-container scalar
resource overrides. These exact rendered files are the host-configuration
fingerprint authority; none of the per-container runtime fields enter the
creation/rootfs fingerprint. They may be applied while the container is stopped. A running
container with a changed host fingerprint is reported as pending; background
reconcile will not stop it. An explicit `restart` of a running container with
no pending host changes performs a real unit restart. Pending host
configuration is applied after a validated stop, then the unit is started.

Rendered `.nspawn` `[Exec]` sections include Chimera's host-aware resolver
policy when the profile does not set `ResolvConf=`. Hosts with a usable
`/run/systemd/resolve/resolv.conf` receive `ResolvConf=replace-uplink`; other
hosts receive `ResolvConf=replace-host`. An explicit profile `ResolvConf=` is
preserved. Image `nspawn_parameters` are the usual mechanism for image-specific
systemd runtime tokens such as `systemd.mask=` and participate in
host-configuration fingerprints. `custom_files` remain for guest-rootfs
transformations that nspawn/systemd runtime policy cannot express.

systemd and systemd-nspawn remain runtime authority. Chimera stores intent and
renders native configuration; it does not implement a separate mount, network,
resource, readiness, or log subsystem. Built-in profiles select
`NotifyReady=yes`, while site profiles are left unchanged. Existing Chimera
readiness checks remain an additional product-level verification. Logs are read
from journald with a bounded WebSocket stream and are never copied into Chimera
state.

A newly launched container is not started until initial provisioning has
completed successfully and that completion has been persisted.

## Paths and configuration

Default FHS locations are:

- configuration: `/etc/chimera-spawn`
- packaged catalogs: `/usr/share/chimera-spawn/catalog`
- site catalog overlays: `/etc/chimera-spawn/{images,profiles,cloud-init}`
- durable state: `/var/lib/chimera-spawn`
- runtime socket: `/run/chimera-spawn/server.sock`

Server `--config-dir`, `--catalog-dir`, `--state-dir`, and `--socket` options
are explicit deployment/runtime path overrides. The client accepts a
command-local `--socket` / `-s` override. These paths never depend on the
current working directory.

Catalogs and reconciliation interval may reload after a complete candidate
validates. Storage paths, proxy settings, administrator group, logging level,
remote listener host/port, and TLS certificate paths are captured by long-lived
components; changing any of them requires a server restart, and rejects the
entire candidate snapshot.
