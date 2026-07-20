#!/usr/bin/env bash
# Tear down the local NetBox/Diode/Dev Container compose project and optionally
# wipe its Docker volumes/secrets/images. Does not touch unrelated projects
# unless --all is used for this project's artifacts only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV="$ROOT/.devcontainer"
PROJECT=orb-platformone-test
COMPOSE=(docker compose --project-name "$PROJECT" -f "$DEV/docker-compose.yml")
WIPE_VOLUMES=false
WIPE_SECRETS=false
WIPE_IMAGES=false

usage() {
  cat <<EOF
Usage: $0 [--volumes] [--secrets] [--images] [--all]

  (default)   Stop/remove project containers and orphans; keep volumes + secrets.
  --volumes   Delete this project's Docker volumes (NetBox + Diode data).
  --secrets   Remove generated Diode/NetBox env secrets under .devcontainer/.
  --images    Remove locally built project images (netbox/workspace).
  --all       Shorthand for --volumes --secrets --images (full local reset).

Examples:
  bash ./.devcontainer/cleanup.sh --all
  bash ./.devcontainer/cleanup.sh --volumes --secrets
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --volumes) WIPE_VOLUMES=true ;;
    --secrets) WIPE_SECRETS=true ;;
    --images) WIPE_IMAGES=true ;;
    --all)
      WIPE_VOLUMES=true
      WIPE_SECRETS=true
      WIPE_IMAGES=true
      ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi

echo "Stopping compose project $PROJECT ..."
# Compose config needs diode/.env when present; if secrets are already gone,
# fall back to removing containers by project name.
if [[ -f "$DEV/diode/.env" ]]; then
  "${COMPOSE[@]}" --profile '*' down --remove-orphans || true
else
  echo "diode/.env missing — removing containers by project name"
fi
docker ps -aq --filter "name=${PROJECT}-" | xargs -r docker rm -f
# Anything still publishing NetBox/Diode host ports
for port in 8000 8080; do
  docker ps -aq --filter "publish=$port" | xargs -r docker rm -f
done

if [[ "$WIPE_VOLUMES" == true ]]; then
  echo "Removing project volumes ..."
  docker volume ls -q | grep -E "^${PROJECT}_" | while read -r v; do
    [[ -n "$v" ]] || continue
    echo "  $v"
    docker volume rm "$v" >/dev/null 2>&1 || docker volume rm -f "$v" >/dev/null || true
  done
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
  if [[ -d "$DEV/diode" ]]; then
    find "$DEV/diode" -mindepth 1 -maxdepth 1 ! -name README.md -exec rm -rf {} +
  fi
fi

if [[ "$WIPE_IMAGES" == true ]]; then
  echo "Removing locally built project images ..."
  docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
    | grep -E 'orb-platformone-(netbox|test-workspace)|^orb-platformone-netbox' \
    | awk '{print $2}' | sort -u | xargs -r docker rmi -f || true
fi

# Drop dangling project network if still present
docker network ls --format '{{.Name}}' | grep -E "^${PROJECT}" | xargs -r docker network rm 2>/dev/null || true

echo
echo "Cleanup done."
if command -v ss >/dev/null 2>&1; then
  for port in 8000 8080; do
    if ss -ltn "sport = :$port" 2>/dev/null | grep -q ":$port"; then
      echo "Warning: host port $port is still in use:"
      ss -ltnp "sport = :$port" 2>/dev/null || ss -ltn "sport = :$port" 2>/dev/null || true
    fi
  done
fi
echo "  bash ./.devcontainer/setup.sh"
echo "  docker compose -f .devcontainer/docker-compose.yml --profile '*' up -d --build"
echo "Then: Dev Containers: Rebuild Container"
