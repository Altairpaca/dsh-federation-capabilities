# Firecrawl token gateway (deploy template)

Self-hosted Firecrawl has no authentication of its own. Anything that can reach
its API port can make the host crawl arbitrary URLs, including `127.0.0.1`,
private ranges and other services on the same Tailnet/VPC (a server-side request
forgery surface). This directory ships a small, standard-library reverse proxy
that requires `Authorization: Bearer <token>` on every request, and the scripts
to install, verify and roll back that proxy.

Everything here is sanitized: placeholders are `{{PYTHON}}` / `{{GATEWAY_DIR}}`,
examples use `127.0.0.1`, `example.invalid` and `/opt/fedcap`. Replace values
with your own host-local paths at deploy time; never commit them.

## Architecture

```
fedcap shell capability / mesh node / curl
        │  Authorization: Bearer <token>
        ▼
gateway.py  (systemd --user, 127.0.0.1:3003)
        │  Authorization: Bearer <token from TOKEN_FILE>   ← added server side
        ▼
Firecrawl api  (127.0.0.1:3002, no auth, loopback only)
        ├── playwright-service   (internal)
        ├── searxng              (internal, used by /v2/search)
        └── redis / rabbitmq / postgres (internal)
```

Two properties matter:

1. **The unauthenticated port must be loopback.** The gateway is only as good as
   the binding of the upstream. Step 1 below rebinds the compose port.
2. **The gateway is auth, not SSRF protection.** Callers that hold the token can
   still ask Firecrawl to fetch internal URLs. Restrict who gets the token
   (Tailscale ACL, firewall, per-node token files) and keep the gateway off the
   public internet.

## Files

| File | Purpose |
| --- | --- |
| `gateway.py` | stdlib reverse proxy; bearer auth; anonymous `GET /__gw/health`; re-reads the token file on **every** request |
| `firecrawl-gateway.service` | systemd `--user` unit template; placeholders `{{PYTHON}}` and `{{GATEWAY_DIR}}` are substituted by `setup.sh` |
| `firecrawl-gateway.env.example` | example environment file (`GATEWAY_HOST`, `GATEWAY_PORT`, `GATEWAY_UPSTREAM`, `GATEWAY_TOKEN_FILE`) |
| `setup.sh` | parameterised installer: compose backup + loopback rebind, token creation, gateway install, unit install, health check |
| `rollback.sh` | stops/removes the unit, restores the newest compose backup, optional `--purge` |

The canonical environment variable names match the reference deployment:
`GATEWAY_HOST`, `GATEWAY_PORT`, `GATEWAY_UPSTREAM`, `GATEWAY_TOKEN_FILE`. The
gateway also accepts the shorter `UPSTREAM` / `TOKEN_FILE` names as a fallback,
and `MAX_BODY_BYTES` / `UPSTREAM_TIMEOUT_SECONDS` are optional tuning knobs.
The anonymous health endpoint returns
`{"ok": true, "gateway": "firecrawl", "version": "0.1.0"}`; every other
response on failure is Firecrawl-shaped: `{"success": false, "error": "..."}`.

## Prerequisites

- A running self-hosted Firecrawl with its compose file on disk (the official
  `docker-compose.yaml` has `api`, `playwright-service`, `redis`, `rabbitmq`,
  `nuq-postgres` and `searxng` services).
- `python3` (3.8+), `curl`, `docker compose`.
- systemd user manager. For the unit to survive logout/reboot:
  `loginctl enable-linger "$USER"`.
- No root is required when `--gateway-dir` stays under `$HOME` (the default).

## Step 1 — rebind the Firecrawl api port to loopback first

> **Do not use a `docker-compose.override.yml` with `!reset` for this.** A
> `!reset` override of a key that only exists in the base file fails on first
> apply in several Compose versions (the merge runs before the base key is
> visible to the reset), which leaves you either with an error or, worse, with a
> stack that still publishes `0.0.0.0:3002`. Edit the base compose file instead;
> `setup.sh` does exactly that and keeps a timestamped backup.

The upstream `api` service normally publishes something like
`- "3002:3002"` or `- "0.0.0.0:3002:3002"` under `ports:`. Change the **host
side** to loopback:

```yaml
  api:
    ports:
      - "127.0.0.1:3002:3002"   # was "- 3002:3002"
```

Then recreate the stack and confirm the port is no longer exposed:

```bash
docker compose -f /opt/fedcap/firecrawl/docker-compose.yaml up -d
ss -ltn | grep ':3002 '          # expect 127.0.0.1:3002 only
```

`setup.sh --compose-file ... --recreate` automates the edit (with a `.bak-<UTC
timestamp>` backup) and the recreate.

## Step 2 — install and start the gateway

```bash
# Plan first (writes nothing):
./setup.sh \
  --compose-file /opt/fedcap/firecrawl/docker-compose.yaml

# Apply. Keep the token file out of version control; 0600 is enforced.
./setup.sh \
  --compose-file /opt/fedcap/firecrawl/docker-compose.yaml \
  --gateway-host 127.0.0.1 \
  --gateway-port 3003 \
  --upstream http://127.0.0.1:3002 \
  --token-file "$HOME/.config/fedcap/firecrawl-gateway.token" \
  --gateway-dir "$HOME/.local/share/fedcap/firecrawl-gateway" \
  --recreate \
  --yes
```

If the compose file is managed elsewhere (or you already rebound it manually),
pass `--no-compose` and skip `--compose-file`; the installer will print the
manual checklist instead of editing the file.

The script creates a random token when the file is missing (the value is never
printed to the log), installs `gateway.py`, writes
`~/.config/fedcap/firecrawl-gateway.env`, installs and enables
`firecrawl-gateway.service`, then calls the anonymous health endpoint.

## Step 3 — verify

```bash
# 1. Anonymous health (no token, must be 200):
curl -fsS http://127.0.0.1:3003/__gw/health

# 2. Without a token the API must refuse (401):
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://127.0.0.1:3003/v2/scrape \
  -H 'content-type: application/json' -d '{"url":"https://example.invalid"}'

# 3. With the token it must proxy (200; example.invalid has no content, so use
#    any URL you are allowed to crawl):
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://127.0.0.1:3003/v2/scrape \
  -H "authorization: Bearer $(cat "$HOME/.config/fedcap/firecrawl-gateway.token")" \
  -H 'content-type: application/json' -d '{"url":"https://example.invalid","formats":["markdown"]}'

# 4. The upstream port must be loopback-only:
ss -ltn | grep ':3002 '
```

Then install the pack against the gateway:

```bash
fedcap install --pack packs/firecrawl --node-config /opt/fedcap/node.json \
  --set baseUrl=http://127.0.0.1:3003 \
  --set tokenFile="$HOME/.config/fedcap/firecrawl-gateway.token"
```

## Token rotation

The gateway re-reads `TOKEN_FILE` on every request, so rotation needs no restart:

1. write the new token into the token file (`printf '%s' "$NEW" > "$TOKEN_FILE"`,
   keep `chmod 600`);
2. update the values used by every client node (`--set tokenFile=...` or the
   values file) and restart those nodes if they cache values;
3. invalidate the old token — it stops working on the next request.

Keep one token per client node when possible so revocation is per-node.

## Rollback

```bash
./rollback.sh \
  --compose-file /opt/fedcap/firecrawl/docker-compose.yaml \
  --recreate --purge --yes
```

This disables/removes the unit, restores the newest compose backup (the stack
goes back to the previous port binding), optionally removes the gateway
directory and token file, and reloads systemd. Without `--purge`, the token and
gateway files are kept for a later re-install.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `401 unauthorized` | missing/malformed `Authorization: Bearer …` header, or client still has the old token | rotate/re-copy the token; check the header exactly |
| `503 token_unavailable` | token file missing, empty or unreadable for the unit's user | re-create it with `chmod 600`; `journalctl --user -u firecrawl-gateway.service` |
| `502 upstream_unreachable` | Firecrawl is not running, or it still listens somewhere else than `UPSTREAM` | start the stack; check `ss -ltn ':(3002)'`; fix `UPSTREAM` in the env file |
| `504 upstream_timeout` | Firecrawl is slow (cold Playwright, big crawl) | retry; raise `UPSTREAM_TIMEOUT_SECONDS`; keep capability `timeoutMs` above it |
| `413 request_too_large` | body above `MAX_BODY_BYTES` | raise the limit only if you really proxy big crawl payloads |
| gateway starts but health fails | bind address/port in use | `ss -ltn ':(3003)'`; change `--gateway-port` |
| unit does not start at boot | systemd user manager stops at logout | `loginctl enable-linger "$USER"` |

Logs: `journalctl --user -u firecrawl-gateway.service -n 100 --no-pager`.
The gateway never logs the token, the `Authorization` header or query strings.

## Security notes

- Bind loopback (`127.0.0.1`) or a private/Tailnet address; never `0.0.0.0` on a
  public host. If you bind a Tailnet address, put a Tailscale ACL in front of it.
- This template does **not** add URL filtering: a token holder can still use
  Firecrawl to reach internal URLs. Combine it with network policy (ACL,
  egress firewall on the Firecrawl container/host) if that matters.
- Rotate the token on suspicion and keep it out of shell history and version
  control (`--set` values end up in the node config; the file path in
  `--set tokenFile=...` is fine, the token itself never enters the manifest).
- The health endpoint is intentionally anonymous and exposes only
  `{"ok":true,"gateway":"firecrawl","version":"0.1.0"}`. Auth failures return
  `401 {"success":false,"error":"Unauthorized"}`; upstream failures return
  `502`/`503`/`504` with the same `{"success":false,"error":"..."}` shape.
