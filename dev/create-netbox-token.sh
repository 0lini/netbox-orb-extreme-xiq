#!/usr/bin/env bash
# Create a NetBox API token for admin and store it in dev/.env.local
#
# Uses a v1 token so existing clients (Orb bootstrap, curl with
# `Authorization: Token …`) keep working. NetBox 4.6 defaults to v2 Bearer tokens.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_LOCAL="$ROOT/dev/.env.local"
CONTAINER="${NETBOX_CONTAINER:-orb-platformone-test-netbox-1}"

if [[ ! -f "$ENV_LOCAL" ]]; then
  echo "Run ./dev/setup.sh first" >&2
  exit 1
fi

TOKEN_FILE="$(mktemp)"
trap 'rm -f "$TOKEN_FILE"' EXIT

docker exec -i "$CONTAINER" /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell <<'PY'
from django.contrib.auth import get_user_model
from users.models import Token
from pathlib import Path

u = get_user_model().objects.get(username="admin")
Token.objects.filter(user=u, description="local-dev").delete()
# Explicit v1 so Authorization: Token <plaintext> works with Orb bootstrap.
t = Token(user=u, description="local-dev", version=1)
t.save()
Path("/tmp/nb_token").write_text(t.plaintext or t.token or "")
PY

docker cp "$CONTAINER:/tmp/nb_token" "$TOKEN_FILE"
docker exec "$CONTAINER" rm -f /tmp/nb_token

TOKEN="$(tr -d '\r\n' <"$TOKEN_FILE")"
if [[ ${#TOKEN} -lt 20 ]]; then
  echo "Failed to create token (len=${#TOKEN})" >&2
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

code="$(curl -s -o /tmp/nb-status.json -w '%{http_code}' -H "Authorization: Token ${TOKEN}" http://localhost:8000/api/status/ || true)"
echo "Updated NETBOX_API_TOKEN in $ENV_LOCAL (len=${#TOKEN})"
echo "API status HTTP $code"
if command -v jq >/dev/null; then
  jq -c '{netbox: .["netbox-version"], plugins: (.plugins|keys? // empty)}' /tmp/nb-status.json 2>/dev/null || true
fi
