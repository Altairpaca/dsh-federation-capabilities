# Examples: from pack descriptors to a hub

These snippets are **sanitized illustrations** of how a capability pack becomes
visible to a `dsh-federation 0.8.0` hub. Every value here is a placeholder
(`example.invalid`, `127.0.0.1`, `/opt/fedcap`, `REPLACE_ME`). Real host names,
addresses, tokens and home directories belong in the operator's profile and
must never be committed.

| File | What it shows |
| --- | --- |
| [`node.json`](node.json) | A worker configuration whose `capabilitiesFile` array points at the file `fedcap install` writes. |
| [`node.bad-glob.json`](node.bad-glob.json) | The same worker with a shell-style glob (`capabilities.d/*.json`), which **does not work**. |
| [`capabilities.d/network-probe.json`](capabilities.d/network-probe.json) | Illustrative output of `fedcap install` for `packs/network-probe`: resolved descriptor array. |
| [`hub-nodes.snippet.yml`](hub-nodes.snippet.yml) | Fragment for the hub plugin's `config.nodes[]` (HTTP and SSH worker variants). |
| [`topology.json`](topology.json) | Fragment of a desired-state `topology.json` (v1) that registers the same worker. |

## How 0.8.0 loads it

`dsh-federation` 0.8.0 already understands `capabilitiesFile`. The installer
does not modify federation core; it only writes a descriptor file and edits the
node configuration:

```
packs/network-probe/fedcap.json
        │  fedcap install
        ▼
capabilities.d/network-probe.json   (resolved descriptor array)
        │  node.json: "capabilitiesFile": ["capabilities.d/network-probe.json"]
        ▼
mesh-node manifest --config node.json      ← worker advertises the capabilities
        │  hub refresh() / manifest.get
        ▼
hub registry → DSH mesh_capabilities / mesh_dispatch / MCP / A2A
```

Load and check the worker:

```bash
# 1. the descriptor file must exist next to the node configuration it is
#    referenced from (a relative path is resolved against that directory)
mesh-node doctor  --config /opt/fedcap/mesh/node.json
mesh-node manifest --config /opt/fedcap/mesh/node.json

# 2. serve it over HTTP (or reuse sshd for the SSH transport)
mesh-node serve --port 8787 --host 127.0.0.1 \
  --token-file /opt/fedcap/mesh/http.token --config /opt/fedcap/mesh/node.json
```

On the hub, register the worker with the `hub-nodes.snippet.yml` fragment (or
with a `topology.json` worker entry, which is the 0.8.x desired-state path), then
refresh:

```bash
# hub side: the transport token comes from the hub environment
export MESH_NODE_TOKEN="$(cat /opt/fedcap/hub/mesh-node.token)"
# restart the hub profile, or GET /mesh/api/state?refresh=1 if the plugin is live
```

The hub's registry now lists `net.egress` and `web.fetch` for `example-worker`;
the router agent sees them through the `mesh_capabilities` tool and can call
them with `mesh_dispatch`.

> A `manifest` probe reads descriptors only. It does not execute the executor
> scripts, so a wrong `{{python}}` path or a missing pack directory shows up on
> the first real invocation, not in `manifest`. Keep the installed pack
> directory stable, or re-run `fedcap install --force` after moving it.

## Why the glob example is broken

`node.bad-glob.json` sets:

```json
"capabilitiesFile": "capabilities.d/*.json"
```

`mesh-node` treats `capabilitiesFile` as **literal paths**: a string, or an array
of strings. There is no glob expansion and no directory scan. With the glob
above the worker looks for a file literally named `*.json`, does not find it, and
prints a diagnostic to stderr:

```text
capability file not found: /opt/fedcap/mesh/capabilities.d/*.json
  (continuing with the inline descriptors)
```

The node still starts, but it advertises **zero** capabilities, so dispatch on
the hub fails with `UNKNOWN_CAPABILITY` while the worker looks healthy. Other
common variants that fail the same way:

- a directory (`"capabilities.d/"` — it is not expanded either);
- an object instead of a string/array (the installer refuses to edit it:
  `capabilitiesFile must be a string or an array of strings`);
- duplicate capability ids across files (the node refuses to build because a
  node cannot declare the same capability twice).

The fix is always the explicit array produced by `fedcap install`:

```json
"capabilitiesFile": ["capabilities.d/network-probe.json"]
```

A missing file is a diagnostic rather than a hard failure in 0.8.0, which makes
this a silent-degradation trap: check the worker's manifest, not just its exit
code.

## Hub node entry vs topology entry

Both are equivalent ways to register the same worker:

- `hub-nodes.snippet.yml` is the 0.7-style static `config.nodes[]` list. It is
  still accepted (and boots the first `topology.json` on a fresh 0.8.0 install).
- `topology.json` is the 0.8.x desired state (single source of truth, `revision`
  CAS). The plugin materializes it from `config.nodes[]` on first start; after
  that, edit the topology (or use the 0.8.1 worker write API) instead of the
  static list.

The `capabilities` field in a node entry is optional routing metadata. The hub
learns the authoritative capability list from the worker manifest after
`refresh()`; keep it only as a human hint.

## Sanitization

All examples are scanned by `npm run sanitize` in CI. When adapting them:

- keep placeholder domains (`example.invalid`), loopback addresses
  (`127.0.0.1`) and `/opt/fedcap` paths;
- never commit tokens — reference an environment variable name
  (`MESH_NODE_TOKEN`) or a `0600` token file path that lives outside git;
- never commit real host names, public or private addresses, or home
  directories;
- run `npm run sanitize` and `npm run sanitize:history` before opening a PR
  that touches examples.
