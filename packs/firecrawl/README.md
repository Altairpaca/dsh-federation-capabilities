# firecrawl

A [Capability Pack v1](../../spec/capability-pack-v1.md) for a **self-hosted
Firecrawl** instance. It exposes two mesh capabilities:

| Capability | Kind | Timeout | Input | Output |
| --- | --- | --- | --- | --- |
| `firecrawl.scrape` | shell | 120 s | `{url, onlyMainContent?}` | `{ok, url, markdown, markdownBytes, truncated, title, sourceURL, statusCode, upstreamStatus}` |
| `firecrawl.search` | shell | 90 s | `{query, limit?}` (limit 1–10) | `{ok, query, count, results:[{title,url,description,engine}], upstreamStatus}` |

Both executors are **zero-dependency Python 3.8+ (`urllib` only)**, read one
JSON object from stdin and write exactly one JSON object to stdout, following
the mesh shell contract used by `dsh-federation >= 0.8.0`. Handled failures are
returned as `{"ok": false, "error": ...}` with exit code 0, so the mesh gets a
structured result instead of an opaque `CAPABILITY_FAILED`.

## Install

```bash
fedcap validate packs/firecrawl
fedcap install --pack packs/firecrawl \
  --node-config /opt/fedcap/node.json \
  --set baseUrl=http://127.0.0.1:3002
# with a token gateway in front of Firecrawl:
fedcap install --pack packs/firecrawl \
  --node-config /opt/fedcap/node.json \
  --set baseUrl=http://127.0.0.1:3003 \
  --set tokenFile=/opt/fedcap/firecrawl-gateway.token
```

`fedcap` resolves `{{config.*}}` placeholders and writes
`capabilities.d/firecrawl.json` next to the node config, registering it via
`capabilitiesFile`. The token is **never** written into the manifest or the
descriptor — only the path to the token file is.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `baseUrl` | `http://127.0.0.1:3002` | Firecrawl API base URL, no trailing slash. Use the gateway URL when deploying `deploy/`. |
| `tokenFile` | `""` (empty) | Path to a file with a bearer token (0600). Empty = no `Authorization` header (loopback-only deployments). Re-read on every request, so rotation needs no restart. |
| `maxMarkdownBytes` | `65536` | Byte cap for `firecrawl.scrape` markdown. Larger documents are truncated and flagged `truncated: true`. |

Environment variables used by the executors (set from the manifest):
`FIRECRAWL_BASE_URL`, `FIRECRAWL_TOKEN_FILE`, `FIRECRAWL_MAX_MARKDOWN_BYTES`.
The executors connect **directly** to `baseUrl` and deliberately ignore ambient
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` values, so a host-wide proxy cannot
silently reroute loopback or Tailnet traffic. Point `baseUrl` at an explicit
gateway if a proxy hop is really intended.

## Deployment modes

### A. Direct loopback (simplest)

Firecrawl listens on `127.0.0.1:3002` and the pack runs on the same host. No
token is needed, because only that host's user can reach the port:

```bash
fedcap install --pack packs/firecrawl --node-config /opt/fedcap/node.json
```

If Firecrawl currently binds `0.0.0.0`, fix that first — see
[`deploy/README.md`](deploy/README.md#step-1--rebind-the-firecrawl-api-port-to-loopback-first).

### B. Token gateway (recommended whenever remote nodes call it)

```
remote mesh node → gateway.py :3003 (bearer auth) → Firecrawl :3002 (loopback)
```

The pack then uses `baseUrl = http://127.0.0.1:3003` and
`tokenFile = <same token file the gateway reads>`. The full template
(`gateway.py`, systemd `--user` unit, `setup.sh`, `rollback.sh`) lives in
[`deploy/`](deploy/README.md).

## Output examples

```json
// firecrawl.scrape (truncated for brevity)
{"ok": true, "url": "https://example.invalid/", "markdown": "# Example\n\n...",
 "markdownBytes": 42, "truncated": false, "title": "Example",
 "sourceURL": "https://example.invalid/", "statusCode": null, "upstreamStatus": 200}

// firecrawl.search
{"ok": true, "query": "example", "count": 2, "results": [
  {"title": "Example", "url": "https://example.invalid/", "description": "...", "engine": ""}],
 "upstreamStatus": 200}

// handled failure (upstream down, bad input, missing token file, ...)
{"ok": false, "error": "upstream_unreachable", "detail": "Connection refused"}
{"ok": false, "error": "invalid_input", "detail": "url is required"}
{"ok": false, "error": "token_unavailable", "detail": "cannot read token file (FileNotFoundError)"}
```

## Self-check

Offline (no Firecrawl needed):

```bash
python3 -m py_compile bin/firecrawl_scrape.py bin/firecrawl_search.py deploy/gateway.py
echo '{}' | python3 bin/firecrawl_scrape.py      # → {"ok": false, "error": "invalid_input", ...}
```

Online, against a running instance (substitute your own URL; nothing here needs
this repository to know a real host):

```bash
export FIRECRAWL_BASE_URL=http://127.0.0.1:3002
export FIRECRAWL_TOKEN_FILE=            # or a 0600 file when using the gateway

echo '{"url":"https://example.invalid","onlyMainContent":true}' \
  | python3 bin/firecrawl_scrape.py

echo '{"query":"capability pack","limit":3}' \
  | python3 bin/firecrawl_search.py
```

The manifest also declares these as `checks` (`fedcap validate` runs them when
`--python` is available).

## Security and limits

- **Server-side request forgery**: Firecrawl fetches the URL *on the server*, so
  callers can reach `127.0.0.1`, private ranges and other services on the same
  network. Give the capability only to trusted nodes and keep the Firecrawl port
  on loopback (and, ideally, add an egress policy to the container host). The
  optional gateway is authentication, not URL filtering.
- Keep the token file `0600` and outside version control. The pack never prints
  or forwards it beyond the configured Firecrawl URL.
- Responses are bounded: scrape reads at most 16 MiB from upstream before
  returning `upstream_error`, search at most 8 MiB, and both honour
  `maxMarkdownBytes` / `limit`.
- `firecrawl.search` quality and engines depend on the upstream instance (the
  reference deployment wires SearXNG into Firecrawl; see the searxng-search pack
  for a direct search capability). If the upstream search aggregation is
  rate-limited, `/v2/search` can exceed the 80 s client budget and the
  capability returns `{"ok": false, "error": "upstream_error", "detail":
  "timed out"}`; the direct searxng-search pack avoids that extra hop.
- Only the Firecrawl v2 endpoints `/v2/scrape` and `/v2/search` are exposed.
  Crawl/map/extract are intentionally out of scope in v0.1.0 (crawl is a
  long-running job and would need its own lifecycle contract).
