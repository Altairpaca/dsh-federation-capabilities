# Capability pack specification v1

Status: **draft, implemented by `fedcap` v0.1** (this repository).

A *capability pack* is a zero-dependency package that adds one host-local
capability family to a `dsh-federation` mesh node. A pack contains:

- `fedcap.json` — the manifest (metadata, capability descriptor templates,
  config schema, checks, deployment notes).
- `bin/` — executor scripts. They follow the mesh shell contract: the
  invocation args arrive as one JSON value on **stdin**, exactly one JSON value
  goes to **stdout**, diagnostics go to **stderr**.
- `deploy/` (optional) — templates and scripts for host-side services the pack
  needs (for example a token gateway in front of an unauthenticated API).
- `README.md` — what the pack exposes, security notes, and a local self-check.

Packs do **not** require changes to `dsh-federation` core or the worker. They
compile down to capability descriptors that the existing node configuration
already understands (`capabilities[]` or `capabilitiesFile`).

## 1. Manifest (`fedcap.json`)

```jsonc
{
  "v": 1,
  "id": "example-pack",                 // [a-z0-9][a-z0-9-]*; also the file name
  "title": "Example capability pack",
  "version": "0.1.0",                   // semver
  "description": "One paragraph.",
  "license": "MIT",
  "requires": {                         // informational, checked by `doctor`
    "node": ">=20",
    "python": ">=3.8"                   // omit when the pack has no python
  },
  "config": {                           // values used by {{config.<key>}}
    "baseUrl": {
      "default": "http://127.0.0.1:3002",
      "description": "Where the host service listens."
    },
    "tokenFile": {
      "default": "",
      "description": "Optional bearer token file (0600)."
    }
  },
  "capabilities": [
    {
      "id": "example.fetch",            // mesh capability id
      "title": "Fetch one URL",
      "description": "What it does, in one or two sentences.",
      "tags": ["example", "web"],
      "idempotent": true,
      "timeoutMs": 60000,               // node-side execution bound
      "inputSchema": { "type": "object", "properties": { "url": { "type": "string" } }, "required": ["url"], "additionalProperties": false },
      "invoke": {
        "kind": "shell",                // agent | shell | mcp | tool
        "command": "{{python}} {{packDir}}/bin/fetch.py",
        "env": { "EXAMPLE_BASE_URL": "{{config.baseUrl}}" }
      }
    }
  ],
  "checks": [
    { "description": "python syntax", "command": "{{python}} -m py_compile {{packDir}}/bin/fetch.py" }
  ],
  "deploy": { "docs": "deploy/README.md" }
}
```

Rules:

1. `id`, `version`, `capabilities` are required. `capabilities[].id`,
   `.invoke` are required and follow the mesh/1 descriptor rules
   (`docs/002-protocol.md` in `dsh-federation`).
2. Capability ids should be namespaced (`<domain>.<verb>`) and unique inside
   the pack. Duplicate ids are a validation error.
3. `invoke.command` must be a *fixed* command string. Dynamic arguments go
   through stdin JSON; the command itself must not interpolate user input.
4. Secrets never appear in the manifest. A pack may reference a **path** to a
   secret file (`config.tokenFile`) and the executor reads it at run time.
5. No absolute deployment paths in the manifest. Use `{{packDir}}` and
   `{{config.<key>}}`; the installer substitutes them.
6. `visibility` defaults to `shared`. Packs with side effects should set
   `idempotent: false` and document a confirmation/allowlist expectation.

## 2. Placeholders

| Placeholder | Substituted with |
| --- | --- |
| `{{packDir}}` | absolute directory the pack was installed from (no trailing slash) |
| `{{python}}` | python executable reported by the installer (`--python`, default `python3`) |
| `{{node}}` | Node.js executable running `fedcap` |
| `{{config.<key>}}` | resolved config value for `<key>` |

Unknown placeholders, unresolved keys, or values containing newlines are
validation errors. Placeholder substitution is plain text replacement; the
installer refuses to write descriptors containing unsubstituted `{{`.

## 3. Config resolution

For each `config` key, in increasing precedence:

1. `default` from the manifest.
2. `--values <file>` (`{ "baseUrl": "..." }`) or `FEDCAP_CONFIG` (JSON).
3. `--set key=value` (repeatable).

`fedcap install` resolves every config key referenced by the descriptors it
writes; unused keys are allowed. Values are written into the descriptor's
`invoke.env` / `command` exactly as supplied — keep secrets in files.

## 4. Installed layout

`fedcap install --pack <dir> --node-config <node.json>` writes:

```
<node.json directory>/
  capabilities.d/<packId>.json     # resolved descriptor array (0600? no: 0644)
  node.json                        # capabilitiesFile gains capabilities.d/<packId>.json
  node.json.bak-<timestamp>        # backup before the edit
```

`capabilitiesFile` in the node configuration accepts a string or an array of
paths, resolved relative to the node configuration directory (this is existing
`dsh-federation` 0.8.0 behaviour). More than one pack can be installed; each
gets its own file. `fedcap uninstall` removes the file and the entry.

## 5. `fedcap` CLI (v0.1)

```
fedcap list [--json]
fedcap validate <pack-dir> [--json]
fedcap install --pack <pack-dir> --node-config <node.json>
               [--python <exe>] [--values <file>] [--set key=value ...] [--force]
fedcap uninstall --pack-id <id> --node-config <node.json>
fedcap doctor --node-config <node.json> [--json]
```

Exit codes: `0` success, `1` validation/runtime error, `2` usage error.

## 6. Public distribution and sanitization

This repository is the **public, sanitized** distribution. Operator-specific
values (host names, IP addresses, home directories, tokens) must never be
committed:

- manifests use placeholders only;
- examples use `example.invalid`, `127.0.0.1`, and `/opt/...` paths;
- `npm run sanitize` enforces this locally, `--history` scans all commits;
- CI runs the same gate on every push.

A private deployment installs a pack and supplies its own values at install
time; the deployment values stay outside version control.

## 7. Compatibility

Packs target `dsh-federation >= 0.8.0` (mesh/1 capability descriptors with
`capabilitiesFile`). No pack may require a core change; if a capability needs
new semantics (for example long-running jobs), the pack documents the gap and
ships a host-side helper instead.
