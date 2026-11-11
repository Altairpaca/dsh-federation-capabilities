#!/usr/bin/env python3
"""web.fetch executor for the network-probe capability pack.

Contract (mesh shell capability):
  stdin  : one JSON object with the invocation args (``url``, optional ``maxBytes``)
  stdout : exactly one JSON object
  stderr : human diagnostics only
  exit   : 0 after a report was produced (HTTP/network failures are reported as
           ``ok:false`` JSON); 1 only when the request could not be answered

SSRF guard:
  * only http(s), only ports 80/443;
  * no URL userinfo;
  * the host must be a globally routable address or a name that resolves only
    to globally routable addresses (private, loopback, link-local, CGNAT,
    reserved and special-use names are refused);
  * every redirect hop is validated the same way.

Environment (resolved by ``fedcap install`` from ``fedcap.json``):
  NETWORK_PROBE_PROXY      optional http(s) proxy URL, empty = direct
  NETWORK_PROBE_MAX_BYTES  stdout size bound, default 262144
"""

import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROXY_ENV = "NETWORK_PROBE_PROXY"
MAX_BYTES_ENV = "NETWORK_PROBE_MAX_BYTES"
DEFAULT_MAX_BYTES = 262144
MIN_MAX_BYTES = 1024
MAX_MAX_BYTES = 1048576
MIN_CALL_BYTES = 512
REQUEST_TIMEOUT_S = 20
MAX_REDIRECTS = 5
USER_AGENT = "fedcap-network-probe/0.1"

ALLOWED_PORTS = (80, 443)
BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".lan",
    ".test",
    ".invalid",
    ".example",
    ".onion",
)
TEXTUAL_CONTENT_TYPES = (
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/x-www-form-urlencoded",
)
CHARSET_RE = re.compile(r"charset\s*=\s*\"?([^;\"]+)", re.IGNORECASE)


class InvalidArgument(Exception):
    """The caller supplied an unusable argument."""

    code = "INVALID_ARGUMENT"


class BlockedTarget(Exception):
    """The URL points at (or redirects to) a non-public destination."""

    code = "TARGET_BLOCKED"


def _env_text(name):
    raw = os.environ.get(name)
    return "" if raw is None else str(raw).strip()


def _env_int(name, default):
    raw = _env_text(name)
    value = default
    if raw != "":
        try:
            value = int(raw, 10)
        except (TypeError, ValueError):
            value = default
    return max(MIN_MAX_BYTES, min(MAX_MAX_BYTES, value))


def _parse_args(raw):
    if raw.strip() == "":
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise InvalidArgument("args must be a JSON object")
    return value


def _elapsed_ms(started):
    return int(round((time.monotonic() - started) * 1000))


def _ip_allowed(address):
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(address.is_global)


def _blocked_reason(host, port):
    """Return a stable code when ``host`` must not be contacted, else None."""
    if host is None or host == "":
        return "missing-host"
    value = host.strip().rstrip(".").lower()
    if value == "localhost" or value.endswith(BLOCKED_HOST_SUFFIXES):
        return "special-use-name"
    literal = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        address = None
    if address is not None:
        return None if _ip_allowed(address) else "non-public-address"
    try:
        infos = socket.getaddrinfo(value, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return "dns-error"
    if not infos:
        return "dns-error"
    for info in infos:
        resolved = str(info[4][0]).split("%")[0]
        try:
            address = ipaddress.ip_address(resolved)
        except ValueError:
            return "dns-error"
        if not _ip_allowed(address):
            return "non-public-address"
    return None


def _validate_url(raw):
    """Validate one URL and return ``(normalized_url, host, port)``."""
    if not isinstance(raw, str):
        raise InvalidArgument("url must be a string")
    text = raw.strip()
    if text == "":
        raise InvalidArgument("url must not be empty")
    if len(text) > 4096:
        raise InvalidArgument("url is longer than 4096 characters")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        raise InvalidArgument("url is not a valid absolute URL") from None
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise InvalidArgument("url must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidArgument("url must not carry userinfo")
    if parsed.hostname is None or parsed.hostname == "":
        raise InvalidArgument("url must contain a host")
    try:
        port = parsed.port
    except ValueError:
        raise InvalidArgument("url contains an invalid port") from None
    if port is not None and port not in ALLOWED_PORTS:
        raise InvalidArgument("only ports 80 and 443 are allowed")
    default_port = 443 if scheme == "https" else 80
    socket_port = port if port is not None else default_port
    reason = _blocked_reason(parsed.hostname, socket_port)
    if reason is not None:
        raise BlockedTarget(reason)
    normalized = parsed._replace(fragment="").geturl()
    return normalized, parsed.hostname, socket_port


def _charset_of(content_type):
    match = CHARSET_RE.search(content_type or "")
    if match is None:
        return None
    return match.group(1).strip().strip("'\"").lower() or None


def _is_textual(content_type):
    value = (content_type or "").split(";", 1)[0].strip().lower()
    if value == "":
        return True
    if value.startswith("text/"):
        return True
    if value in TEXTUAL_CONTENT_TYPES:
        return True
    return value.endswith("+json") or value.endswith("+xml")


def _decode(body, charset):
    candidates = []
    if charset:
        candidates.append(charset)
    candidates.extend(["utf-8", "latin-1"])
    for candidate in candidates:
        try:
            return body.decode(candidate)
        except (UnicodeDecodeError, LookupError, TypeError):
            continue
    return body.decode("utf-8", "replace")


def _error_code(error):
    if isinstance(error, InvalidArgument):
        return error.code
    if isinstance(error, BlockedTarget):
        return error.code
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "TIMEOUT"
    if isinstance(error, socket.gaierror):
        return "DNS_ERROR"
    if isinstance(error, ssl.SSLError):
        return "TLS_ERROR"
    if isinstance(error, urllib.error.HTTPError):
        return "HTTP_ERROR"
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "TIMEOUT"
        if isinstance(reason, socket.gaierror):
            return "DNS_ERROR"
        if isinstance(reason, ssl.SSLError):
            return "TLS_ERROR"
        return "TRANSPORT"
    return "TRANSPORT"


def _error_message(error):
    code = _error_code(error)
    if code == "TIMEOUT":
        return "request timed out"
    if code == "DNS_ERROR":
        return "the host name could not be resolved"
    if code == "TLS_ERROR":
        return "TLS handshake failed"
    if code == "HTTP_ERROR":
        return "the upstream answered with an error status"
    if code == "TARGET_BLOCKED":
        return "the target is not a public http(s) endpoint"
    if code == "INVALID_ARGUMENT":
        return str(error) or "invalid argument"
    return "transport failure"


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only after validating the new URL with the SSRF guard."""

    def __init__(self):
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        self.hops += 1
        if self.hops > MAX_REDIRECTS:
            raise BlockedTarget("too many redirects")
        try:
            _validate_url(newurl)
        except (InvalidArgument, BlockedTarget) as error:
            raise BlockedTarget("redirect to a non-public or invalid URL: %s" % error) from None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(proxy_url, redirect_handler):
    handlers = [redirect_handler]
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    else:
        # Ambient proxy variables are intentionally ignored; the mesh host
        # passes a minimal environment and results must follow the pack config.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _read_body(response, limit):
    try:
        return response.read(limit + 1)
    except (socket.timeout, TimeoutError):
        raise
    except Exception:  # noqa: BLE001 - body read failed, report what we have
        return b""


def _fetch(url, limit, proxy_url):
    redirects = _ValidatingRedirectHandler()
    opener = _build_opener(proxy_url, redirects)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/*, application/json;q=0.9, */*;q=0.5",
            "Accept-Encoding": "identity",
        },
    )
    started = time.monotonic()
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            status = int(getattr(response, "status", 200))
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "") or ""
            body = _read_body(response, limit)
    except urllib.error.HTTPError as error:
        status = int(error.code)
        final_url = error.geturl()
        content_type = error.headers.get("Content-Type", "") if error.headers is not None else ""
        body = _read_body(error, limit)
    except Exception as error:  # noqa: BLE001 - mapped to a stable code below
        return {
            "ok": False,
            "kind": "web.fetch",
            "url": url,
            "error": {"code": _error_code(error), "message": _error_message(error)},
            "elapsedMs": _elapsed_ms(started),
        }
    truncated = len(body) > limit
    payload = body[:limit]
    textual = _is_textual(content_type)
    text = _decode(payload, _charset_of(content_type)) if textual else None
    ok = 200 <= status < 300
    report = {
        "ok": ok,
        "kind": "web.fetch",
        "url": url,
        "finalUrl": final_url,
        "status": status,
        "contentType": content_type,
        "bytes": len(payload),
        "truncated": truncated,
        "redirects": redirects.hops,
        "viaProxy": bool(proxy_url),
        "elapsedMs": _elapsed_ms(started),
    }
    if text is None:
        report["contentOmitted"] = "binary-or-unknown-content-type"
    else:
        report["content"] = text
    if not ok:
        report["error"] = {"code": "HTTP_ERROR", "message": "the upstream answered with status %d" % status}
    return report


def _write_json(text):
    """Write exactly one UTF-8 JSON line without platform newline translation."""
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _emit_fitted(report, budget):
    """Serialize ``report`` as one JSON line, shrinking ``content`` to fit."""
    text = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) + 1 <= budget:
        _write_json(text)
        return
    content = report.get("content")
    if isinstance(content, str):
        encoded = content.encode("utf-8")
        low, high = 0, len(encoded)
        best = None
        while low <= high:
            middle = (low + high) // 2
            candidate = dict(report)
            candidate["content"] = encoded[:middle].decode("utf-8", "ignore")
            candidate["truncated"] = True
            candidate_text = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            if len(candidate_text.encode("utf-8")) + 1 <= budget:
                best = candidate_text
                low = middle + 1
            else:
                high = middle - 1
        if best is not None:
            _write_json(best)
            return
    minimal = {
        "ok": report.get("ok"),
        "kind": "web.fetch",
        "url": report.get("url"),
        "status": report.get("status"),
        "truncated": True,
        "error": {
            "code": "OUTPUT_TOO_LARGE",
            "message": "the response exceeds maxBytes; raise the config value or request fewer bytes",
        },
    }
    _write_json(json.dumps(minimal, ensure_ascii=False, separators=(",", ":")))


def _self_test():
    """Offline checks (no network). Addresses are constructed from integers so
    the public repository never contains a literal IP address."""
    failures = []

    def check(name, condition):
        if not condition:
            failures.append(name)

    def rejected(url):
        try:
            _validate_url(url)
        except (InvalidArgument, BlockedTarget):
            return True
        return False

    loopback_v4 = ipaddress.IPv4Address((127 << 24) | 1)
    private_v4 = ipaddress.IPv4Address((10 << 24) | (1 << 16) | 1)
    cgnat_v4 = ipaddress.IPv4Address((100 << 24) | (64 << 16) | 1)
    global_v4 = ipaddress.IPv4Address((1 << 24) | (1 << 8) | 1)

    check("empty args parse", _parse_args("") == {})
    check("json args parse", _parse_args('{"url":"x"}') == {"url": "x"})
    check("public literal accepted", rejected("http://%s/path" % global_v4) is False)
    check("loopback literal refused", rejected("http://127.0.0.1/") is True)
    check("ipv6 loopback refused", rejected("http://[::1]/") is True)
    check("localhost name refused", rejected("http://localhost/") is True)
    check("invalid name refused", rejected("http://example.invalid/") is True)
    check("private literal refused", rejected("http://%s/" % private_v4) is True)
    check("cgnat literal refused", rejected("http://%s/" % cgnat_v4) is True)
    check("non-http scheme refused", rejected("ftp://example.invalid/") is True)
    check("non-standard port refused", rejected("http://%s:8080/" % global_v4) is True)
    check("userinfo refused", rejected("http://user:secret@%s/" % global_v4) is True)
    normalized, host, port = _validate_url("https://%s/a?b=1#frag" % global_v4)
    check("fragment stripped", "#" not in normalized)
    check("default https port", port == 443 and host == str(global_v4))
    check("charset parsing", _charset_of('text/html; charset="UTF-8"') == "utf-8")
    check("json is textual", _is_textual("application/json") is True)
    check("png is not textual", _is_textual("image/png") is False)
    check("latin-1 fallback", _decode(b"caf\xe9", None) == "café")
    try:
        _ValidatingRedirectHandler().redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/x")
        check("redirect to loopback refused", False)
    except BlockedTarget:
        check("redirect to loopback refused", True)
    result = {"ok": not failures, "kind": "web.fetch.self-test", "failures": failures}
    _write_json(json.dumps(result, separators=(",", ":")))
    return 0 if not failures else 1


def main(argv):
    if "--self-test" in argv:
        return _self_test()
    budget = _env_int(MAX_BYTES_ENV, DEFAULT_MAX_BYTES)
    limit = budget
    try:
        args = _parse_args(sys.stdin.read())
        if args.get("maxBytes") is not None:
            raw_limit = args.get("maxBytes")
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
                raise InvalidArgument("maxBytes must be an integer")
            if raw_limit < MIN_CALL_BYTES or raw_limit > MAX_MAX_BYTES:
                raise InvalidArgument("maxBytes must be between %d and %d" % (MIN_CALL_BYTES, MAX_MAX_BYTES))
            limit = min(budget, raw_limit)
        if "url" not in args:
            raise InvalidArgument("url is required")
        normalized, _host, _port = _validate_url(args.get("url"))
        report = _fetch(normalized, limit, _env_text(PROXY_ENV))
    except (InvalidArgument, BlockedTarget) as error:
        report = {
            "ok": False,
            "kind": "web.fetch",
            "error": {"code": _error_code(error), "message": _error_message(error)},
        }
    except (ValueError, TypeError):
        # Malformed stdin JSON is a caller error, not an internal failure.
        report = {
            "ok": False,
            "kind": "web.fetch",
            "error": {"code": "INVALID_ARGUMENT", "message": "args must be a JSON object"},
        }
    except Exception as error:  # noqa: BLE001 - never leak internals
        report = {
            "ok": False,
            "kind": "web.fetch",
            "error": {"code": "INTERNAL", "message": "fetch failed internally", "detail": type(error).__name__},
        }
    _emit_fitted(report, min(limit, budget))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
