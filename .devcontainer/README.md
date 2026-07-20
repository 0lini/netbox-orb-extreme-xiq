# Local NetBox + Diode + Dev Container

One compose stack under `.devcontainer/`: NetBox (with Diode plugin), Diode, and
a Python workspace. Codespaces / VS Code Dev Containers start it via
`devcontainer.json` (no Docker-in-Docker required).

## Quick start

**Dev Container / Codespaces:** reopen in container. `setup.sh` runs on the host
first; compose brings up NetBox, Diode, and the workspace.

**Host only:**

```bash
./.devcontainer/setup.sh
docker compose -f .devcontainer/docker-compose.yml up -d --build
./.devcontainer/create-netbox-token.sh   # after NetBox is up
```

| Service | URL |
|---------|-----|
| NetBox | http://localhost:8000 (`admin` / `admin`) |
| Diode | `grpc://localhost:8080/diode` |
| In workspace | NetBox `http://netbox:8080`, Diode `grpc://ingress-nginx:80/diode` |

Ports bind to **127.0.0.1** only. Secrets are gitignored under `.devcontainer/`.

## Orb agent

```bash
set -a; source .devcontainer/.env.local; set +a
export PLATFORMONE_API_TOKEN=...

docker run --rm --network host \
  -v "$PWD:/opt/orb/" \
  -e INSTALL_WORKERS_PATH=/opt/orb/.devcontainer/workers.local.txt \
  -e DIODE_CLIENT_ID -e DIODE_CLIENT_SECRET \
  -e PLATFORMONE_API_TOKEN \
  -e NETBOX_API_URL -e NETBOX_API_TOKEN \
  netboxlabs/orb-agent:latest run -c /opt/orb/.devcontainer/agent.local.yaml
```

Set `BOOTSTRAP: false` in `.devcontainer/agent.local.yaml` after the first run.

## Tear down

```bash
docker compose -f .devcontainer/docker-compose.yml down -v
```
