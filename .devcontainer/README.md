# Local NetBox + Diode test environment

This stack runs **NetBox** (with the Diode plugin) and the **Diode server** via
`docker compose`, on one shared network, for exercising this Orb worker against
a real ingest path. Everything lives under `.devcontainer/` (Home Assistant–style:
one place for the Dev Container and the optional local services).

## Prerequisites

- `docker` / Podman with the docker CLI shim (`podman-docker`)
- `docker compose` (e.g. Homebrew `docker-compose`)
- `jq`, `curl`

On Bazzite (rootless Podman) this stack already works with the
`.devcontainer/docker-compose.yml` overrides for Diode Postgres init.

On this Bazzite host that is already set up as:

```bash
export PATH="$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:$PATH"
```

## Quick start (host)

```bash
./.devcontainer/setup.sh
docker compose -f .devcontainer/docker-compose.yml up -d --build
```

| Service | URL |
|---------|-----|
| NetBox UI | http://localhost:8000 (`admin` / `admin`) |
| Diode gRPC | `grpc://localhost:8080/diode` |

Published ports bind to **127.0.0.1 only** so the demo NetBox (`admin`/`admin`)
and Diode ingress are not reachable from other hosts on the LAN.

Generated secrets and agent env land in gitignored files
(`.devcontainer/.env.local`, `.devcontainer/diode/.env`,
`.devcontainer/netbox/env/*.env`). Templates live next to them as
`*.env.example`. Re-running `./.devcontainer/setup.sh` keeps existing secret
files so DB/Redis passwords stay aligned with volumes.

After NetBox is up, mint a REST API token for bootstrap:

```bash
./.devcontainer/create-netbox-token.sh
```

### Run the Orb agent against the stack

```bash
set -a; source .devcontainer/.env.local; set +a
export PLATFORMONE_API_TOKEN=...   # your Platform ONE token

docker run --rm --network host \
  -v "$PWD:/opt/orb/" \
  -e INSTALL_WORKERS_PATH=/opt/orb/.devcontainer/workers.local.txt \
  -e DIODE_CLIENT_ID -e DIODE_CLIENT_SECRET \
  -e PLATFORMONE_API_TOKEN \
  -e NETBOX_API_URL -e NETBOX_API_TOKEN \
  netboxlabs/orb-agent:latest run -c /opt/orb/.devcontainer/agent.local.yaml
```

First run keeps `BOOTSTRAP: true` in `.devcontainer/agent.local.yaml` so custom
fields and tags are created. Set it to `false` afterward.

## Dev Container

1. Command Palette → **Dev Containers: Reopen in Container**.
2. `initializeCommand` runs `./.devcontainer/setup.sh` on the host before
   compose starts (idempotent: existing secret files are kept).

That uses `.devcontainer/devcontainer.json`, which starts
`.devcontainer/docker-compose.yml` and attaches to the `workspace` service
(`runServices: ["workspace"]`; compose `depends_on` brings up NetBox, Diode,
and `netbox-worker`). Image tags stay unpinned — rebuild with compose when the
Dockerfile changes. Inside the container, NetBox is `http://netbox:8080` and
Diode is `grpc://ingress-nginx:80/diode`.

### NetBox MCP (read-only)

After minting a token (`./.devcontainer/create-netbox-token.sh`), Cursor can
talk to the local NetBox via `.cursor/mcp.json` →
`.devcontainer/netbox-mcp.sh`. The script reads `NETBOX_API_TOKEN` from
`.devcontainer/.env.local` (never committed) and picks `http://netbox:8080`
inside the compose network or `http://localhost:8000` on the host. Requires
`uv` / `uvx` (installed in the workspace image).

## Layout

| Path | Role |
|------|------|
| `.devcontainer/docker-compose.yml` | NetBox services + includes Diode compose + workspace |
| `.devcontainer/diode/` | Upstream Diode server compose + nginx |
| `.devcontainer/netbox/` | NetBox image with `netboxlabs-diode-netbox-plugin` |
| `.devcontainer/setup.sh` | Generates OAuth + NetBox env secrets and `agent.local.yaml` |
| `.devcontainer/netbox-mcp.sh` | Wrapper for NetBox Labs MCP (token from `.env.local`) |
| `.devcontainer/netbox/env/*.env.example` | Templates; real `*.env` files are generated and gitignored |
| `.devcontainer/devcontainer.json` | VS Code / Cursor Dev Container definition |
| `.cursor/mcp.json` | Cursor MCP server entry for local NetBox |

## Tear down

```bash
docker compose -f .devcontainer/docker-compose.yml down -v
```
