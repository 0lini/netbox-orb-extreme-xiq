#!/usr/bin/env bash
# Create a NetBox API token for admin and store it in .devcontainer/.env.local
#
# Uses a v1 token so existing clients (Orb bootstrap, curl with
# `Authorization: Token …`) keep working. NetBox 4.6 defaults to v2 Bearer tokens.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_LOCAL="$ROOT/.devcontainer/.env.local"

if [[ ! -f "$ENV_LOCAL" ]]; then
  echo "Run ./.devcontainer/setup.sh first" >&2
  exit 1
fi

# Prefer compose service name over a hard-coded container name.
if [[ -n "${NETBOX_CONTAINER:-}" ]]; then
  CONTAINER="$NETBOX_CONTAINER"
else
  CONTAINER="$(
    docker compose -f "$ROOT/.devcontainer/docker-compose.yml" ps -q netbox 2>/dev/null | head -n1
  )"
  if [[ -z "$CONTAINER" ]]; then
    CONTAINER="orb-platformone-test-netbox-1"
  fi
fi

STATUS_FILE="$(mktemp)"
trap 'rm -f "$STATUS_FILE"' EXIT

# manage.py shell prints config banners to stdout; bracket the token so we can
# extract it cleanly (never write plaintext into the container FS).
RAW="$(
  docker exec -i "$CONTAINER" /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell <<'PY'
from django.contrib.auth import get_user_model
from users.models import Token

u = get_user_model().objects.get(username="admin")
Token.objects.filter(user=u, description="local-dev").delete()
# Explicit v1 so Authorization: Token <plaintext> works with Orb bootstrap.
t = Token(user=u, description="local-dev", version=1)
t.save()
print("__ORB_NETBOX_TOKEN_BEGIN__")
print(t.plaintext or t.token or "")
print("__ORB_NETBOX_TOKEN_END__")
PY
)"

TOKEN="$(
  printf '%s\n' "$RAW" | awk '
    $0 == "__ORB_NETBOX_TOKEN_BEGIN__" {grab=1; next}
    $0 == "__ORB_NETBOX_TOKEN_END__" {grab=0; next}
    grab {print}
  ' | tr -d '\r\n[:space:]'
)"

if [[ ${#TOKEN} -lt 20 || "$TOKEN" == *"loaded config"* ]]; then
  echo "Failed to create clean token (len=${#TOKEN})" >&2
  exit 1
fi

tmp="$(mktemp)"
awk -v tok="$TOKEN" '
  BEGIN{done=0}
  /^NETBOX_API_TOKEN=/ {print "NETBOX_API_TOKEN=" tok; done=1; next}
  {print}
  END{if(!done) print "NETBOX_API_TOKEN=" tok}
' "$ENV_LOCAL" >"$tmp"
mv "$tmp" "$ENV_LOCAL"
chmod 600 "$ENV_LOCAL"

code="$(
  curl -s -o "$STATUS_FILE" -w '%{http_code}' \
    -H "Authorization: Token ${TOKEN}" \
    http://127.0.0.1:8000/api/status/ || true
)"
echo "Updated NETBOX_API_TOKEN in $ENV_LOCAL (len=${#TOKEN})"
echo "API status HTTP $code"
if [[ "$code" != "200" ]]; then
  echo "NetBox API token check failed" >&2
  head -c 400 "$STATUS_FILE" >&2 || true
  echo >&2
  exit 1
fi
if command -v jq >/dev/null; then
  jq -c '{netbox: .["netbox-version"], plugins: (.plugins|keys? // empty)}' "$STATUS_FILE" 2>/dev/null || true
fi
