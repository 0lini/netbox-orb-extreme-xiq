# Local NetBox + Diode test environment

This stack runs **NetBox** (with the Diode plugin) and the **Diode server** via
`docker compose`, on one shared network, for exercising this Orb worker against
a real ingest path.

Committed NetBox/Postgres/Redis env under `dev/netbox/env/` matches
[netbox-docker](https://github.com/netbox-community/netbox-docker) **release**
defaults (`env/netbox.env`, `postgres.env`, `redis.env`, `redis-cache.env`).
Only service hostnames are adjusted (`netbox-postgres` / `netbox-redis` /
`netbox-redis-cache`) so they do not clash with Diode's own Postgres/Redis on
the same compose network. Upstream ships `SKIP_SUPERUSER=true` (no
`SUPERUSER_*` / `SUPERUSER_API_TOKEN`).

## Prerequisites

- `docker` / Podman with the docker CLI shim (`podman-docker`)
- `docker compose` (e.g. Homebrew `docker-compose`)
- `jq`, `curl`

On Bazzite (rootless Podman) this stack already works with the
`dev/docker-compose.yml` overrides for Diode Postgres init.

On this Bazzite host that is already set up as:

```bash
export PATH="$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:$PATH"
```

## Quick start (host)

```bash
./dev/setup.sh
docker compose -f dev/docker-compose.yml up -d --build
```

| Service | URL |
|---------|-----|
| NetBox UI | http://localhost:8000 |
| Diode gRPC | `grpc://localhost:8080/diode` |

`setup.sh` generates Diode OAuth / server env and `agent.local.yaml` into
gitignored files (`dev/.env.local`, `dev/diode/.env`, OAuth
`client-credentials.json`, `dev/netbox/secrets/`). NetBox compose env under
`dev/netbox/env/*.env` is already committed (upstream defaults), so
`compose up` works without inventing secrets first.

After NetBox is up, create a local admin user and mint a REST API token for
bootstrap (also enables UI login `admin` / `admin` unless you override
`NETBOX_ADMIN_PASSWORD`):

```bash
./dev/create-netbox-token.sh
```

### Run the Orb agent against the stack

```bash
set -a; source dev/.env.local; set +a
export PLATFORMONE_API_TOKEN=...   # your Platform ONE token

docker run --rm --network host \
  -v "$PWD:/opt/orb/" \
  -e INSTALL_WORKERS_PATH=/opt/orb/dev/workers.local.txt \
  -e DIODE_CLIENT_ID -e DIODE_CLIENT_SECRET \
  -e PLATFORMONE_API_TOKEN \
  -e NETBOX_API_URL -e NETBOX_API_TOKEN \
  netboxlabs/orb-agent:latest run -c /opt/orb/dev/agent.local.yaml
```

First run keeps `BOOTSTRAP: true` in `dev/agent.local.yaml` so custom fields and
tags are created. Set it to `false` afterward.

## Dev Container

1. Run `./dev/setup.sh` once on the host (creates Diode OAuth secrets before compose starts).
2. Command Palette → **Dev Containers: Reopen in Container**.

That uses `.devcontainer/devcontainer.json`, which starts the same
`dev/docker-compose.yml` and attaches to the `workspace` service. Inside the
container, NetBox is `http://netbox:8080` and Diode is
`grpc://ingress-nginx:80/diode`.

## Layout

| Path | Role |
|------|------|
| `dev/docker-compose.yml` | NetBox services + includes Diode compose + workspace |
| `dev/diode/` | Upstream Diode server compose + nginx |
| `dev/netbox/` | NetBox image with `netboxlabs-diode-netbox-plugin` |
| `dev/setup.sh` | Generates Diode OAuth + `agent.local.yaml` (not NetBox env) |
| `dev/netbox/env/*.env` | Upstream netbox-docker defaults (hostnames remapped) |
| `.devcontainer/` | VS Code / Cursor Dev Container definition |

## Tear down

```bash
docker compose -f dev/docker-compose.yml down -v
```
