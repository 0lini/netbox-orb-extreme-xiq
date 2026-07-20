# Diode server (generated)

Do not vendor Diode compose here. `../setup.sh` downloads the official
quickstart and runs it in this directory:

```bash
curl -sSfLo quickstart.sh \
  https://raw.githubusercontent.com/netboxlabs/diode/release/diode-server/docker/scripts/quickstart.sh
chmod +x quickstart.sh
./quickstart.sh http://netbox:8080
```

That creates `docker-compose.yaml`, `.env`, `nginx/`, and
`oauth2/client/client-credentials.json` (all gitignored). The parent
`.devcontainer/docker-compose.yml` includes those files on the shared network
with NetBox.
