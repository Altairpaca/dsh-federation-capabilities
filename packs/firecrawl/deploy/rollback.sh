#!/usr/bin/env bash
# Fedcap Firecrawl token gateway rollback (sanitized template).
#
# Undoes what deploy/setup.sh did:
#   1. stop + disable + remove the systemd --user unit;
#   2. remove the generated environment file (kept as .bak-*);
#   3. restore the newest timestamped compose backup and rebind it by recreating
#      the stack when --recreate is given;
#   4. optionally purge the gateway directory and token file.
#
# The Firecrawl stack itself is never touched unless --recreate is passed.
#
# Usage:
#   ./rollback.sh [options] [--yes]
#   ./rollback.sh --compose-file /opt/fedcap/firecrawl/docker-compose.yaml --recreate --purge --yes
#
# Options:
#   --gateway-dir DIR     gateway install dir   (default $HOME/.local/share/fedcap/firecrawl-gateway)
#   --unit-dir DIR        systemd --user dir    (default $HOME/.config/systemd/user)
#   --env-file PATH       environment file      (default $HOME/.config/fedcap/firecrawl-gateway.env)
#   --token-file PATH     token file to purge   (default $HOME/.config/fedcap/firecrawl-gateway.token)
#   --compose-file PATH   compose file whose newest .bak-* backup is restored
#   --recreate            run `docker compose up -d` after restoring the compose file
#   --purge               remove the gateway dir and token file (off by default)
#   --yes                 actually apply changes (default: print the plan only)
#   -h, --help            this help
#
# Exit codes: 0 success, 1 runtime error, 2 usage error.

set -euo pipefail

GATEWAY_DIR=${GATEWAY_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/fedcap/firecrawl-gateway}
UNIT_DIR=${UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}
ENV_FILE=${ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/fedcap/firecrawl-gateway.env}
TOKEN_FILE=${TOKEN_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/fedcap/firecrawl-gateway.token}
COMPOSE_FILE=${COMPOSE_FILE:-}
ASSUME_YES=0
RECREATE=0
PURGE=0

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --gateway-dir) GATEWAY_DIR=${2:?missing value}; shift 2 ;;
    --unit-dir) UNIT_DIR=${2:?missing value}; shift 2 ;;
    --env-file) ENV_FILE=${2:?missing value}; shift 2 ;;
    --token-file) TOKEN_FILE=${2:?missing value}; shift 2 ;;
    --compose-file) COMPOSE_FILE=${2:?missing value}; shift 2 ;;
    --recreate) RECREATE=1; shift ;;
    --purge) PURGE=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s (try --help)\n' "$1" >&2; exit 2 ;;
  esac
done

UNIT_PATH=${UNIT_DIR}/firecrawl-gateway.service

printf 'Fedcap Firecrawl token gateway rollback\n'
printf '  unit           %s\n' "$UNIT_PATH"
printf '  env file       %s\n' "$ENV_FILE"
printf '  gateway dir    %s\n' "$GATEWAY_DIR"
if [ -n "$COMPOSE_FILE" ]; then
  printf '  compose        %s (restore newest %s.bak-*)\n' "$COMPOSE_FILE" "$COMPOSE_FILE"
else
  printf '  compose        (not given; left untouched)\n'
fi
if [ "$PURGE" -eq 1 ]; then
  printf '  purge          gateway dir + token file\n'
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  printf '\nplan only; re-run with --yes to apply.\n'
  exit 0
fi

# --- 1. systemd unit --------------------------------------------------------
printf '\n[1/4] stop and remove systemd user unit\n'
if systemctl --user list-unit-files firecrawl-gateway.service >/dev/null 2>&1 \
  && systemctl --user cat firecrawl-gateway.service >/dev/null 2>&1; then
  systemctl --user disable --now firecrawl-gateway.service || true
fi
rm -f -- "$UNIT_PATH"
systemctl --user daemon-reload
printf '      removed %s\n' "$UNIT_PATH"

# --- 2. environment file ----------------------------------------------------
printf '\n[2/4] remove environment file\n'
if [ -f "$ENV_FILE" ]; then
  cp -p "$ENV_FILE" "${ENV_FILE}.bak-$(date +%Y%m%d-%H%M%S)"
  rm -f -- "$ENV_FILE"
  printf '      removed %s (backup kept)\n' "$ENV_FILE"
else
  printf '      not present\n'
fi

# --- 3. compose restore -----------------------------------------------------
printf '\n[3/4] restore compose file\n'
if [ -n "$COMPOSE_FILE" ]; then
  [ -f "$COMPOSE_FILE" ] || die "compose file not found: $COMPOSE_FILE"
  newest=$(ls -1t "${COMPOSE_FILE}".bak-* 2>/dev/null | head -n 1 || true)
  if [ -n "$newest" ]; then
    cp -p "$newest" "$COMPOSE_FILE"
    printf '      restored %s -> %s\n' "$newest" "$COMPOSE_FILE"
    if [ "$RECREATE" -eq 1 ]; then
      docker compose -f "$COMPOSE_FILE" up -d
      printf '      compose stack recreated; the api port binding is back to the backup value\n'
    else
      printf '      re-run `docker compose -f %s up -d` to apply\n' "$COMPOSE_FILE"
    fi
  else
    printf '      no %s.bak-* backup found; edit the compose file manually if needed\n' "$COMPOSE_FILE"
  fi
else
  printf '      skipped (no --compose-file)\n'
fi

# --- 4. optional purge ------------------------------------------------------
printf '\n[4/4] purge\n'
if [ "$PURGE" -eq 1 ]; then
  rm -rf -- "$GATEWAY_DIR"
  rm -f -- "$TOKEN_FILE"
  printf '      removed %s and %s\n' "$GATEWAY_DIR" "$TOKEN_FILE"
else
  printf '      kept gateway dir and token file (use --purge to remove)\n'
fi

printf '\nrollback finished\n'
