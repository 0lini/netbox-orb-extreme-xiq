# Dev Container + optional NetBox/Diode stack

## Dev Container (default)

Reopen in a Dev Container for a Python workspace (`pip install -e '.[dev]'`).
It does **not** start NetBox or Diode.

## Local NetBox + Diode stack (optional E2E)

Run on the **host** (Docker/Podman) when you need a real ingest path:

```bash
./.devcontainer/setup.sh
docker compose -f .devcontainer/docker-compose.yml up -d --build
./.devcontainer/create-netbox-token.sh
```

| Service | URL |
|---------|-----|
| NetBox UI | http://localhost:8000 (`admin` / `admin`) |
| Diode gRPC | `grpc://localhost:8080/diode` |

Ports bind to **127.0.0.1** only. Secrets land in gitignored files under
`.devcontainer/` (`.env.local`, `diode/.env`, `netbox/env/*.env`).

### Orb agent against the stack

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

### From inside the Dev Container

With the stack up on the host, use `host.docker.internal` (the Dev Container
adds this host mapping):

- NetBox: `http://host.docker.internal:8000`
- Diode: `grpc://host.docker.internal:8080/diode`

### NetBox MCP

After `./.devcontainer/create-netbox-token.sh`, `.cursor/mcp.json` launches
`.devcontainer/netbox-mcp.sh` (reads `.env.local`).

### Tear down

```bash
docker compose -f .devcontainer/docker-compose.yml down -v
```
