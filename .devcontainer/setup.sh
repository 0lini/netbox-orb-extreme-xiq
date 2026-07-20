#!/usr/bin/env bash
# Bootstrap local NetBox env + official Diode quickstart + Orb agent config.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV="$ROOT/.devcontainer"
DIODE="$DEV/diode"
NETBOX_SECRETS="$DEV/netbox/secrets"
# Compose-network URL so Diode reconciler can reach the NetBox plugin API.
NETBOX_HOST="${NETBOX_HOST:-http://netbox:8080}"
DIODE_QUICKSTART_URL="${DIODE_QUICKSTART_URL:-https://raw.githubusercontent.com/netboxlabs/diode/release/diode-server/docker/scripts/quickstart.sh}"

generate_secret() {
  while true; do
    local s
    s="$(head -c 48 /dev/urandom | base64 | tr -d '/\n+=' | head -c 40)"
    if [[ ${#s} -eq 40 ]]; then
      printf '%s' "$s"
      return
    fi
  done
}

generate_long_secret() {
  while true; do
    local s
    s="$(head -c 64 /dev/urandom | base64 | tr -d '/\n+=' | head -c 64)"
    if [[ ${#s} -ge 50 ]]; then
      printf '%s' "$s"
      return
    fi
  done
}

generate_hex() {
  local n="${1:-40}"
  while true; do
    local s
    s="$(head -c "$((n + 8))" /dev/urandom | od -An -v -tx1 | tr -d ' \n' | head -c "$n")"
    if [[ ${#s} -eq "$n" ]]; then
      printf '%s' "$s"
      return
    fi
  done
}

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required for Diode OAuth client parsing." >&2
  echo "Install it, then reopen the Dev Container:" >&2
  echo "  Fedora/Bazzite:  rpm-ostree install jq && systemctl reboot" >&2
  echo "  Debian/Ubuntu:   sudo apt-get install -y jq" >&2
  echo "  macOS:           brew install jq" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required" >&2
  exit 1
fi
if ((BASH_VERSINFO[0] < 4)); then
  echo "Bash 4+ required (official Diode quickstart uses associative arrays)" >&2
  exit 1
fi

# Persist output for Dev Containers (Cursor often hides initializeCommand stderr).
SETUP_LOG="$DEV/setup.log"
mkdir -p "$DEV" "$NETBOX_SECRETS" "$DEV/netbox/env" "$DIODE"
if [[ -z "${_ORB_SETUP_LOGGING:-}" ]]; then
  export _ORB_SETUP_LOGGING=1
  # Re-exec so a tee'd log still preserves the real exit code.
  bash "$0" "$@" 2>&1 | tee "$SETUP_LOG"
  exit "${PIPESTATUS[0]}"
fi
echo "Logging setup to $SETUP_LOG"

# --- NetBox compose env (gitignored); keep existing so volumes stay valid ---
NETBOX_ENV="$DEV/netbox/env/netbox.env"
POSTGRES_ENV="$DEV/netbox/env/postgres.env"
REDIS_ENV="$DEV/netbox/env/redis.env"
REDIS_CACHE_ENV="$DEV/netbox/env/redis-cache.env"

if [[ ! -f "$NETBOX_ENV" || ! -f "$POSTGRES_ENV" || ! -f "$REDIS_ENV" || ! -f "$REDIS_CACHE_ENV" ]]; then
  echo "Generating NetBox env files under $DEV/netbox/env/ ..."
  NB_DB_PASSWORD="$(generate_secret)"
  NB_REDIS_PASSWORD="$(generate_secret)"
  NB_REDIS_CACHE_PASSWORD="$(generate_secret)"
  NB_SECRET_KEY="$(generate_long_secret)"
  NB_API_PEPPER="$(generate_long_secret)"
  NB_SUPERUSER_API_TOKEN="$(generate_hex 40)"

  cat >"$POSTGRES_ENV" <<EOF
POSTGRES_DB=netbox
POSTGRES_PASSWORD=${NB_DB_PASSWORD}
POSTGRES_USER=netbox
EOF

  cat >"$REDIS_ENV" <<EOF
REDIS_PASSWORD=${NB_REDIS_PASSWORD}
EOF

  cat >"$REDIS_CACHE_ENV" <<EOF
REDIS_PASSWORD=${NB_REDIS_CACHE_PASSWORD}
EOF

  cat >"$NETBOX_ENV" <<EOF
CORS_ORIGIN_ALLOW_ALL=True
DB_HOST=netbox-postgres
DB_NAME=netbox
DB_PASSWORD=${NB_DB_PASSWORD}
DB_USER=netbox
EMAIL_FROM=netbox@localhost
EMAIL_PASSWORD=
EMAIL_PORT=25
EMAIL_SERVER=localhost
EMAIL_SSL_CERTFILE=
EMAIL_SSL_KEYFILE=
EMAIL_TIMEOUT=5
EMAIL_USERNAME=netbox
EMAIL_USE_SSL=false
EMAIL_USE_TLS=false
GRAPHQL_ENABLED=true
HOUSEKEEPING_INTERVAL=86400
MEDIA_ROOT=/opt/netbox/netbox/media
METRICS_ENABLED=false
REDIS_CACHE_DATABASE=1
REDIS_CACHE_HOST=netbox-redis-cache
REDIS_CACHE_INSECURE_SKIP_TLS_VERIFY=false
REDIS_CACHE_PASSWORD=${NB_REDIS_CACHE_PASSWORD}
REDIS_CACHE_SSL=false
REDIS_DATABASE=0
REDIS_HOST=netbox-redis
REDIS_INSECURE_SKIP_TLS_VERIFY=false
REDIS_PASSWORD=${NB_REDIS_PASSWORD}
REDIS_SSL=false
SECRET_KEY=${NB_SECRET_KEY}
SKIP_SUPERUSER=false
SUPERUSER_NAME=admin
SUPERUSER_EMAIL=admin@example.com
SUPERUSER_PASSWORD=admin
SUPERUSER_API_TOKEN=${NB_SUPERUSER_API_TOKEN}
WEBHOOKS_ENABLED=true
DEBUG=False
API_TOKEN_PEPPER_1=${NB_API_PEPPER}
EOF
  chmod 600 "$NETBOX_ENV" "$POSTGRES_ENV" "$REDIS_ENV" "$REDIS_CACHE_ENV"
else
  echo "Using existing NetBox env files under $DEV/netbox/env/"
  NB_SUPERUSER_API_TOKEN="$(grep -E '^SUPERUSER_API_TOKEN=' "$NETBOX_ENV" | cut -d= -f2- || true)"
fi

# --- Official Diode quickstart (downloads compose / nginx / .env / oauth clients) ---
# Keep existing secrets so Postgres/Redis volumes stay valid (same idea as NetBox
# env above). Upstream quickstart only fills missing files, but minting a new
# .env or OAuth clients against leftover volumes breaks auth until Diode volumes
# are wiped (do not use parent compose down -v — that also drops NetBox data).
CREDS="$DIODE/oauth2/client/client-credentials.json"
DIODE_COMPOSE="$DIODE/docker-compose.yaml"
DIODE_ENV="$DIODE/.env"

list_diode_data_volumes() {
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  docker volume ls -q 2>/dev/null | grep -E '(^|_)diode-(postgres|redis)-data$' || true
}

diode_data_volumes_exist() {
  local vols
  vols="$(list_diode_data_volumes)"
  if [[ -n "$vols" ]]; then
    return 0
  fi
  # docker CLI missing/unreachable — treat a prior compose download as evidence
  # of an existing stack (safer than minting passwords that may not match data).
  [[ -f "$DIODE_COMPOSE" ]]
}

wipe_diode_data_volumes() {
  local vols cid
  vols="$(list_diode_data_volumes)"
  if [[ -z "$vols" ]]; then
    return 0
  fi
  # Compose down needs diode/.env (include env_file), which is exactly what is
  # missing in this recovery path. Remove only containers that still hold the
  # Diode data volumes, then delete those volumes. NetBox volumes stay intact.
  echo "Removing orphaned Diode volumes (NetBox volumes are left untouched):"
  while IFS= read -r v; do
    [[ -n "$v" ]] || continue
    while IFS= read -r cid; do
      [[ -n "$cid" ]] || continue
      docker rm -f "$cid" >/dev/null 2>&1 || true
    done < <(docker ps -aq --filter "volume=$v" 2>/dev/null || true)
    echo "  $v"
    docker volume rm "$v" >/dev/null 2>&1 || docker volume rm -f "$v" >/dev/null
  done <<<"$vols"
}

if [[ ! -f "$DIODE_COMPOSE" || ! -f "$DIODE_ENV" || ! -f "$CREDS" ]]; then
  missing_env=false
  missing_creds=false
  [[ -f "$DIODE_ENV" ]] || missing_env=true
  [[ -f "$CREDS" ]] || missing_creds=true

  if [[ "$missing_env" == true || "$missing_creds" == true ]]; then
    if diode_data_volumes_exist; then
      # Both secret files gone (common after the move into .devcontainer/, or a
      # clean clone with leftover project volumes): wipe Diode volumes and
      # re-mint. A partial loss still needs an explicit force to avoid desync.
      if [[ -z "${DIODE_FORCE_QUICKSTART:-}" && "$missing_env" == true && "$missing_creds" == true ]]; then
        echo "Diode secrets are missing but leftover Diode Docker volumes exist."
        echo "Wiping Diode volumes only so quickstart can mint matching secrets"
        echo "(NetBox data volumes are left untouched)."
        wipe_diode_data_volumes
      elif [[ -z "${DIODE_FORCE_QUICKSTART:-}" ]]; then
        cat >&2 <<EOF
Error: Diode secrets are missing (.env and/or client-credentials.json) but Diode
Postgres/Redis Docker volumes still exist. Re-running the official quickstart
would generate new passwords that no longer match persisted data.

Restore the missing files, or wipe Diode volumes only (not NetBox) and re-run:
  docker compose -f "$DEV/docker-compose.yml" down
  docker volume ls -q | grep -E '(^|_)diode-(postgres|redis)-data\$' | xargs -r docker volume rm
  bash ./.devcontainer/setup.sh

To proceed anyway (breaks an existing Diode volume), set DIODE_FORCE_QUICKSTART=1.
EOF
        exit 1
      else
        echo "DIODE_FORCE_QUICKSTART=1: wiping Diode volumes before regenerating secrets."
        wipe_diode_data_volumes || true
      fi
    fi
    # Regenerating only one secret file desyncs OAuth clients from .env
    # (DIODE_TO_NETBOX_CLIENT_SECRET is not rewritten once placeholders are gone).
    if [[ -z "${DIODE_FORCE_QUICKSTART:-}" && "$missing_env" != "$missing_creds" ]]; then
      cat >&2 <<EOF
Error: Diode secrets are incomplete — need both:
  $DIODE_ENV
  $CREDS
Restore the missing file, or remove both (and wipe Diode volumes if present)
before re-running setup. To proceed anyway, set DIODE_FORCE_QUICKSTART=1.
EOF
      exit 1
    fi
  fi
  echo "Running official Diode quickstart in $DIODE ..."
  curl -sSfLo "$DIODE/quickstart.sh" "$DIODE_QUICKSTART_URL"
  chmod +x "$DIODE/quickstart.sh"
  (
    cd "$DIODE"
    ./quickstart.sh "$NETBOX_HOST"
  )
else
  echo "Using existing Diode quickstart files under $DIODE/"
fi

# Local HTTP NetBox: skip TLS verify between Diode reconciler and the plugin.
if grep -q '^NETBOX_DIODE_PLUGIN_SKIP_TLS_VERIFY=' "$DIODE/.env"; then
  tmp="$(mktemp)"
  awk '
    /^NETBOX_DIODE_PLUGIN_SKIP_TLS_VERIFY=/ {print "NETBOX_DIODE_PLUGIN_SKIP_TLS_VERIFY=true"; next}
    {print}
  ' "$DIODE/.env" >"$tmp"
  mv "$tmp" "$DIODE/.env"
  chmod 600 "$DIODE/.env"
fi

INGEST_SECRET="$(jq -r '.[] | select(.client_id=="diode-ingest") | .client_secret' "$CREDS")"
NETBOX_TO_DIODE_SECRET="$(jq -r '.[] | select(.client_id=="netbox-to-diode") | .client_secret' "$CREDS")"
if [[ -z "$INGEST_SECRET" || "$INGEST_SECRET" == "null" ]]; then
  echo "Missing diode-ingest client secret in $CREDS" >&2
  exit 1
fi
if [[ -z "$NETBOX_TO_DIODE_SECRET" || "$NETBOX_TO_DIODE_SECRET" == "null" ]]; then
  echo "Missing netbox-to-diode client secret in $CREDS" >&2
  exit 1
fi

printf '%s' "$NETBOX_TO_DIODE_SECRET" >"$NETBOX_SECRETS/netbox_to_diode"
chmod 600 "$NETBOX_SECRETS/netbox_to_diode"

# --- Host / Orb agent env ---
NETBOX_TOKEN="${NB_SUPERUSER_API_TOKEN:-}"
if [[ -z "$NETBOX_TOKEN" ]]; then
  NETBOX_TOKEN="$(generate_hex 40)"
fi
if [[ ! -f "$DEV/.env.local" ]]; then
  cat >"$DEV/.env.local" <<EOF
# Generated by .devcontainer/setup.sh — do not commit
NETBOX_API_URL=http://localhost:8000
NETBOX_API_TOKEN=${NETBOX_TOKEN}
DIODE_TARGET=grpc://localhost:8080/diode
DIODE_CLIENT_ID=diode-ingest
DIODE_CLIENT_SECRET=${INGEST_SECRET}
EOF
  chmod 600 "$DEV/.env.local"
else
  echo "Using existing $DEV/.env.local"
  tmp="$(mktemp)"
  awk -v secret="$INGEST_SECRET" '
    /^DIODE_CLIENT_SECRET=/ {print "DIODE_CLIENT_SECRET=" secret; next}
    {print}
  ' "$DEV/.env.local" >"$tmp"
  mv "$tmp" "$DEV/.env.local"
  chmod 600 "$DEV/.env.local"
fi

if [[ ! -f "$DEV/agent.local.yaml" ]]; then
  cat >"$DEV/agent.local.yaml" <<EOF
# Generated local Orb Agent policy — Diode target is the compose-published port.
orb:
  config_manager:
    active: local
  backends:
    worker:
      common:
        diode:
          target: grpc://localhost:8080/diode
          client_id: \${DIODE_CLIENT_ID}
          client_secret: \${DIODE_CLIENT_SECRET}
          agent_name: platformone_local_1
  policies:
    worker:
      extreme_platformone_worker:
        config:
          package: orb_extreme_platformone
          BOOTSTRAP: true
          NETBOX_API_URL: \${NETBOX_API_URL}
          NETBOX_API_TOKEN: \${NETBOX_API_TOKEN}
          PLATFORMONE_API_TOKEN: \${PLATFORMONE_API_TOKEN}
          classification: ALL
        scope:
          sites: ["*"]
EOF
else
  echo "Using existing $DEV/agent.local.yaml"
fi

cat >"$DEV/workers.local.txt" <<EOF
.
EOF

cat <<EOF

Setup complete (NetBox env + official Diode quickstart).

  docker compose -f "$DEV/docker-compose.yml" up -d --build
  ./.devcontainer/create-netbox-token.sh   # once NetBox is healthy

NetBox http://localhost:8000 (admin/admin)  Diode grpc://localhost:8080/diode
Diode files: $DIODE/ (from upstream quickstart; gitignored)
Secrets: $DEV/.env.local , $DEV/netbox/env/*.env , $CREDS

Orb agent (from repo root):
  set -a; source "$DEV/.env.local"; set +a
  export PLATFORMONE_API_TOKEN=...
  docker run --rm --network host \\
    -v "\$PWD:/opt/orb/" \\
    -e INSTALL_WORKERS_PATH=/opt/orb/.devcontainer/workers.local.txt \\
    -e DIODE_CLIENT_ID -e DIODE_CLIENT_SECRET \\
    -e PLATFORMONE_API_TOKEN -e NETBOX_API_URL -e NETBOX_API_TOKEN \\
    netboxlabs/orb-agent:latest run -c /opt/orb/.devcontainer/agent.local.yaml
EOF
