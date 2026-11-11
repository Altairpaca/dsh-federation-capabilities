#!/usr/bin/env bash
# Fedcap Firecrawl token gateway installer (sanitized template).
#
# What it does, in order:
#   1. (optionally) rebind the Firecrawl compose api port to 127.0.0.1, backing
#      up the compose file first;
#   2. create a bearer token file (mode 0600) or reuse an existing one;
#   3. install gateway.py under $GATEWAY_DIR;
#   4. write a systemd --user environment file and unit, enable + start it;
#   5. print verification commands.
#
# Everything is parameterised. Defaults are safe for a single-user host and
# never touch the real Firecrawl compose file unless --compose-file is given.
#
# Usage:
#   ./setup.sh [options]
#   ./setup.sh --compose-file /opt/fedcap/firecrawl/docker-compose.yaml --yes --recreate
#
# Options (environment variables with the same names are also accepted):
#   --gateway-host HOST     bind address for the gateway      (default 127.0.0.1)
#   --gateway-port PORT     bind port for the gateway         (default 3003)
#   --upstream URL          upstream Firecrawl base URL       (default http://127.0.0.1:3002)
#   --token-file PATH       bearer token file                 (default $HOME/.config/fedcap/firecrawl-gateway.token)
#   --gateway-dir DIR       where gateway.py is installed     (default $HOME/.local/share/fedcap/firecrawl-gateway)
#   --unit-dir DIR          systemd --user unit directory     (default $HOME/.config/systemd/user)
#   --env-file PATH         systemd environment file          (default $HOME/.config/fedcap/firecrawl-gateway.env)
#   --compose-file PATH     Firecrawl docker-compose.yaml to rebind (optional; see --no-compose)
#   --host-port PORT        host port to bind in compose       (default: port of --upstream)
#   --container-port PORT   container port in compose          (default: same as --host-port)
#   --python EXE            python interpreter                 (default python3)
#   --recreate              run `docker compose up -d` after editing the compose file
#   --no-compose            do not edit the compose file (you will rebind it yourself)
#   --yes                   actually apply changes (default: print the plan only)
#   -h, --help              this help
#
# Exit codes: 0 success, 1 runtime error, 2 usage error.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

GATEWAY_HOST=${GATEWAY_HOST:-127.0.0.1}
GATEWAY_PORT=${GATEWAY_PORT:-3003}
UPSTREAM=${UPSTREAM:-http://127.0.0.1:3002}
TOKEN_FILE=${TOKEN_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/fedcap/firecrawl-gateway.token}
GATEWAY_DIR=${GATEWAY_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/fedcap/firecrawl-gateway}
UNIT_DIR=${UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}
ENV_FILE=${ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/fedcap/firecrawl-gateway.env}
COMPOSE_FILE=${COMPOSE_FILE:-}
HOST_PORT=${HOST_PORT:-}
CONTAINER_PORT=${CONTAINER_PORT:-}
PYTHON_BIN=${PYTHON_BIN:-python3}
ASSUME_YES=0
RECREATE=0
SKIP_COMPOSE=0

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; }

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --gateway-host) GATEWAY_HOST=${2:?missing value}; shift 2 ;;
    --gateway-port) GATEWAY_PORT=${2:?missing value}; shift 2 ;;
    --upstream) UPSTREAM=${2:?missing value}; shift 2 ;;
    --token-file) TOKEN_FILE=${2:?missing value}; shift 2 ;;
    --gateway-dir) GATEWAY_DIR=${2:?missing value}; shift 2 ;;
    --unit-dir) UNIT_DIR=${2:?missing value}; shift 2 ;;
    --env-file) ENV_FILE=${2:?missing value}; shift 2 ;;
    --compose-file) COMPOSE_FILE=${2:?missing value}; shift 2 ;;
    --host-port) HOST_PORT=${2:?missing value}; shift 2 ;;
    --container-port) CONTAINER_PORT=${2:?missing value}; shift 2 ;;
    --python) PYTHON_BIN=${2:?missing value}; shift 2 ;;
    --recreate) RECREATE=1; shift ;;
    --no-compose) SKIP_COMPOSE=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s (try --help)\n' "$1" >&2; exit 2 ;;
  esac
done

UPSTREAM=${UPSTREAM%/}
if [ -z "$HOST_PORT" ]; then
  HOST_PORT=${UPSTREAM##*:}
  HOST_PORT=${HOST_PORT%%/*}
  case "$HOST_PORT" in
    ''|*[!0-9]*) HOST_PORT=3002 ;;
  esac
fi
if [ -z "$CONTAINER_PORT" ]; then CONTAINER_PORT=$HOST_PORT; fi

command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "python interpreter not found: $PYTHON_BIN"
PYTHON_ABS=$(command -v "$PYTHON_BIN")
[ -f "$SCRIPT_DIR/gateway.py" ] || die "gateway.py not found next to setup.sh"
[ -f "$SCRIPT_DIR/firecrawl-gateway.service" ] || die "firecrawl-gateway.service not found next to setup.sh"

printf 'Fedcap Firecrawl token gateway setup\n'
printf '  gateway        http://%s:%s\n' "$GATEWAY_HOST" "$GATEWAY_PORT"
printf '  upstream       %s\n' "$UPSTREAM"
printf '  token file     %s\n' "$TOKEN_FILE"
printf '  install dir    %s\n' "$GATEWAY_DIR"
printf '  systemd unit   %s/firecrawl-gateway.service\n' "$UNIT_DIR"
printf '  env file       %s\n' "$ENV_FILE"
printf '  python         %s\n' "$PYTHON_ABS"
if [ "$SKIP_COMPOSE" -eq 1 ]; then
  printf '  compose        (skipped; rebind the api port to 127.0.0.1 yourself)\n'
elif [ -n "$COMPOSE_FILE" ]; then
  printf '  compose        %s (port %s -> 127.0.0.1:%s)\n' "$COMPOSE_FILE" "$HOST_PORT" "$CONTAINER_PORT"
else
  printf '  compose        (not given; pass --compose-file or rebind manually)\n'
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  printf '\nplan only; re-run with --yes to apply. The compose file is edited in place\n'
  printf 'after a timestamped backup; the gateway unit is installed under systemd --user.\n'
  exit 0
fi

# --- 1. optional compose rebind ------------------------------------------
if [ "$SKIP_COMPOSE" -eq 0 ] && [ -n "$COMPOSE_FILE" ]; then
  [ -f "$COMPOSE_FILE" ] || die "compose file not found: $COMPOSE_FILE"
  stamp=$(date +%Y%m%d-%H%M%S)
  backup=${COMPOSE_FILE}.bak-${stamp}
  cp -p "$COMPOSE_FILE" "$backup"
  printf '\n[1/5] backed up compose file -> %s\n' "$backup"
  "$PYTHON_ABS" - "$COMPOSE_FILE" "$HOST_PORT" "$CONTAINER_PORT" <<'PY'
import re
import sys

path, host_port, container_port = sys.argv[1:4]
with open(path, "r", encoding="utf-8") as handle:
    lines = handle.readlines()

# A compose port entry looks like  - "3002:3002", - 0.0.0.0:3002:3002,
# - "${PORT:-3002}:3002" or - "127.0.0.1:3002:3002". Rewrite only entries whose
# container-side port is container_port, and force the host side to loopback.
entry = re.compile(r'^(?P<indent>\s*)-\s*(?P<quote>["\']?)(?P<spec>[^#\s"\']+)(?P=quote)(?P<trail>\s*(?:#.*)?)$')

def split_spec(spec):
    parts = []
    current = ""
    depth = 0
    for char in spec:
        if char == "{" : depth += 1
        elif char == "}": depth -= 1
        if char == ":" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts

changed = 0
for index, line in enumerate(lines):
    match = entry.match(line.rstrip("\n"))
    if not match:
        continue
    spec = match.group("spec")
    parts = split_spec(spec)
    if len(parts) < 2:
        continue
    container = parts[-1]
    if container != container_port and not container.endswith(":-" + container_port + "}"):
        continue
    rewritten = "127.0.0.1:" + host_port + ":" + container
    quote = match.group("quote") or '"'
    lines[index] = "%s- %s%s%s%s\n" % (match.group("indent"), quote, rewritten, quote, match.group("trail") or "")
    changed += 1
    sys.stderr.write("  rebind: %s -> %s\n" % (line.strip(), lines[index].strip()))

if changed == 0:
    sys.stderr.write(
        "  warning: no port mapping for container port %s found; edit the compose file manually\n"
        % container_port
    )
else:
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    sys.stderr.write("  rewrote %d port mapping(s)\n" % changed)
PY
else
  printf '\n[1/5] compose rebind skipped\n'
  if [ "$SKIP_COMPOSE" -eq 0 ]; then
    printf '      remember: the Firecrawl api must listen on 127.0.0.1 before the\n'
    printf '      gateway is useful, otherwise the unauthenticated port stays exposed.\n'
  fi
fi

# --- 2. token file ---------------------------------------------------------
printf '\n[2/5] token file\n'
mkdir -p -- "$(dirname -- "$TOKEN_FILE")"
if [ -s "$TOKEN_FILE" ]; then
  chmod 600 "$TOKEN_FILE"
  printf '      reusing existing token file (value not printed)\n'
else
  "$PYTHON_ABS" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  printf '      generated a new random token (value not printed): %s\n' "$TOKEN_FILE"
fi
printf '      copy this token into the client node.json values file, e.g.\n'
printf '      printf "%%s" "REPLACE_ME" > %s\n' "$TOKEN_FILE"

# --- 3. install gateway ----------------------------------------------------
printf '\n[3/5] install gateway\n'
mkdir -p -- "$GATEWAY_DIR"
install -m 0755 "$SCRIPT_DIR/gateway.py" "$GATEWAY_DIR/gateway.py"
printf '      %s/gateway.py\n' "$GATEWAY_DIR"

# --- 4. environment file + unit -------------------------------------------
printf '\n[4/5] systemd user unit\n'
mkdir -p -- "$(dirname -- "$ENV_FILE")" "$UNIT_DIR"
if [ -f "$ENV_FILE" ]; then
  cp -p "$ENV_FILE" "${ENV_FILE}.bak-$(date +%Y%m%d-%H%M%S)"
fi
cat > "$ENV_FILE" <<EOF
# Generated by Fedcap packs/firecrawl/deploy/setup.sh
GATEWAY_HOST=${GATEWAY_HOST}
GATEWAY_PORT=${GATEWAY_PORT}
GATEWAY_UPSTREAM=${UPSTREAM}
GATEWAY_TOKEN_FILE=${TOKEN_FILE}
EOF
chmod 600 "$ENV_FILE"
"$PYTHON_ABS" - "$SCRIPT_DIR/firecrawl-gateway.service" "$UNIT_DIR/firecrawl-gateway.service" "$PYTHON_ABS" "$GATEWAY_DIR" <<'PY'
import sys

template_path, target_path, python_bin, gateway_dir = sys.argv[1:5]
with open(template_path, "r", encoding="utf-8") as handle:
    text = handle.read()
text = text.replace("{{PYTHON}}", python_bin).replace("{{GATEWAY_DIR}}", gateway_dir)
if "{{" in text:
    raise SystemExit("unsubstituted placeholder left in unit template")
with open(target_path, "w", encoding="utf-8") as handle:
    handle.write(text)
PY
systemctl --user daemon-reload
systemctl --user enable --now firecrawl-gateway.service
sleep 1
systemctl --user --no-pager --lines=5 status firecrawl-gateway.service || true

# --- 5. verify -------------------------------------------------------------
probe_host=$GATEWAY_HOST
if [ "$probe_host" = "0.0.0.0" ] || [ "$probe_host" = "::" ]; then probe_host=127.0.0.1; fi
printf '\n[5/5] health check\n'
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 5 "http://${probe_host}:${GATEWAY_PORT}/__gw/health"; then
    printf '\n      gateway is healthy\n'
  else
    printf '\n      warning: anonymous health check failed; inspect the unit logs:\n'
    printf '      journalctl --user -u firecrawl-gateway.service -n 50\n'
  fi
else
  printf '      curl not found; check manually: http://%s:%s/__gw/health\n' "$probe_host" "$GATEWAY_PORT"
fi

# --- optional compose recreate -------------------------------------------
if [ "$RECREATE" -eq 1 ] && [ -n "$COMPOSE_FILE" ]; then
  printf '\n[+] recreating compose stack to apply the loopback port binding\n'
  docker compose -f "$COMPOSE_FILE" up -d
fi

linger=$(loginctl show-user "${USER:-$(id -un)}" -p Linger 2>/dev/null || true)
if [ "$linger" != "Linger=yes" ]; then
  printf '\nhint: enable the systemd user manager across logout/boot with:\n'
  printf '  loginctl enable-linger %s\n' "${USER:-$(id -un)}"
fi

cat <<EOF

Next steps
----------
1. Recreate the Firecrawl stack if you have not already:
     docker compose -f ${COMPOSE_FILE:-/opt/fedcap/firecrawl/docker-compose.yaml} up -d
   then confirm it only listens on loopback:
     ss -ltn | grep ':${HOST_PORT} '
2. Install the pack pointing at the gateway (the token file is the same one):
     fedcap install --pack packs/firecrawl \\
       --node-config /opt/fedcap/node.json \\
       --set baseUrl=http://${GATEWAY_HOST}:${GATEWAY_PORT} \\
       --set tokenFile=${TOKEN_FILE}
3. Verify: anonymous request must fail with 401, authenticated request must work:
     curl -s -o /dev/null -w '%{http_code}\\n' -X POST http://${probe_host}:${GATEWAY_PORT}/v2/scrape \\
       -H 'content-type: application/json' -d '{"url":"https://example.invalid"}'
     printf 'Bearer ' ; cat ${TOKEN_FILE}   # copy into a 0600 header file instead of the shell
EOF
