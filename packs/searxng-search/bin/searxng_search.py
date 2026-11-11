#!/usr/bin/env python3
"""web.search - run a web search through a self-hosted SearXNG instance.

Mesh shell capability contract:
  stdin  : exactly one JSON object, e.g. {"query": "capability pack", "limit": 5}
  stdout : exactly one JSON object, either {"ok": true, ...} or {"ok": false, "error": "..."}
  stderr : diagnostics only

Zero dependencies (Python standard library only). The instance must allow the
JSON output format; see the pack README for the one-line SearXNG setting.
Handled failures exit 0 with {"ok": false, ...} so the mesh receives structured
results instead of CAPABILITY_FAILED.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_LIMIT = 5
MAX_LIMIT = 10
HARD_RESPONSE_LIMIT = 4 * 1024 * 1024
UPSTREAM_TIMEOUT_SECONDS = 35  # below the 45000 ms capability timeout
MAX_CONTENT_CHARS = 2000
MAX_QUERY_CHARS = 512


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(error, **extra):
    payload = {"ok": False, "error": error}
    payload.update(extra)
    emit(payload)
    return 0


def read_stdin_args():
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("arguments must be a JSON object")
    return parsed


def clamp_limit(value):
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    return max(1, min(MAX_LIMIT, value))


def build_url(base_url, query, language, engines):
    params = {"q": query, "format": "json"}
    if language:
        params["language"] = language
    if engines:
        params["engines"] = engines
    return base_url + "/search?" + urllib.parse.urlencode(params)


def urlopen_direct(request, timeout):
    """Open a URL without consulting HTTP(S)_PROXY/ALL_PROXY environment values.

    SearXNG is normally loopback or a private/Tailnet address, so an ambient
    proxy would either fail (502) or send host-local traffic through an
    unrelated exit. Front the instance explicitly if a proxy is really needed.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def fetch_json(url):
    headers = {
        "Accept": "application/json",
        "User-Agent": "fedcap-searxng-search/" + VERSION,
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urlopen_direct(request, UPSTREAM_TIMEOUT_SECONDS) as response:
        status = response.status
        raw = response.read(HARD_RESPONSE_LIMIT + 1)
    if len(raw) > HARD_RESPONSE_LIMIT:
        raise RuntimeError("upstream response is larger than %d bytes" % HARD_RESPONSE_LIMIT)
    return status, json.loads(raw.decode("utf-8", "replace"))


def normalize_results(data, limit):
    raw_results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        return []
    results = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        content = item.get("content")
        if not isinstance(content, str):
            content = ""
        engine = item.get("engine")
        if not isinstance(engine, str):
            engines = item.get("engines")
            if isinstance(engines, list):
                engine = ",".join(str(entry) for entry in engines)
            else:
                engine = ""
        results.append(
            {
                "title": item.get("title") if isinstance(item.get("title"), str) else "",
                "url": url,
                "content": content[:MAX_CONTENT_CHARS],
                "engine": engine,
                "score": item.get("score") if isinstance(item.get("score"), (int, float)) else None,
            }
        )
        if len(results) >= limit:
            break
    return results


def unresponsive_engines(data):
    value = data.get("unresponsive_engines") if isinstance(data, dict) else None
    if not isinstance(value, list):
        return []
    output = []
    for entry in value[:10]:
        if isinstance(entry, (list, tuple)) and entry:
            output.append([str(entry[0])[:64], str(entry[1])[:160] if len(entry) > 1 else ""])
        elif isinstance(entry, str):
            output.append([entry[:64], ""])
    return output


def main():
    try:
        args = read_stdin_args()
    except (ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_input", detail="cannot parse stdin JSON: %s" % exc)

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return fail("invalid_input", detail="query is required")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        return fail("invalid_input", detail="query is longer than %d characters" % MAX_QUERY_CHARS)

    try:
        limit = clamp_limit(args.get("limit"))
    except ValueError as exc:
        return fail("invalid_input", detail=str(exc))

    base_url = os.environ.get("SEARXNG_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    engines = os.environ.get("SEARXNG_ENGINES", "").strip()
    default_language = os.environ.get("SEARXNG_LANGUAGE", "").strip()

    language = args.get("language")
    if language is None:
        language = default_language
    if not isinstance(language, str):
        return fail("invalid_input", detail="language must be a string")
    language = language.strip()[:32]

    url = build_url(base_url, query, language, engines)
    try:
        upstream_status, data = fetch_json(url)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(1024).decode("utf-8", "replace").strip()[:400]
        except Exception:  # noqa: BLE001 - diagnostics must never mask the error
            detail = ""
        if exc.code == 403:
            return fail(
                "upstream_forbidden",
                status=exc.code,
                detail=detail or "the instance may not allow the json output format",
            )
        return fail("upstream_http_error", status=exc.code, detail=detail)
    except urllib.error.URLError as exc:
        return fail("upstream_unreachable", detail=str(exc.reason)[:500])
    except (RuntimeError, ValueError, OSError) as exc:
        return fail("upstream_error", detail=str(exc)[:500])

    if not isinstance(data, dict):
        return fail("upstream_error", detail="unexpected upstream response shape")

    results = normalize_results(data, limit)
    emit(
        {
            "ok": True,
            "query": query,
            "count": len(results),
            "results": results,
            "unresponsiveEngines": unresponsive_engines(data),
            "upstreamStatus": upstream_status,
        }
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # keep the shell contract even on unexpected bugs
        emit({"ok": False, "error": "internal_error", "detail": str(exc)[:500]})
        sys.exit(0)
