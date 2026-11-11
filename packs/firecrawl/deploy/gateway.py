#!/usr/bin/env python3
"""Fedcap Firecrawl token gateway - dependency-free bearer-auth reverse proxy.

Why this exists
---------------
A self-hosted Firecrawl API is normally unauthenticated: anything that can reach
its port can make the host crawl arbitrary URLs (including internal ones). This
gateway puts a single bearer-token check in front of the upstream API and only
forwards requests that authenticate. It is intentionally small, standard-library
only, and has no configuration file format of its own: everything comes from
environment variables so the systemd unit owns the deployment surface.

Environment variables
---------------------
GATEWAY_HOST              bind address                          (default 127.0.0.1)
GATEWAY_PORT              bind port                             (default 3003)
GATEWAY_UPSTREAM          upstream API base URL                 (default http://127.0.0.1:3002)
GATEWAY_TOKEN_FILE        file holding the bearer token         (required, 0600)
MAX_BODY_BYTES            max request body size                 (default 8388608)
UPSTREAM_TIMEOUT_SECONDS  upstream connect/read timeout         (default 120)

For convenience the shorter `UPSTREAM` and `TOKEN_FILE` names are also accepted
when the GATEWAY_* variables are absent, so the template works with either a
minimal env file or the reference deployment that uses the GATEWAY_* names.

Behaviour
---------
* GET  /__gw/health        anonymous liveness check:
                           {"ok": true, "gateway": "firecrawl", "version": "0.1.0"}
* everything else          requires `Authorization: Bearer <token>`
* the token file is re-read on every request, so rotating the token is just
  rewriting the file (no restart needed)
* the client token is never forwarded upstream; the upstream sees the token from
  the token file instead
* only a small response-header allowlist is forwarded; request cookies and
  Accept-Encoding are dropped
* logs never contain credentials or query strings
* every failure is fail-closed with a Firecrawl-style JSON body:
  {"success": false, "error": "..."}, plus HTTP 401 for missing/bad tokens and
  502/503/504 for upstream problems

Run directly for a quick test:
  GATEWAY_PORT=3003 GATEWAY_UPSTREAM=http://127.0.0.1:3002 \\
    GATEWAY_TOKEN_FILE=/opt/fedcap/firecrawl-gateway.token python3 gateway.py
"""

import hmac
import http.client
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

VERSION = "0.1.0"
SERVICE = "fedcap-firecrawl-gateway"
HEALTH_PATH = "/__gw/health"
FORWARD_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
RESPONSE_HEADER_ALLOWLIST = (
    "Content-Type",
    "Cache-Control",
    "ETag",
    "Last-Modified",
    "X-Response-Time",
)
BEARER_PREFIX = "Bearer "


def env_int(name, default):
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit("%s must be an integer" % name)


class Settings:
    def __init__(self):
        self.host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
        self.port = env_int("GATEWAY_PORT", 3003)
        upstream = (
            os.environ.get("GATEWAY_UPSTREAM")
            or os.environ.get("UPSTREAM")
            or "http://127.0.0.1:3002"
        )
        self.upstream = upstream.rstrip("/")
        self.token_file = os.environ.get("GATEWAY_TOKEN_FILE") or os.environ.get("TOKEN_FILE") or ""
        self.max_body_bytes = env_int("MAX_BODY_BYTES", 8 * 1024 * 1024)
        self.timeout = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "120"))
        parsed = urlsplit(self.upstream)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise SystemExit(
                "GATEWAY_UPSTREAM must be an http(s) URL with a host, e.g. http://127.0.0.1:3002"
            )
        self.upstream_scheme = parsed.scheme
        self.upstream_host = parsed.hostname
        self.upstream_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.upstream_prefix = parsed.path.rstrip("/")


class TokenUnavailable(Exception):
    pass


def read_token(path):
    if not path:
        raise TokenUnavailable("TOKEN_FILE is not set")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        raise TokenUnavailable("cannot read token file (%s)" % exc.__class__.__name__)
    if not token:
        raise TokenUnavailable("token file is empty")
    return token


def token_equals(presented, expected):
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = SERVICE + "/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.0"

    # --- plumbing ---------------------------------------------------------
    @property
    def settings(self):
        return self.server.settings

    def log_line(self, message):
        sys.stderr.write(
            "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), message)
        )
        sys.stderr.flush()

    def log_request(self, code="-", size="-"):
        # Never log the raw request line: it contains query strings.
        path = urlsplit(self.path).path
        self.log_line('"%s %s" %s %s' % (self.command, path, code, size))

    def write_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # --- request handling -------------------------------------------------
    def do_GET(self):
        self.handle_request()

    def do_HEAD(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def do_PATCH(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()

    def do_OPTIONS(self):
        self.write_json(405, {"success": False, "error": "method_not_allowed"})

    def handle_request(self):
        split = urlsplit(self.path)
        path = split.path
        if path == HEALTH_PATH:
            if self.command not in ("GET", "HEAD"):
                self.write_json(405, {"success": False, "error": "method_not_allowed"})
                return
            self.write_json(200, {"ok": True, "gateway": "firecrawl", "version": VERSION})
            return
        if path.startswith("/__gw/"):
            self.write_json(404, {"success": False, "error": "not_found"})
            return
        if self.command not in FORWARD_METHODS:
            self.write_json(405, {"success": False, "error": "method_not_allowed"})
            return

        settings = self.settings
        header = self.headers.get("Authorization", "")
        try:
            expected = read_token(settings.token_file)
        except TokenUnavailable as exc:
            # Fail closed: never proxy unauthenticated traffic because of an IO error.
            self.log_line("503 token_unavailable path=%s" % path)
            self.write_json(
                503,
                {"success": False, "error": "token_unavailable", "detail": str(exc)},
            )
            return
        if not header.startswith(BEARER_PREFIX):
            self.write_json(
                401,
                {"success": False, "error": "Unauthorized"},
                {"WWW-Authenticate": "Bearer"},
            )
            return
        if not token_equals(header[len(BEARER_PREFIX):].strip(), expected):
            self.write_json(
                401,
                {"success": False, "error": "Unauthorized"},
                {"WWW-Authenticate": "Bearer"},
            )
            return

        body = b""
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            self.write_json(411, {"success": False, "error": "length_required"})
            return
        content_length = self.headers.get("Content-Length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError:
                self.write_json(400, {"success": False, "error": "bad_content_length"})
                return
            if length < 0 or length > settings.max_body_bytes:
                self.write_json(413, {"success": False, "error": "request_too_large"})
                return
            body = self.rfile.read(length)

        target_path = settings.upstream_prefix + path
        if split.query:
            target_path += "?" + split.query
        upstream_headers = {
            "Authorization": BEARER_PREFIX + expected,
            "User-Agent": SERVICE + "/" + VERSION,
            "Accept": self.headers.get("Accept") or "application/json",
            "Connection": "close",
        }
        content_type = self.headers.get("Content-Type")
        if content_type:
            upstream_headers["Content-Type"] = content_type

        connection = None
        try:
            if settings.upstream_scheme == "https":
                connection = http.client.HTTPSConnection(
                    settings.upstream_host, settings.upstream_port, timeout=settings.timeout
                )
            else:
                connection = http.client.HTTPConnection(
                    settings.upstream_host, settings.upstream_port, timeout=settings.timeout
                )
            connection.request(self.command, target_path, body=body, headers=upstream_headers)
            response = connection.getresponse()
            response_body = response.read()
            response_headers = {}
            for name in RESPONSE_HEADER_ALLOWLIST:
                value = response.getheader(name)
                if value:
                    response_headers[name] = value
            self.send_response(response.status)
            for name, value in response_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response_body)
            self.log_line("%d %s %s upstream=%d" % (response.status, self.command, path, response.status))
        except socket.timeout:
            self.log_line("504 upstream_timeout path=%s" % path)
            self.write_json(504, {"success": False, "error": "upstream_timeout"})
        except (ConnectionError, OSError) as exc:
            self.log_line("502 upstream_unreachable path=%s (%s)" % (path, exc.__class__.__name__))
            self.write_json(502, {"success": False, "error": "upstream_unreachable"})
        except Exception as exc:  # noqa: BLE001 - a proxy must not crash on one request
            self.log_line("502 proxy_error path=%s (%s)" % (path, exc.__class__.__name__))
            self.write_json(502, {"success": False, "error": "proxy_error"})
        finally:
            if connection is not None:
                connection.close()


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, settings):
        self.settings = settings
        super().__init__(address, handler)


def preflight(settings):
    if not settings.token_file:
        raise SystemExit("TOKEN_FILE is required, e.g. /opt/fedcap/firecrawl-gateway.token")
    try:
        read_token(settings.token_file)
    except TokenUnavailable as exc:
        raise SystemExit(
            "cannot start gateway: %s (create a 0600 file with a random token first)" % exc
        )
    if settings.host in ("0.0.0.0", "::"):
        sys.stderr.write(
            "warning: binding every interface; make sure a firewall or tailnet ACL limits access\n"
        )


def main():
    settings = Settings()
    preflight(settings)
    server = GatewayServer((settings.host, settings.port), GatewayHandler, settings)
    sys.stderr.write(
        "%s %s listening on %s:%d -> %s (token file re-read per request)\n"
        % (SERVICE, VERSION, settings.host, settings.port, settings.upstream)
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
