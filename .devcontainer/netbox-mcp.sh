#!/usr/bin/env bash
# Launch the NetBox Labs read-only MCP server with local Orb stack credentials.
# Maps NETBOX_API_* (.devcontainer/.env.local) to the NETBOX_URL / NETBOX_TOKEN
# names the server expects. No secrets are embedded here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${NETBOX_MCP_ENV_FILE:-$ROOT/.devcontainer/.env.local}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# Prefer an explicit NETBOX_URL. Resolve in order: compose DNS `netbox`,
# host.docker.internal (Dev Container → host-published stack), then .env.local.
if [[ -z "${NETBOX_URL:-}" ]]; then
  if getent hosts netbox >/dev/null 2>&1; then
    NETBOX_URL="http://netbox:8080"
  elif getent hosts host.docker.internal >/dev/null 2>&1; then
    NETBOX_URL="http://host.docker.internal:8000"
  else
    NETBOX_URL="${NETBOX_API_URL:-http://localhost:8000}"
  fi
fi
export NETBOX_URL
export NETBOX_TOKEN="${NETBOX_TOKEN:-${NETBOX_API_TOKEN:-}}"

if [[ -z "${NETBOX_TOKEN}" ]]; then
  echo "netbox-mcp: set NETBOX_API_TOKEN (e.g. source .devcontainer/.env.local) or NETBOX_TOKEN" >&2
  exit 1
fi

UVX="${UVX:-uvx}"
if ! command -v "$UVX" >/dev/null 2>&1; then
  if [[ -x "${HOME}/.local/bin/uvx" ]]; then
    UVX="${HOME}/.local/bin/uvx"
  else
    echo "netbox-mcp: uvx not found; install uv (https://docs.astral.sh/uv/)" >&2
    exit 1
  fi
fi

# Pin a released tag so first-run installs stay reproducible.
NETBOX_MCP_REF="${NETBOX_MCP_REF:-v1.2.1}"
exec "$UVX" --from "git+https://github.com/netboxlabs/netbox-mcp-server@${NETBOX_MCP_REF}" \
  netbox-mcp-server "$@"
