# Migration from node YAML

`configs/nodes/*.yaml` is no longer runtime lifecycle authority. Images,
profiles, and cloud-init remain catalogs; each managed container now has durable
CLI-owned intent in the ContainerStore. Import a node YAML file explicitly:

```sh
chimeractl config import-nodes configs/nodes/example.yaml --dry-run
chimeractl config import-nodes configs/nodes/example.yaml
```

Dry-run sends the parsed file to the server for the same catalog, ownership, and
semantic validation as a real import. It writes no state and performs no host
mutation. Adoption of an existing materialized container is recorded as
provisioning `unknown`. A declaration whose host materialization is proven
absent is recorded as `pending` before Chimera creates it. Public container
names may not use the reserved `chimera-src-` image-cache prefix. A real
import revalidates under the lifecycle lock and writes the valid batch
atomically.

The YAML mapping key is the container identity. An embedded `name` must match
the key exactly. Entries with `ensure: absent` are rejected: migration never
turns an old “absent” declaration into a newly created managed container or an
implicit delete. The unambiguous lifecycle mappings are:

- `ensure: present`, `state: running`, `autostart: true` → desired running
- `ensure: present`, `state: stopped`, `autostart: false` → desired stopped

The other state/autostart combinations are rejected because they express a
separate old boot policy that the durable lifecycle model does not silently
reinterpret. Missing `image_source`, canonical product, profiles, cloud-init
templates, unsupported disk guest-filesystem mutation, and incompatible
existing managed records are also rejected.
Re-running an exact successful import reports compatible records as already
imported and does not overwrite them.

## CLI compatibility changes

`spawn` remains a deprecated alias for `launch`, but its contract changed. It
now requires `spawn IMAGE NAME`; it no longer selects a node-YAML declaration.
`spawn --all` has been removed. Import node declarations first, then use
`launch`, `start`, and other per-container lifecycle commands.

`remove` remains a deprecated alias for `delete`. `status NAME` remains
temporarily compatible with `info NAME` and prints a deprecation warning on
stderr; plain `status` and `server status` report the server.

Local operation uses the FHS Unix socket by default. Server path options and
command-local `chimeractl` `--socket` / `-s` provide explicit runtime
overrides; they do not infer paths from the current directory. Remote
administration uses command-local `--host` / `-H` with `--tls-ca`, `--tls-cert`,
and `--tls-key` (or `CHIMERA_HOST` / `CHIMERA_TLS_*`). Remote access is
HTTPS/WSS with mutual TLS and is disabled on the server until configured.

Operator-controlled upstream version remains **1.0.0**. Breaking CLI and
lifecycle contracts are documented here independently of that version number.
