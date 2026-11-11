# Writing a capability pack

This guide is for external contributors. A pack is a small, reviewable package:
one manifest, a few zero-dependency executors, a README and a self-check. The
formal format is [`spec/capability-pack-v1.md`](../spec/capability-pack-v1.md);
this page is the practical path from idea to merged PR.

If you only want to **use** a pack, read
[`docs/integration.md`](integration.md) and the pack README instead.

## 1. Minimum shape

```
packs/my-pack/
├── fedcap.json      manifest (placeholder templates + config + checks)
├── bin/hello.py     executor: one JSON in on stdin, one JSON out on stdout
├── README.md        capability table, install, security notes, self-check
└── deploy/          optional: host-side service templates (sanitized)
```

Start by copying the shape of an existing pack (`packs/network-probe` is a good
small reference) and keep the diff focused: one pack, one capability family.

A minimal manifest:

```jsonc
{
  "v": 1,
  "id": "my-pack",                    // [a-z0-9][a-z0-9-]*; also the output file name
  "title": "My pack",
  "version": "0.1.0",
  "description": "One or two sentences for `fedcap list`.",
  "license": "MIT",
  "requires": { "python": ">=3.8" },
  "config": {
    "baseUrl": {
      "default": "http://127.0.0.1:3002",
      "description": "Where the host service listens."
    }
  },
  "capabilities": [
    {
      "id": "my.fetch",
      "title": "Fetch one thing",
      "description": "What it returns and what it refuses.",
      "tags": ["example"],
      "idempotent": true,
      "timeoutMs": 30000,
      "inputSchema": {
        "type": "object",
        "properties": { "url": { "type": "string", "minLength": 1 } },
        "required": ["url"],
        "additionalProperties": false
      },
      "outputSchema": { "type": "object" },
      "invoke": {
        "kind": "shell",
        "command": "{{python}} {{packDir}}/bin/hello.py",
        "env": { "MY_PACK_BASE_URL": "{{config.baseUrl}}" }
      }
    }
  ],
  "checks": [
    { "description": "python syntax", "command": "{{python}} -m py_compile {{packDir}}/bin/hello.py" },
    { "description": "offline self-test", "command": "{{python}} {{packDir}}/bin/hello.py --self-test" }
  ]
}
```

`{{packDir}}`, `{{python}}`, `{{node}}` and `{{config.<key>}}` are the only
placeholders. Absolute deployment paths, real host names and secrets in
`fedcap.json` are review failures (see §6).

## 2. Steps

1. **Pick the ids.** Pack id: lowercase, hyphenated, stable (it becomes
   `capabilities.d/<id>.json`). Capability ids: `<domain>.<verb>`
   (`net.egress`, `gpu.info`, `web.search`), unique inside the pack and across
   packs installed on one node.
2. **Decide the invocation kind.** `shell` is the default for packs: a fixed
   command, arguments as JSON on stdin, one JSON object on stdout. Do not
   interpolate request data into the command string.
3. **Write the manifest.** Describe every input in `inputSchema`, mark
   read-only capabilities `idempotent: true` (side-effecting ones `false` and
   document the confirmation/allowlist expectation), set a realistic
   `timeoutMs`, and point `invoke.env` at the config keys the executor needs.
4. **Write the executor.** Zero third-party dependencies preferred (standard
   library only); bound every read, every request and every output; return
   `ok:false` JSON for expected failures instead of crashing; keep stdout to
   exactly one JSON value.
5. **Write the README.** Capability table, requirements, config table, install
   example, invocation JSON, output example, security notes, local self-check.
6. **Add checks.** `py_compile` (or equivalent) plus an offline `--self-test`
   that exercises pure logic, validation and failure paths without touching the
   network. CI must never need credentials or a live service.
7. **Validate and test locally** (§5), then open the PR.

## 3. Manifest reference

| Field | Required | Notes |
| --- | --- | --- |
| `v` | yes | Must be `1`. |
| `id` | yes | `[a-z0-9][a-z0-9-]*`; also the descriptor file name. |
| `version` | yes | Semver. Bump when the capability behaviour or schema changes. |
| `title`, `description`, `license` | title yes | Human-facing; keep the description free of deployment data. |
| `requires` | no | Informational (`node`, `python`); matched by reviewers, not enforced at runtime. |
| `config` | no | Each key needs a `default`; referenced only through `{{config.<key>}}`. |
| `capabilities[]` | yes | Non-empty; descriptor rules from `mesh/1` (id, invoke, schemas, tags, idempotent, timeoutMs, visibility). |
| `checks[]` | no | Commands a reviewer/CI can run; each needs `command`. Offline only. |
| `deploy` | no | `{ "docs": "deploy/README.md" }` for host-side templates. |

Descriptor rules that matter in practice:

- `invoke.kind` is `agent | shell | mcp | tool`; a pack almost always uses
  `shell` (packs ship executables, not prompts).
- `inputSchema`/`outputSchema` are advertised to the hub and the router agent,
  but **not enforced** by the node or the hub. Validate inside the executor.
- `timeoutMs` bounds the whole invocation on the worker; keep internal timeouts
  shorter so the executor can return a structured error first.
- `visibility: node-local` hides a capability from peer hubs; the default
  `shared` is right for most packs.
- `idempotent` is a promise the hub relies on for retries. Read-only = `true`.

## 4. Executor contract

| Aspect | Requirement |
| --- | --- |
| stdin | exactly one JSON object (the capability `args`); tolerate an empty stdin as `{}` |
| stdout | exactly one JSON object, bounded in size; no logging, no banners |
| stderr | human diagnostics only; the node keeps the tail for error reporting |
| exit code | `0` after a report was produced (report failures as `ok:false` JSON); non-zero only for cases where no JSON result is possible |
| config | read from environment variables set by `invoke.env`; never from file paths baked into the manifest |
| secrets | a **path** to a `0600` token file, read at execution time; never echo or log the token |
| paths | use `{{packDir}}` and config values; do not rely on the worker's cwd |
| time/output | bound everything: request timeouts, read sizes, response bytes, list lengths |
| errors | stable `error.code` strings (`TIMEOUT`, `TARGET_BLOCKED`, `HTTP_ERROR`, …) and messages with no secrets or internal paths |

A useful convention for JSON responses:

```json
{"ok": true, "kind": "<pack>.<verb>", "data": {"...": "..."}, "elapsedMs": 12}
```

```json
{"ok": false, "kind": "<pack>.<verb>", "error": {"code": "TARGET_BLOCKED", "message": "the target is not public"}}
```

## 5. Testing requirements

Run at least the following before opening a PR:

```bash
# manifest: placeholders, descriptor shape, executor files exist
node packages/fedcap/bin/fedcap.js validate packs/<id>

# repository gates
node scripts/check-packs.mjs
node scripts/check-sanitize.mjs
npm test

# executor checks (the same commands listed under checks[])
python3 packs/<id>/bin/<executor>.py --self-test

# end-to-end install into a scratch node configuration (never a real one)
mkdir -p /tmp/fedcap-scratch
printf '%s\n' '{"nodeId":"scratch-worker","kind":"shell","stateDir":"/tmp/fedcap-scratch/state"}' \
  > /tmp/fedcap-scratch/node.json
node packages/fedcap/bin/fedcap.js install --pack packs/<id> --node-config /tmp/fedcap-scratch/node.json
node packages/fedcap/bin/fedcap.js doctor --node-config /tmp/fedcap-scratch/node.json
mesh-node manifest --config /tmp/fedcap-scratch/node.json
mesh-node invoke --capability <id> --args-json - --config /tmp/fedcap-scratch/node.json <<< '{}'
```

What the checks must cover:

- **offline by default** — CI and `checks[]` must not depend on a live service,
  credentials or outbound network; mark any live test as a manual step in the
  README;
- **pure logic** — input parsing/validation, target classification, output
  truncation/fitting, error mapping;
- **failure paths** — invalid input, refused target, upstream error, timeout
  handling (mock where possible);
- **bounded output** — assert the serialized result fits the configured cap;
- **install round trip** — `fedcap install` + `doctor` + `mesh-node manifest` as
  above, then one real invocation on a host you control;
- **documented gaps** — if a capability cannot be tested in CI, say what was
  tested manually and on which kind of host (without recording deployment data).

## 6. Sanitization rules

This repository is the public distribution. The tree and history are scanned by
`npm run sanitize` / `npm run sanitize:history`; CI runs the tree scan. Do not
commit:

- real host names, domain names, public or private IP addresses (the sanitizer
  flags **all** dotted IPv4 literals except `127.0.0.1`/`0.0.0.0`);
- usernames, home directories, absolute Windows paths, e-mail addresses;
- tokens, API keys, cookies, private keys, token-file contents;
- deployment identifiers of the reference installation (host names, service
  container names, tailnet names).

Allowed placeholders: `example.invalid`, `example.com` in e-mail-shaped values,
`127.0.0.1`, `0.0.0.0`, `/opt/fedcap`, `REPLACE_ME`, and relative paths inside
the pack. For tests that need IP-shaped inputs, construct addresses from
integers instead of writing literals (see
`packs/network-probe/bin/*.py --self-test`), and mask any live output before
pasting it into a PR.

When in doubt: the value belongs in the operator's install values
(`--values`/`--set`) or a token file, not in the pack.

## 7. Security checklist

- [ ] **No command injection**: `invoke.command` is fixed; user input only via
      stdin JSON. Never build shell strings from args, never `shell=True` with
      interpolated input.
- [ ] **No secret leakage**: tokens only by file path, read at run time; not in
      argv, stdout, logs, error messages or the report.
- [ ] **SSRF / target validation** for anything that makes outbound requests:
      scheme allowlist, private/loopback/link-local/CGNAT/reserved refusal,
      redirect re-validation, host resolution checks, port restrictions.
- [ ] **Bounded resources**: request timeouts, response size caps, output caps,
      list lengths, child-process timeouts.
- [ ] **Least privilege**: no root requirement, no writes outside a scratch
      directory, no killing/affecting other processes, no broad filesystem
      access.
- [ ] **Side effects are explicit**: `idempotent: false`, documented
      confirmation/allowlist expectations, no silent retries of writes.
- [ ] **Safe defaults**: loopback bindings for host services; the pack fails
      closed when its token/config is missing.
- [ ] **Error hygiene**: stable error codes; no stack traces, host names or
      absolute paths in the returned JSON.
- [ ] **Dependencies**: standard library only unless the README and reviewers
      explicitly accept otherwise; no post-install scripts.

## 8. Review checklist

- [ ] `fedcap validate` passes; the spec rules in
      [`spec/capability-pack-v1.md`](../spec/capability-pack-v1.md) are respected.
- [ ] `fedcap install` into a scratch `node.json` passes and `doctor` is clean.
- [ ] `mesh-node manifest` advertises the expected ids and schemas.
- [ ] One real invocation succeeds on a controlled host; failure paths return
      structured JSON.
- [ ] `npm test`, `npm run check:packs`, `npm run sanitize` pass locally (and
      `sanitize:history` before release).
- [ ] README covers requirements, install, invocation, output, security notes and
      the local self-check.
- [ ] No deployment data, no secrets, no absolute deployment paths.
- [ ] Capability ids are namespaced and unique; `timeoutMs`/`idempotent`/
      `visibility` are justified.

## 9. Versioning and stability

- Bump `version` for any behaviour change; breaking changes to an input schema
  or output shape are a minor bump while the repository is 0.x.
- Capability ids are a public contract once released; prefer adding a new id over
  changing the meaning of an existing one.
- Document a migration note in the pack README when an install value moves or a
  capability id is deprecated.
- Packs target `dsh-federation >= 0.8.0`. Do not require core changes; document
  the gap and ship a host-side helper instead (spec §7).

Questions and design discussions belong in an issue before a large PR. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md) and [`SECURITY.md`](../SECURITY.md).
