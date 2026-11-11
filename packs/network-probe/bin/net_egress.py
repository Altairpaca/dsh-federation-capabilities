#!/usr/bin/env python3
"""net.egress executor for the network-probe capability pack.

Contract (mesh shell capability):
  stdin  : one JSON object with the invocation args (unused by this capability)
  stdout : exactly one JSON object
  stderr : human diagnostics only
  exit   : 0 after a report was produced (even when every probe failed);
           1 only when the request could not be answered at all

The report never contains local interface addresses, the configured proxy URL
or resolved DNS addresses. Only the public egress address reported by an
external echo endpoint is included, and a non-global address is withheld.

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
import urllib.request

PROXY_ENV = "NETWORK_PROBE_PROXY"
MAX_BYTES_ENV = "NETWORK_PROBE_MAX_BYTES"
DEFAULT_MAX_BYTES = 262144
MIN_MAX_BYTES = 1024
MAX_MAX_BYTES = 1048576
REQUEST_TIMEOUT_S = 6
OVERALL_BUDGET_S = 22
USER_AGENT = "fedcap-network-probe/0.1"

# Public IP echo endpoints. They answer with the address they observed for the
# connection; no other data is sent to them. The first success wins.
ECHO_TARGETS = (
    ("api.ipify.org", "https://api.ipify.org?format=json", "json"),
    ("ifconfig.me", "https://ifconfig.me/ip", "text"),
    ("icanhazip.com", "https://icanhazip.com", "text"),
)

# Public names used for a resolution check only; no address is reported.
DNS_TARGETS = ("api.ipify.org", "ifconfig.me")


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


def _elapsed_ms(started):
    return int(round((time.monotonic() - started) * 1000))


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_args(raw):
    if raw.strip() == "":
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("args must be a JSON object")
    return value


def _read_args():
    return _parse_args(sys.stdin.read())


def _ip_is_public(text):
    candidate = str(text).strip().strip("[]").strip()
    if candidate == "":
        return False
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(address.is_global)


def _extract_ip(payload, kind):
    """Return the first globally routable IP in an echo response, else None.

    A non-global address is deliberately not returned: the report must not
    expose local or private addresses a misbehaving endpoint might echo back.
    """
    text = payload
    if kind == "json":
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key in ("ip", "address", "origin"):
                value = data.get(key)
                if isinstance(value, str) and _ip_is_public(value):
                    return value.strip().strip("[]")
    for token in re.split(r"\s+", text):
        cleaned = token.strip().strip("[]()<>,;\"'")
        if cleaned == "":
            continue
        if _ip_is_public(cleaned):
            return cleaned
    return None


def _error_name(error):
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


def _build_opener(proxy_url):
    handlers = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    else:
        # Ignore ambient proxy variables on purpose: the mesh host passes a
        # minimal environment, and probe results must follow the pack config.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _probe_echo(target, url, kind, opener, timeout_s):
    started = time.monotonic()
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain;q=0.9"},
    )
    try:
        with opener.open(request, timeout=timeout_s) as response:
            status = int(getattr(response, "status", 200))
            body = response.read(4096)
    except Exception as error:  # noqa: BLE001 - mapped to a stable code below
        return {
            "ok": False,
            "target": target,
            "latencyMs": _elapsed_ms(started),
            "error": _error_name(error),
        }
    text = body.decode("utf-8", "replace")
    address = _extract_ip(text, kind)
    attempt = {"ok": address is not None, "target": target, "status": status, "latencyMs": _elapsed_ms(started)}
    if address is not None:
        attempt["ip"] = address
    else:
        attempt["error"] = "NO_PUBLIC_ADDRESS"
    return attempt


def _probe_mode(proxy_url, deadline):
    opener = _build_opener(proxy_url)
    attempts = []
    for target, url, kind in ECHO_TARGETS:
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            attempts.append({"ok": False, "target": target, "error": "DEADLINE"})
            break
        attempt = _probe_echo(target, url, kind, opener, min(REQUEST_TIMEOUT_S, remaining))
        attempts.append(attempt)
        if attempt.get("ok") is True:
            break
    success = None
    for attempt in attempts:
        if attempt.get("ok") is True:
            success = attempt
            break
    mode = {"ok": success is not None, "attempts": attempts}
    if success is not None:
        mode["ip"] = success["ip"]
        mode["target"] = success["target"]
        mode["latencyMs"] = success["latencyMs"]
    return mode


def _probe_dns(hosts, deadline):
    started = time.monotonic()
    records = []
    for host in hosts:
        if deadline - time.monotonic() <= 1:
            records.append({"host": host, "ok": False, "error": "DEADLINE"})
            continue
        host_started = time.monotonic()
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            records.append(
                {
                    "host": host,
                    "ok": len(infos) > 0,
                    "addresses": len(infos),
                    "latencyMs": _elapsed_ms(host_started),
                }
            )
        except Exception as error:  # noqa: BLE001
            records.append(
                {
                    "host": host,
                    "ok": False,
                    "error": _error_name(error),
                    "latencyMs": _elapsed_ms(host_started),
                }
            )
    return {
        "ok": any(record.get("ok") is True for record in records),
        "hosts": records,
        "latencyMs": _elapsed_ms(started),
    }


def _build_report():
    started = time.monotonic()
    deadline = started + OVERALL_BUDGET_S
    proxy_url = _env_text(PROXY_ENV)
    proxy_configured = proxy_url != ""
    dns = _probe_dns(DNS_TARGETS, deadline)
    # A configured proxy is probed first: on networks where direct egress is
    # blocked, it is the path that is expected to work.
    if proxy_configured:
        via_proxy = _probe_mode(proxy_url, deadline)
        via_proxy["configured"] = True
        direct = _probe_mode("", deadline)
        report = {
            "ok": bool(direct.get("ok") or via_proxy.get("ok")),
            "kind": "net.egress",
            "checkedAt": _utc_now(),
            "proxyConfigured": True,
            "dns": dns,
            "direct": direct,
            "viaProxy": via_proxy,
        }
    else:
        direct = _probe_mode("", deadline)
        report = {
            "ok": bool(direct.get("ok")),
            "kind": "net.egress",
            "checkedAt": _utc_now(),
            "proxyConfigured": False,
            "dns": dns,
            "direct": direct,
            "viaProxy": {"configured": False, "skipped": True},
        }
    report["durationMs"] = _elapsed_ms(started)
    if report["ok"] is False:
        report["error"] = {
            "code": "EGRESS_UNREACHABLE",
            "message": "no public egress probe succeeded; check outbound access or the proxy setting",
        }
    return report


def _write_json(text):
    """Write exactly one UTF-8 JSON line without platform newline translation."""
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _fits(value, max_bytes):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) + 1 <= max_bytes


def _shrink(report, max_bytes):
    """Drop diagnostic detail until the serialized report fits the byte cap."""
    for key in ("direct", "viaProxy"):
        mode = report.get(key)
        if isinstance(mode, dict) and isinstance(mode.get("attempts"), list):
            mode["attempts"] = mode["attempts"][:1]
    if _fits(report, max_bytes):
        return report
    dns = report.get("dns")
    if isinstance(dns, dict) and isinstance(dns.get("hosts"), list):
        dns["hosts"] = dns["hosts"][:1]
    if _fits(report, max_bytes):
        return report
    minimal = {
        "ok": report.get("ok"),
        "kind": "net.egress",
        "truncated": True,
        "error": {
            "code": "OUTPUT_TOO_LARGE",
            "message": "report exceeded NETWORK_PROBE_MAX_BYTES; raise the config value",
        },
    }
    direct = report.get("direct")
    if isinstance(direct, dict) and "ip" in direct:
        minimal["direct"] = {"ok": direct.get("ok"), "ip": direct.get("ip")}
    via_proxy = report.get("viaProxy")
    if isinstance(via_proxy, dict) and "ip" in via_proxy:
        minimal["viaProxy"] = {"configured": True, "ok": via_proxy.get("ok"), "ip": via_proxy.get("ip")}
    return minimal


def _emit(report, max_bytes):
    _write_json(json.dumps(_shrink(report, max_bytes), ensure_ascii=False, separators=(",", ":")))


def _self_test():
    """Offline checks (no network). Addresses are constructed from integers so
    the public repository never contains a literal IP address."""
    failures = []

    def check(name, condition):
        if not condition:
            failures.append(name)

    def _args_rejected(raw):
        try:
            _parse_args(raw)
        except ValueError:
            return True
        return False

    loopback_v4 = ipaddress.IPv4Address((127 << 24) | 1)
    private_v4 = ipaddress.IPv4Address((10 << 24) | (1 << 16) | 1)
    cgnat_v4 = ipaddress.IPv4Address((100 << 24) | (64 << 16) | 1)
    documentation_v4 = ipaddress.IPv4Address((192 << 24) | (2 << 8) | 1)
    global_v4 = ipaddress.IPv4Address((1 << 24) | (1 << 8) | 1)

    check("loopback is not public", _ip_is_public(str(loopback_v4)) is False)
    check("rfc1918 is not public", _ip_is_public(str(private_v4)) is False)
    check("cgnat is not public", _ip_is_public(str(cgnat_v4)) is False)
    check("documentation range is not public", _ip_is_public(str(documentation_v4)) is False)
    check("global address is public", _ip_is_public(str(global_v4)) is True)
    check(
        "json body extraction",
        _extract_ip('{"ip":"%s"}' % global_v4, "json") == str(global_v4),
    )
    check(
        "text body extraction",
        _extract_ip("your address: %s\n" % global_v4, "text") == str(global_v4),
    )
    check(
        "non-public echo is withheld",
        _extract_ip(str(documentation_v4), "text") is None,
    )
    check("empty args parse", _parse_args("") == {})
    check("json args parse", _parse_args('{"limit":1}') == {"limit": 1})
    check("non-object args rejected", _args_rejected("[]") is True)
    check("max bytes clamps low", _env_int(MAX_BYTES_ENV, 1) >= MIN_MAX_BYTES)
    check("max bytes clamps high", _env_int(MAX_BYTES_ENV, MAX_MAX_BYTES * 4) == MAX_MAX_BYTES)
    payload = {
        "ok": True,
        "kind": "net.egress",
        "direct": {"ok": True, "attempts": [{"ok": True}], "ip": str(global_v4)},
        "viaProxy": {"configured": False},
        "dns": {"ok": True, "hosts": [{"host": "api.ipify.org", "ok": True}]},
    }
    check("shrink keeps report", isinstance(_shrink(payload, 4096), dict))
    result = {"ok": not failures, "kind": "net.egress.self-test", "failures": failures}
    _write_json(json.dumps(result, separators=(",", ":")))
    return 0 if not failures else 1


def main(argv):
    if "--self-test" in argv:
        return _self_test()
    max_bytes = _env_int(MAX_BYTES_ENV, DEFAULT_MAX_BYTES)
    try:
        _read_args()
    except (ValueError, TypeError):
        # Malformed stdin JSON is a caller error, not an internal failure.
        _emit(
            {
                "ok": False,
                "kind": "net.egress",
                "error": {"code": "INVALID_ARGUMENT", "message": "args must be a JSON object"},
            },
            max_bytes,
        )
        return 0
    try:
        report = _build_report()
    except Exception as error:  # noqa: BLE001 - never leak provider/proxy detail
        _emit(
            {
                "ok": False,
                "kind": "net.egress",
                "error": {"code": "INTERNAL", "message": "probe failed internally", "detail": type(error).__name__},
            },
            max_bytes,
        )
        return 0
    _emit(report, max_bytes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
