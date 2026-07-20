#!/usr/bin/env bash
# Tear down the local NetBox/Diode/Dev Container compose project and optionally
# wipe its Docker volumes. Does not touch unrelated Docker projects.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV="$ROOT/.devcontainer"
COMPOSE=(docker compose --project-name orb-platformone-test -f "$DEV/docker-compose.yml")
WIPE_VOLUMES=false
WIPE_SECRETS=false

usage() {
  cat <<EOF
Usage: $0 [--volumes] [--secrets]

  (default)   Stop/remove project containers and orphans; keep volumes + secrets.
  --volumes   Also delete this project's Docker volumes (NetBox + Diode data).
  --secrets   Also remove generated Diode/NetBox env secrets under .devcontainer/
              (next setup.sh will re-mint them).

Examples:
  bash ./.devcontainer/cleanup.sh
  bash ./.devcontainer/cleanup.sh --volumes --secrets
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --volumes) WIPE_VOLUMES=true ;;
    --secrets) WIPE_SECRETS=true ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi

echo "Stopping compose project orb-platformone-test ..."
# Compose config needs diode/.env when present; if secrets are already gone,
# fall back to removing containers by project name/label.
if [[ -f "$DEV/diode/.env" ]]; then
  "${COMPOSE[@]}" --profile '*' down --remove-orphans || true
else
  echo "diode/.env missing — removing containers by project name"
  docker ps -aq --filter "name=orb-platformone-test-" | xargs -r docker rm -f
fi

if [[ "$WIPE_VOLUMES" == true ]]; then
  echo "Removing project volumes ..."
  docker volume ls -q | grep -E '^orb-platformone-test_' | while read -r v; do
    [[ -n "$v" ]] || continue
    echo "  $v"
    docker volume rm "$v" >/dev/null 2>&1 || docker volume rm -f "$v" >/dev/null || true
  done
  # Older leftover names from partial runs
  docker volume ls -q | grep -E '(^|_)diode-(postgres|redis)-data$' | while read -r v; do
    [[ -n "$v" ]] || continue
    echo "  $v"
    docker volume rm "$v" >/dev/null 2>&1 || docker volume rm -f "$v" >/dev/null || true
  done
fi

if [[ "$WIPE_SECRETS" == true ]]; then
  echo "Removing generated secrets under .devcontainer/ ..."
  rm -f "$DEV/.env.local" "$DEV/agent.local.yaml" "$DEV/setup.log"
  rm -f "$DEV/netbox/env/netbox.env" "$DEV/netbox/env/postgres.env"
  rm -f "$DEV/netbox/env/redis.env" "$DEV/netbox/env/redis-cache.env"
  rm -f "$DEV/netbox/secrets/netbox_to_diode"
  # Keep diode/README.md; drop quickstart outputs (gitignored).
  if [[ -d "$DEV/diode" ]]; then
    find "$DEV/diode" -mindepth 1 -maxdepth 1 ! -name README.md -exec rm -rf {} +
  fi
fi

echo
echo "Cleanup done."
if command -v ss >/dev/null 2>&1; then
  for port in 8000 8080; do
    if ss -ltn "sport = :$port" 2>/dev/null | grep -q ":$port"; then
      echo "Warning: host port $port is still in use (NetBox/Diode publish will fail):"
      ss -ltnp "sport = :$port" 2>/dev/null || ss -ltn "sport = :$port" 2>/dev/null || true
    fi
  done
fi
echo "  bash ./.devcontainer/setup.sh"
echo "  docker compose -f .devcontainer/docker-compose.yml --profile '*' up -d --build"
echo "Then: Dev Containers: Rebuild Container"
