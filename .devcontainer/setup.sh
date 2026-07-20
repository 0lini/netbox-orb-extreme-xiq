#!/usr/bin/env bash
# Generate Diode OAuth credentials and wire local NetBox + agent config.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV="$ROOT/.devcontainer"
DIODE="$DEV/diode"
NETBOX_SECRETS="$DEV/netbox/secrets"

generate_secret() {
  # Enough entropy after base64 charset filtering.
  while true; do
    local s
    s="$(head -c 48 /dev/urandom | base64 | tr -d '/\n+=' | head -c 40)"
    if [[ ${#s} -eq 40 ]]; then
      printf '%s' "$s"
      return
    fi
  done
}

# NetBox SECRET_KEY / pepper need >= 50 chars.
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
    # `od` (coreutils) is used instead of `xxd` (ships with vim, not guaranteed
    # on a bare host) so setup does not hang when xxd is absent.
    s="$(head -c "$((n + 8))" /dev/urandom | od -An -v -tx1 | tr -d ' \n' | head -c "$n")"
    if [[ ${#s} -eq "$n" ]]; then
      printf '%s' "$s"
      return
    fi
  done
}

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required" >&2
  exit 1
fi

mkdir -p "$DIODE/oauth2/client" "$NETBOX_SECRETS" "$DIODE/nginx" "$DEV/netbox/env"

# Ensure upstream Diode assets exist (compose + nginx).
if [[ ! -f "$DIODE/docker-compose.yaml" ]]; then
  echo "Missing $DIODE/docker-compose.yaml — re-run from a complete checkout." >&2
  exit 1
fi

# NetBox compose env files (gitignored). Skip if already present so volumes stay usable.
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
# Required for API tokens on NetBox 4.6+
API_TOKEN_PEPPER_1=${NB_API_PEPPER}
EOF
  chmod 600 "$NETBOX_ENV" "$POSTGRES_ENV" "$REDIS_ENV" "$REDIS_CACHE_ENV"
else
  echo "Using existing NetBox env files under $DEV/netbox/env/"
  NB_SUPERUSER_API_TOKEN="$(grep -E '^SUPERUSER_API_TOKEN=' "$NETBOX_ENV" | cut -d= -f2- || true)"
fi

CREDS="$DIODE/oauth2/client/client-credentials.json"
if [[ ! -f "$CREDS" ]]; then
  echo "Generating OAuth2 client credentials..."
  INGEST_SECRET="$(generate_secret)"
  TO_NETBOX_SECRET="$(generate_secret)"
  NETBOX_TO_DIODE_SECRET="$(generate_secret)"
  cat >"$CREDS" <<EOF
[
  {
    "client_id": "diode-ingest",
    "client_secret": "${INGEST_SECRET}",
    "grant_types": ["client_credentials"],
    "scope": "diode:ingest"
  },
  {
    "client_id": "diode-to-netbox",
    "client_secret": "${TO_NETBOX_SECRET}",
    "grant_types": ["client_credentials"],
    "scope": "netbox:read netbox:write"
  },
  {
    "client_id": "netbox-to-diode",
    "client_secret": "${NETBOX_TO_DIODE_SECRET}",
    "grant_types": ["client_credentials"],
    "scope": "diode:read diode:write"
  }
]
EOF
else
  echo "Using existing $CREDS"
  INGEST_SECRET="$(jq -r '.[] | select(.client_id=="diode-ingest") | .client_secret' "$CREDS")"
  TO_NETBOX_SECRET="$(jq -r '.[] | select(.client_id=="diode-to-netbox") | .client_secret' "$CREDS")"
  NETBOX_TO_DIODE_SECRET="$(jq -r '.[] | select(.client_id=="netbox-to-diode") | .client_secret' "$CREDS")"
fi

# Secret file consumed by the Diode NetBox plugin (default path).
printf '%s' "$NETBOX_TO_DIODE_SECRET" >"$NETBOX_SECRETS/netbox_to_diode"
chmod 600 "$NETBOX_SECRETS/netbox_to_diode"

# Diode server .env (same-network NetBox URL).
# Preserve an existing file so re-running setup does not rotate DB/Redis/Hydra
# passwords out from under live volumes.
DIODE_ENV="$DIODE/.env"
if [[ ! -f "$DIODE_ENV" ]]; then
  echo "Generating Diode env at $DIODE_ENV ..."
  REDIS_PASSWORD="$(generate_secret)"
  POSTGRES_PASSWORD="$(generate_secret)"
  DIODE_PG_PASSWORD="$(generate_secret)"
  HYDRA_PG_PASSWORD="$(generate_secret)"
  HYDRA_SYSTEM_SECRET="$(generate_secret)"

  cat >"$DIODE_ENV" <<EOF
DIODE_NGINX_PORT=8080
REDIS_PASSWORD=${REDIS_PASSWORD}
REDIS_HOST=redis
REDIS_PORT=6378
REDIS_USERNAME=
LOGGING_LEVEL=INFO
LOGGING_FORMAT=json
SENTRY_DSN=
MIGRATION_ENABLED=true
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
DIODE_POSTGRES_DB_NAME=diode
DIODE_POSTGRES_USER=diode
DIODE_POSTGRES_PASSWORD=${DIODE_PG_PASSWORD}
TELEMETRY_ENVIRONMENT=dev
TELEMETRY_METRICS_EXPORTER=none
TELEMETRY_TRACES_EXPORTER=none
HYDRA_POSTGRES_DB_NAME=hydra
HYDRA_POSTGRES_USER=hydra
HYDRA_POSTGRES_PASSWORD=${HYDRA_PG_PASSWORD}
HYDRA_STRATEGIES_ACCESS_TOKEN=jwt
HYDRA_STRATEGIES_REFRESH_TOKEN=jwt
HYDRA_STRATEGIES_JWT_SCOPE_CLAIM=both
HYDRA_TTL_ACCESS_TOKEN=1h
HYDRA_OIDC_SUBJECT_IDENTIFIERS_SUPPORTED_TYPES=public
HYDRA_URLS_SELF_ISSUER=http://hydra:4444
HYDRA_SECRETS_SYSTEM_0=${HYDRA_SYSTEM_SECRET}
AUTH_HTTP_PORT=8080
OAUTH2_PUBLIC_SERVER_URL=http://hydra:4444
OAUTH2_ADMIN_SERVER_URL=http://hydra:4445
DIODE_AUTH_TOKEN_URL=http://diode-auth:8080/token
DIODE_TO_NETBOX_CLIENT_ID=diode-to-netbox
DIODE_TO_NETBOX_CLIENT_SECRET=${TO_NETBOX_SECRET}
DIODE_TO_NETBOX_RATE_LIMITER_RPS=20
DIODE_TO_NETBOX_RATE_LIMITER_BURST=1
NETBOX_DIODE_PLUGIN_API_BASE_URL=http://netbox:8080/api/plugins/diode
NETBOX_DIODE_PLUGIN_API_TIMEOUT_SECONDS=30
NETBOX_DIODE_PLUGIN_SKIP_TLS_VERIFY=true
ENABLE_GRAPH_DB=false
INGESTION_LOG_PROCESSOR_BATCH_SIZE=50
INGESTION_LOG_PROCESSOR_CONCURRENCY=1
AUTO_APPLY_PROCESSOR_BATCH_SIZE=50
AUTO_APPLY_PROCESSOR_CONCURRENCY=1
EOF
  chmod 600 "$DIODE_ENV"
else
  echo "Using existing $DIODE_ENV"
  # Keep OAuth client secret in sync if credentials were rotated.
  if grep -q '^DIODE_TO_NETBOX_CLIENT_SECRET=' "$DIODE_ENV"; then
    tmp="$(mktemp)"
    awk -v secret="$TO_NETBOX_SECRET" '
      /^DIODE_TO_NETBOX_CLIENT_SECRET=/ {print "DIODE_TO_NETBOX_CLIENT_SECRET=" secret; next}
      {print}
    ' "$DIODE_ENV" >"$tmp"
    mv "$tmp" "$DIODE_ENV"
    chmod 600 "$DIODE_ENV"
  fi
fi

# Env for host-side orb-agent / workspace.
# Prefer the NetBox SUPERUSER_API_TOKEN until create-netbox-token.sh mints a v1 token.
# Preserve an existing .env.local (create-netbox-token.sh may have replaced the token).
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
  # Keep ingest secret aligned with oauth credentials file.
  tmp="$(mktemp)"
  awk -v secret="$INGEST_SECRET" '
    /^DIODE_CLIENT_SECRET=/ {print "DIODE_CLIENT_SECRET=" secret; next}
    {print}
  ' "$DEV/.env.local" >"$tmp"
  mv "$tmp" "$DEV/.env.local"
  chmod 600 "$DEV/.env.local"
fi

# Agent policy pointed at local Diode (host network / published ports).
# Preserve an existing file so re-running setup does not flip BOOTSTRAP back
# to true after the operator has disabled it.
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
          # dry_run: true
          # dry_run_output_dir: /opt/orb
  policies:
    worker:
      extreme_platformone_worker:
        config:
          package: orb_extreme_platformone
          # omit schedule to run once
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

# workers.txt install path for a repo-root mount at /opt/orb
cat >"$DEV/workers.local.txt" <<EOF
.
EOF

cat <<EOF

Setup complete.

  docker compose -f "$DEV/docker-compose.yml" up -d --build
  ./.devcontainer/create-netbox-token.sh   # once NetBox is healthy

NetBox http://localhost:8000 (admin/admin)  Diode grpc://localhost:8080/diode
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
