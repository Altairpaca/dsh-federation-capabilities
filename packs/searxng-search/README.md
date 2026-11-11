# searxng-search

A [Capability Pack v1](../../spec/capability-pack-v1.md) that turns a
**self-hosted SearXNG** instance into one mesh capability:

| Capability | Kind | Timeout | Input | Output |
| --- | --- | --- | --- | --- |
| `web.search` | shell | 45 s | `{query, limit?, language?}` (limit 1–10) | `{ok, query, count, results:[{title,url,content,engine,score}], unresponsiveEngines, upstreamStatus}` |

The executor is **zero-dependency Python 3.8+ (`urllib` only)**: one JSON object
on stdin, exactly one JSON object on stdout, diagnostics on stderr. Handled
failures (unreachable instance, HTTP errors, malformed JSON) are returned as
`{"ok": false, "error": ...}` with exit code 0, so the mesh gets a structured
result instead of an opaque `CAPABILITY_FAILED`.

## Install

```bash
fedcap validate packs/searxng-search
fedcap install --pack packs/searxng-search \
  --node-config /opt/fedcap/node.json \
  --set baseUrl=http://127.0.0.1:8080
```

Add `--set engines=brave,bing,duckduckgo` to restrict engines, and
`--set language=en` (or `zh-CN`, ...) to set the default language.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `baseUrl` | `http://127.0.0.1:8080` | SearXNG base URL, no trailing slash. |
| `engines` | `""` (empty) | Comma-separated engine names; empty = instance default. |
| `language` | `""` (empty) | Default language code; empty = instance default. A per-call `language` argument overrides it. |

Environment variables used by the executor (set from the manifest):
`SEARXNG_BASE_URL`, `SEARXNG_ENGINES`, `SEARXNG_LANGUAGE`.
The executor connects **directly** to `baseUrl` and deliberately ignores ambient
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` values, so a host-wide proxy cannot
silently reroute loopback or Tailnet traffic.

## Instance requirement: JSON output

SearXNG only serves the `format=json` responses used here when the JSON format is
enabled. In `settings.yml`:

```yaml
search:
  formats:
    - html
    - json
```

Then restart SearXNG. A quick check:

```bash
curl -s 'http://127.0.0.1:8080/search?q=capability+pack&format=json' | head -c 200
```

If JSON is disabled the capability returns
`{"ok": false, "error": "upstream_forbidden", ...}`.

## Output example

```json
{
  "ok": true,
  "query": "capability pack",
  "count": 2,
  "results": [
    {"title": "Example", "url": "https://example.invalid/", "content": "...",
     "engine": "duckduckgo", "score": 1.2}
  ],
  "unresponsiveEngines": [["brave", "timeout"]],
  "upstreamStatus": 200
}
```

Handled failures:

```json
{"ok": false, "error": "invalid_input", "detail": "query is required"}
{"ok": false, "error": "upstream_unreachable", "detail": "Connection refused"}
{"ok": false, "error": "upstream_http_error", "status": 429, "detail": "..."}
```

## Self-check

Offline:

```bash
python3 -m py_compile bin/searxng_search.py
echo '{}' | python3 bin/searxng_search.py     # → {"ok": false, "error": "invalid_input", ...}
```

Online, against a running instance (substitute your own URL):

```bash
SEARXNG_BASE_URL=http://127.0.0.1:8080 \
  sh -c 'echo "{\"query\":\"capability pack\",\"limit\":3}" | python3 bin/searxng_search.py'
```

The manifest declares the `py_compile` step as its `checks` entry
(`fedcap validate` runs it when `--python` is available).

## Security and limits

- SearXNG queries are forwarded as-is; the instance may aggregate third-party
  engines (brave, bing, duckduckgo, google, ...). Respect their rate limits and
  the instance's terms of use.
- Keep SearXNG loopback-only (or behind a Tailnet ACL). The pack adds no
  authentication; anyone who can call the capability can query the instance.
  The reference deployment puts the instance on loopback next to Firecrawl.
- Results are capped at 10 per call, `content` is truncated to 2000 characters,
  and the upstream response is read up to 4 MiB before returning
  `upstream_error`.
- Search relevance, availability and `unresponsiveEngines` are properties of the
  upstream instance, not of the pack.
