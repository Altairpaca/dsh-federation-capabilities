# network-probe capability pack

Outbound-network diagnostics for a `dsh-federation` mesh worker. The pack adds
two read-only capabilities, uses only the Python standard library, and needs no
daemon, account or secret.

| Capability | Invoke | Timeout | Idempotent | What it returns |
| --- | --- | --- | --- | --- |
| `net.egress` | `shell` / `bin/net_egress.py` | 30 s | yes | Public egress identity (direct and, when configured, via proxy), round-trip latency, DNS resolution status. |
| `web.fetch` | `shell` / `bin/web_fetch.py` | 60 s | yes | One public `http(s)` URL as bounded text: status, content type, timing, `content` (truncated to the cap). |

Both capabilities take their arguments as one JSON object on **stdin**, write
exactly one JSON object to **stdout** and keep diagnostics on **stderr** — the
`mesh/1` shell capability contract from
[`spec/capability-pack-v1.md`](../../spec/capability-pack-v1.md).

## Requirements

- Python >= 3.8 on the worker (`python3` by default; pass `--python` to
  `fedcap install` for a venv or a differently named binary).
- Outbound network access from the worker for the live probes. The executors are
  otherwise dependency-free.
- `dsh-federation >= 0.8.0` on the worker for `capabilitiesFile`.

`net.egress` contacts public IP-echo endpoints (listed in the script) to learn
the address they observe. `web.fetch` contacts exactly the URL you give it. No
telemetry is sent anywhere else; the pack contains no hard-coded deployment
endpoint.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `proxy` | `""` | Optional HTTP(S) proxy URL, for example `http://127.0.0.1:3128`. Empty means direct connections. Ambient `HTTP_PROXY`/`HTTPS_PROXY` variables are deliberately ignored — the mesh host passes a minimal environment, so the pack config is the single source of truth. The proxy URL itself is never written to the output. |
| `maxBytes` | `262144` | Upper bound for the JSON on stdout (clamped to 1 MiB). `web.fetch` shrinks `content` to fit and sets `truncated: true`; a per-call `maxBytes` can only lower the effective cap. |

## Install

From a checkout of this repository, on the worker host:

```bash
# 1. validate the pack definition (placeholders, descriptors, executor files)
node packages/fedcap/bin/fedcap.js validate packs/network-probe

# 2. resolve the descriptors into the node configuration
node packages/fedcap/bin/fedcap.js install \
  --pack packs/network-probe \
  --node-config ~/.mesh/node.json

# 3. inspect what was written
node packages/fedcap/bin/fedcap.js doctor --node-config ~/.mesh/node.json

# 4. the worker now advertises the capabilities
mesh-node manifest --config ~/.mesh/node.json
```

`install` writes `capabilities.d/network-probe.json` next to the node
configuration, adds that file to `capabilitiesFile`, and backs up the previous
`node.json`. To set deployment values without editing the manifest:

```bash
node packages/fedcap/bin/fedcap.js install \
  --pack packs/network-probe \
  --node-config ~/.mesh/node.json \
  --python python3 \
  --set proxy=http://127.0.0.1:3128 \
  --set maxBytes=131072
```

The installed descriptor embeds the resolved pack directory and Python path, so
keep the checkout in a stable location (or re-run `install --force` after moving
it). A restart of the worker's long-running `mesh-node serve` is required for a
new descriptor file to be advertised; one-shot SSH workers pick it up on the
next request.

## Call it

From the hub (for example through the `mesh_dispatch` tool or
`POST /mesh/api/dispatch`):

```json
{"capability": "net.egress", "args": {}}
```

```json
{"capability": "web.fetch", "args": {"url": "https://example.invalid/", "maxBytes": 65536}}
```

`example.invalid` is a reserved name and will be refused by the SSRF guard; use
a real public URL for a live check. On the worker itself:

```bash
mesh-node invoke --capability net.egress --args-json - --config ~/.mesh/node.json <<< '{}'
mesh-node invoke --capability web.fetch --args-json - --config ~/.mesh/node.json <<< '{"url":"https://example.com/"}'
```

### Output shape

`net.egress` (values illustrative; no local, DNS-resolved or non-public
addresses are ever included):

```json
{
  "ok": true,
  "kind": "net.egress",
  "checkedAt": "2026-01-01T00:00:00Z",
  "proxyConfigured": false,
  "dns": {"ok": true, "hosts": [{"host": "api.ipify.org", "ok": true, "addresses": 1, "latencyMs": 12}]},
  "direct": {"ok": true, "ip": "<public-egress-ip>", "target": "api.ipify.org", "latencyMs": 180,
             "attempts": [{"ok": true, "target": "api.ipify.org", "status": 200, "latencyMs": 180, "ip": "<public-egress-ip>"}]},
  "viaProxy": {"configured": false, "skipped": true},
  "durationMs": 210
}
```

`web.fetch`:

```json
{
  "ok": true,
  "kind": "web.fetch",
  "url": "https://example.com/",
  "finalUrl": "https://example.com/",
  "status": 200,
  "contentType": "text/html; charset=utf-8",
  "bytes": 1256,
  "truncated": false,
  "redirects": 0,
  "viaProxy": false,
  "elapsedMs": 240,
  "content": "<!doctype html>…"
}
```

Failures are still one JSON object with `ok: false` and an
`error.code` such as `INVALID_ARGUMENT`, `TARGET_BLOCKED`, `DNS_ERROR`,
`TLS_ERROR`, `TIMEOUT`, `TRANSPORT`, `HTTP_ERROR` or `OUTPUT_TOO_LARGE`.

## Safety notes

The fetch path is deliberately conservative, because it is reachable by a
remote hub:

- only `http`/`https`, only ports 80 and 443, no URL userinfo, URL length cap;
- the host must be an IP literal with a **globally routable** address, or a name
  that resolves exclusively to globally routable addresses; loopback, private,
  link-local, CGNAT, multicast, reserved and special-use names are refused;
- **every redirect hop is validated** with the same rule before it is followed;
- the response is read up to `maxBytes + 1`, `content` is truncated and flagged,
  and binary content is reported without bytes;
- a request timeout of 20 s and a redirect cap of 5 keep a slow or looping
  upstream from pinning the worker;
- `net.egress` bounds itself below its 30 s capability timeout: a per-request
  timeout of 6 s, an overall budget of about 22 s, and a configured proxy is
  probed first because it is usually the working path when direct egress is
  blocked;
- the report never contains local interface addresses, the proxy URL, resolved
  DNS addresses, request/response headers or credentials.

Residual risks are documented in [`SECURITY.md`](../../SECURITY.md): DNS
resolution and the subsequent connection are not atomic (a rebinding upstream
could change the answer after the check), certificates are validated with the
host trust store (no pinning), and `web.fetch` is not a general-purpose scraper.
For rendered markdown or JavaScript-heavy pages use the `firecrawl` pack.

`net.egress` contacts third-party IP-echo services. That is visible to those
services as an ordinary HTTPS request from the worker, and the answer (the
public egress address) is the data the capability exists to report.

## Self-check (offline)

The self-tests do not touch the network; addresses used by them are constructed
from integers so the public repository contains no IP literals.

```bash
python3 packs/network-probe/bin/net_egress.py --self-test
python3 packs/network-probe/bin/web_fetch.py --self-test
```

Expected: `{"ok":true,...,"failures":[]}` and exit code 0. The pack manifest
lists the same commands under `checks`, plus `python -m py_compile` for both
scripts.

## Layout

```
packs/network-probe/
├── fedcap.json          manifest: config, descriptors, checks
├── bin/net_egress.py    net.egress executor (stdlib only)
├── bin/web_fetch.py     web.fetch executor (stdlib only)
└── README.md            this file
```

See [`docs/integration.md`](../../docs/integration.md) for how the installed
descriptor travels from `capabilitiesFile` to the hub registry and DSH tools,
and [`docs/pack-authoring.md`](../../docs/pack-authoring.md) for writing your own
pack.
