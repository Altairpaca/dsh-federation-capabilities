#!/usr/bin/env python3
"""firecrawl.scrape - scrape one URL through a self-hosted Firecrawl instance.

Mesh shell capability contract:
  stdin  : exactly one JSON object, e.g. {"url": "https://example.invalid", "onlyMainContent": true}
  stdout : exactly one JSON object, either {"ok": true, ...} or {"ok": false, "error": "..."}
  stderr : diagnostics only

Zero dependencies (Python standard library only). The optional bearer token is
read from the file named by FIRECRAWL_TOKEN_FILE on every invocation, so token
rotation does not require restarting the pack or the mesh node.
"""

import json
import os
import sys
import urllib.error
import urllib.request

VERSION = "0.1.0"
DEFAULT_BASE_URL = "http://127.0.0.1:3002"
DEFAULT_MAX_MARKDOWN_BYTES = 65536
HARD_RESPONSE_LIMIT = 16 * 1024 * 1024  # bytes read from upstream at most
UPSTREAM_TIMEOUT_SECONDS = 110  # below the 120000 ms capability timeout


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


def read_token(token_file):
    """Read the bearer token per request. Returns None when unauthenticated."""
    if not token_file:
        return None
    try:
        with open(token_file, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        raise RuntimeError("cannot read token file (%s)" % exc.__class__.__name__)
    if not token:
        raise RuntimeError("token file is empty")
    return token


def urlopen_direct(request, timeout):
    """Open a URL without consulting HTTP(S)_PROXY/ALL_PROXY environment values.

    The configured baseUrl is normally loopback or a private/Tailnet address, so
    an ambient proxy would either fail (502) or send host-local traffic through
    an unrelated exit. Operators who really need a proxy should front the API
    with an explicit endpoint instead of relying on environment variables.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def http_post_json(url, payload, token):
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "fedcap-firecrawl-scrape/" + VERSION,
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urlopen_direct(request, UPSTREAM_TIMEOUT_SECONDS) as response:
        status = response.status
        raw = response.read(HARD_RESPONSE_LIMIT + 1)
    if len(raw) > HARD_RESPONSE_LIMIT:
        raise RuntimeError("upstream response is larger than %d bytes" % HARD_RESPONSE_LIMIT)
    return status, json.loads(raw.decode("utf-8", "replace"))


def truncate_markdown(markdown, max_bytes):
    """Byte-accurate truncation that never splits a UTF-8 code point."""
    encoded = markdown.encode("utf-8")
    if len(encoded) <= max_bytes:
        return markdown, len(encoded), False
    return encoded[:max_bytes].decode("utf-8", "ignore"), max_bytes, True


def main():
    try:
        args = read_stdin_args()
    except (ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_input", detail="cannot parse stdin JSON: %s" % exc)

    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return fail("invalid_input", detail="url is required")
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return fail("invalid_input", detail="url must start with http:// or https://")

    only_main_content = args.get("onlyMainContent", True)
    if not isinstance(only_main_content, bool):
        return fail("invalid_input", detail="onlyMainContent must be a boolean")

    base_url = os.environ.get("FIRECRAWL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    token_file = os.environ.get("FIRECRAWL_TOKEN_FILE", "")
    raw_limit = os.environ.get("FIRECRAWL_MAX_MARKDOWN_BYTES", "")
    try:
        max_markdown_bytes = int(raw_limit) if raw_limit else DEFAULT_MAX_MARKDOWN_BYTES
    except ValueError:
        return fail("invalid_config", detail="FIRECRAWL_MAX_MARKDOWN_BYTES must be an integer")
    if max_markdown_bytes < 1:
        return fail("invalid_config", detail="FIRECRAWL_MAX_MARKDOWN_BYTES must be positive")

    try:
        token = read_token(token_file)
    except RuntimeError as exc:
        return fail("token_unavailable", detail=str(exc))

    payload = {"url": url, "formats": ["markdown"], "onlyMainContent": only_main_content}
    try:
        upstream_status, data = http_post_json(base_url + "/v2/scrape", payload, token)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(2048).decode("utf-8", "replace").strip()[:500]
        except Exception:  # noqa: BLE001 - diagnostics must never mask the error
            detail = ""
        return fail("upstream_http_error", status=exc.code, detail=detail)
    except urllib.error.URLError as exc:
        return fail("upstream_unreachable", detail=str(exc.reason)[:500])
    except (RuntimeError, ValueError, OSError) as exc:
        return fail("upstream_error", detail=str(exc)[:500])

    if not isinstance(data, dict):
        return fail("upstream_error", detail="unexpected upstream response shape")
    if data.get("success") is False:
        return fail("upstream_rejected", detail=str(data.get("error") or "success=false")[:500])

    document = data.get("data") if isinstance(data.get("data"), dict) else {}
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    markdown = document.get("markdown") if isinstance(document.get("markdown"), str) else ""
    text, size, truncated = truncate_markdown(markdown, max_markdown_bytes)

    emit(
        {
            "ok": True,
            "url": url,
            "markdown": text,
            "markdownBytes": size,
            "truncated": truncated,
            "title": metadata.get("title"),
            "sourceURL": metadata.get("sourceURL"),
            "statusCode": metadata.get("statusCode"),
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
