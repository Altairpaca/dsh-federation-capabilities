# Integration: packs → capabilitiesFile → mesh node → hub → tools

This guide is for operators who already run `dsh-federation` and want to add a
capability family from this repository without touching federation core. It
describes what the installer writes, how a `dsh-federation 0.8.0` worker and hub
consume it, and where the compatibility boundary is.

The pack format itself is specified in
[`spec/capability-pack-v1.md`](../spec/capability-pack-v1.md).

## 1. The chain

```
 packs/<id>/                      this repository (public, sanitized)
 ├── fedcap.json                  manifest with {{placeholders}}
 ├── bin/*.py                     executors: stdin JSON → stdout JSON
 └── README.md
        │
        │  fedcap validate / install --pack <dir> --node-config <node.json>
        ▼
 <worker>/capabilities.d/<id>.json        resolved descriptor array
 <worker>/node.json                       capabilitiesFile gains the file
        │
        │  mesh-node manifest | serve | request
        ▼
 mesh/1 node manifest  ──►  hub refresh()/manifest.get  ──►  hub registry
        │
        ├── DSH plugin tools: mesh_capabilities / mesh_dispatch (router agent)
        ├── panel API: GET /mesh/api/state, POST /mesh/api/dispatch
        ├── MCP: mesh-mcp serve --config <node.json>   (capabilities as MCP tools)
        └── A2A: hub facade with a2a.capability = <mesh capability id>
```

Nothing in this chain changes `dsh-federation`: a pack compiles down to the same
capability descriptors a hand-written node configuration already accepts.

## 2. Install on a worker

Prerequisites: `dsh-federation >= 0.8.0` on the worker, Python 3.8+ when the
pack's executors are Python (see each pack README), and Node.js 20+ for the
`fedcap` CLI itself.

```bash
# from the capability-pack checkout on the worker host
node packages/fedcap/bin/fedcap.js validate packs/network-probe
node packages/fedcap/bin/fedcap.js install \
  --pack packs/network-probe \
  --node-config /opt/fedcap/mesh/node.json
node packages/fedcap/bin/fedcap.js doctor --node-config /opt/fedcap/mesh/node.json
mesh-node doctor   --config /opt/fedcap/mesh/node.json
mesh-node manifest --config /opt/fedcap/mesh/node.json
```

`install` performs three writes, and only these:

1. `capabilities.d/<pack-id>.json` next to the node configuration — the resolved
   descriptor array (placeholders substituted, no secrets);
2. an updated `capabilitiesFile` entry in the node configuration (`array` form,
   created when absent, existing entries preserved);
3. a timestamped backup of the node configuration before editing it.

Per-deployment values (base URLs, token file paths, a venv interpreter, a proxy)
are supplied at install time with `--python`, `--values <file>` or
`--set key=value`; they never enter this repository. See
[`examples/`](../examples/) for sanitized node, hub and topology snippets.

A long-running `mesh-node serve` must be restarted to advertise a new descriptor
file; an SSH worker (`mesh-node request` per call) sees it on the next request.

## 3. Register the worker on the hub

Two equivalent registrations are supported by 0.8.0:

- **static list** — add the worker to the plugin's `config.nodes[]` (see
  [`examples/hub-nodes.snippet.yml`](../examples/hub-nodes.snippet.yml));
- **desired-state topology** — add a `workers[]` entry in
  `stateDir/topology.json` (see [`examples/topology.json`](../examples/topology.json)).
  On a fresh install the plugin materializes the topology from `config.nodes[]`
  once; afterwards the topology file is authoritative.

Then refresh and verify from the hub:

```bash
# plugin panel API: refresh first so the manifest cache is current
# (same-origin / loopback; use an SSH tunnel or VPN for remote access)
curl -s "http://127.0.0.1:3080/mesh/api/state?refresh=1"

# router agent side: list what the mesh now offers, then dispatch
# mesh_capabilities            → net.egress, web.fetch (node: example-worker)
# mesh_dispatch                → {"capability":"net.egress","args":{}}
```

A capability only appears after the hub has fetched the worker manifest. If the
worker is unreachable, the hub keeps the previous cached manifest and shows the
node as unreachable — a dispatch then queues as a pending task or fails over,
depending on the dispatch options.

## 4. Who owns what

| Layer | Owns | Does not own |
| --- | --- | --- |
| Capability pack (this repo) | manifest templates, executor scripts, pack README/checks, deploy templates | host addresses, tokens, deployment paths |
| `fedcap` CLI | validation, placeholder/config resolution, writing `capabilities.d/*.json`, editing `capabilitiesFile`, backup/uninstall, `doctor` | runtime capability execution, mesh protocol |
| Worker (`mesh-node` / node config) | loading descriptor files, invoking `shell` capabilities, stdin/stdout contract, timeouts and output guard, manifest | capability discovery on the hub, retries |
| Hub (`dsh-federation` plugin/core) | manifest refresh, registry, selection, transports, pending/retry, persona/experience sync | executor implementation, argument validation of capabilities |
| DSH / MCP / A2A surfaces | exposing mesh capabilities to agents (tool calls, MCP tools, A2A text tasks) | pack internals |

## 5. Compatibility boundary with dsh-federation 0.8.0

Everything below is existing 0.8.0 behaviour; a pack must live inside it.

### 5.1 `capabilitiesFile`

- accepts a **string or an array of strings**; each entry is a path relative to
  the node configuration file (a leading `~` is expanded);
- a capability file may hold a single descriptor, an array, or
  `{ "capabilities": [...] }`;
- a missing file is a **diagnostic, not a fatal error** — the node continues with
  inline `capabilities[]` and may silently advertise fewer capabilities;
- invalid descriptors are dropped with a diagnostic;
- a node cannot declare the same capability id twice (`createNode` rejects
  duplicates), so id collisions across packs break the node build.

### 5.2 `shell` capability contract

- `invoke.command` is a **fixed command string**, executed by the worker's shell;
  user input must arrive as JSON on **stdin**, never interpolated into the
  command (this is also what makes packs safe to install);
- exactly one JSON value on **stdout**; diagnostics on **stderr**; non-zero exit
  is surfaced as `CAPABILITY_FAILED` with the stderr tail;
- the node enforces the descriptor/capability `timeoutMs` and an output-size
  guard (8 MiB by default) and kills the child on either bound;
- the child environment is **not** the worker's full environment: only `PATH`,
  `HOME` (plus a few platform variables on Windows) and the descriptor's
  `invoke.env` are passed. Packs must pass every custom value through
  `invoke.env` (that is what the `{{config.*}}` placeholders resolve into);
- capability arguments are not validated against `inputSchema` by the node or by
  the hub: **executors must validate their own input** and return structured
  errors.

### 5.3 Transport independence

A pack does not know or care whether the hub reaches the worker over SSH, HTTP
or the in-process local transport: the executor contract is identical. Paths in
`invoke.command` are resolved **on the worker**, so use `{{packDir}}`-based paths
and avoid depending on the worker's working directory.

### 5.4 What packs cannot do in 0.8.0

- no streaming or long-lived jobs as one request — document a background helper
  and expose submit/status/result capabilities instead (see the `gpu-nvidia`
  pack pattern);
- no new authentication, ACL or secret semantics — the transport authenticates
  the hub, the node account authorizes the executor;
- no per-capability allowlist beyond what the node configuration and the hub's
  `allowedNodes` already provide;
- no protocol or descriptor-schema extension. If a pack needs new mesh
  semantics, document the gap and ship a host-side helper instead of forking
  the protocol (see `spec/capability-pack-v1.md` §7).

## 6. Multiple packs, upgrades and rollback

**Stacking.** Each pack gets its own `capabilities.d/<pack-id>.json`; `install`
appends that path to the node's `capabilitiesFile` array and keeps the other
entries. Install order does not matter, but:

- capability ids must be unique across all installed packs (namespacing by
  `<domain>.<verb>` is the convention);
- more packs mean a longer manifest and more executor paths to keep valid;
  `fedcap doctor` checks files, ids and executor first tokens;
- keep pack directories in a stable location: resolved descriptors embed the
  absolute pack directory and Python path. Moving the checkout requires
  `fedcap install --force` (or a re-install) for every pack.

**Upgrades.** Re-run `install` for the pack. If the resolved descriptor content
changed, the installer refuses to overwrite without `--force`; with `--force` it
replaces the file and backs up `node.json` again. Re-read the pack README for
config key changes, and restart the worker's long-running service.

**Rollback / uninstall.**

```bash
node packages/fedcap/bin/fedcap.js uninstall \
  --pack-id network-probe \
  --node-config /opt/fedcap/mesh/node.json
mesh-node manifest --config /opt/fedcap/mesh/node.json   # confirm the ids are gone
```

`uninstall` removes the descriptor file and the `capabilitiesFile` entry (backing
up the node configuration). The hub removes the capabilities after its next
manifest refresh. Tasks already queued in the hub's pending queue for a removed
capability will fail with `UNKNOWN_CAPABILITY` when replayed — they are not
silently dropped, but they do not succeed either.

## 7. The same capabilities on MCP and A2A

- **MCP.** `mesh-mcp serve --config <node.json>` exposes the node's
  capabilities as MCP tools over stdio; dots map to underscores
  (`net.egress` → `net_egress`). This is how a foreign MCP-speaking agent uses a
  pack without speaking `mesh/1`. The bridge does not carry persona, sessions or
  experiences.
- **A2A.** The hub's optional A2A facade maps one text task to either a local
  reply or a mesh dispatch. Set `a2a.capability` to a mesh capability id (for
  example `web.fetch` is a poor fit because A2A carries text, while
  `net.egress` results serialize into the artifact text); A2A is default-off,
  requires per-peer bearer tokens and a reachable `publicUrl`, and is an outer
  interop facade — it does not replace `mesh/1`.

## 8. Optional future enhancement: `mesh-node capabilities` (0.8.2+ candidate)

Today `fedcap doctor` and `mesh-node manifest` cover descriptor presence, and
`mesh-node doctor` covers runtime basics. A small, optional CLI addition in a
later 0.8.x line could make the pluggable path more ergonomic — for example:

```text
mesh-node capabilities list    --config <node.json> [--json]
mesh-node capabilities validate --config <node.json>
```

It would print the merged descriptor list with its source file, detect duplicate
ids across `capabilitiesFile` entries, and optionally stat executor paths. Such
a command is **not implemented in 0.8.0/0.8.1** and is not required by any pack:
it changes no descriptor, manifest or protocol field, only operator ergonomics.
Packs must keep working with plain `mesh-node manifest` regardless of whether the
command ever ships.

## 9. Troubleshooting

| Symptom | Likely cause | Check |
| --- | --- | --- |
| Hub lists fewer capabilities than expected | `capabilitiesFile` entry missing/unresolved, descriptor invalid, or worker advertises an old file | `mesh-node manifest --config …`; `fedcap doctor`; worker logs |
| `UNKNOWN_CAPABILITY` on dispatch | capability id absent from the worker manifest (removed pack, stale cache, broken file) | refresh the hub (`?refresh=1`), re-read the worker manifest |
| `CAPABILITY_FAILED` with `exitCode` | executor crashed or exited non-zero (bad Python path, unhandled exception) | run the exact `invoke.command` on the worker; check `stderr` tail |
| `CAPABILITY_FAILED`, no output | executor printed diagnostics to stdout instead of one JSON object | keep stdout to exactly one JSON value, diagnostics on stderr |
| Capability timed out | executor exceed descriptor `timeoutMs` or downstream is slow | raise `timeoutMs`, add internal timeouts, document the bound |
| Works over SSH but not HTTP (or vice versa) | transport-specific reachability or token, not the pack | `mesh-node doctor`, `fedcap doctor`, transport docs in `dsh-federation/docs/003-transports.md` |
| Newly installed capability missing after restart | long-running `serve` not restarted, or pack directory moved | restart the worker; re-install with `--force` |
| `web.fetch` refuses a target | SSRF guard (private/loopback/reserved host, non-80/443 port, userinfo, redirect) | expected; use a public endpoint or a pack that documents an allowlist |

For transport, authentication and network-direction details (who dials whom,
which ports are required), see `dsh-federation/docs/003-transports.md` and
`dsh-federation/docs/004-security.md`.
