# Security

Security posture for **netbox-orb-extreme-platformone**: the Extreme Platform ONE
→ NetBox Orb worker.

Report vulnerabilities privately to the repository maintainers. Do not open a
public issue with live credentials, inventory exports, or exploit details.

---

## Scope

| Surface | Role |
|---------|------|
| `orb_extreme_platformone/` | Production worker: Platform ONE HTTP client, NetBox bootstrap REST, Diode entity mapping |
| `agent.yaml` / `workers.txt` | Orb Agent policy + install path (env-substituted secrets) |
| `.github/workflows/ci.yml` | Lint + pytest on PRs |

---

## Trust model

1. **Operator-controlled endpoints.** `PLATFORMONE_API_URL` and `NETBOX_API_URL`
   are intentional “call this API” knobs. Treat them like credentials: a
   malicious URL is SSRF by design for an API client.
2. **Secrets live in the environment**, not in git. Policy YAML may reference
   `${VAR}` placeholders; Orb substitutes at runtime.
3. **Bootstrap is one-shot and privileged.** `BOOTSTRAP: true` uses a NetBox
   token that can create/update custom fields and tags. Scheduled runs should
   keep `BOOTSTRAP: false` and drop `NETBOX_API_*`.

---

## What we harden (worker)

### HTTPS and URL hygiene

`orb_extreme_platformone.urls.require_https_url` gates every outbound base URL:

- Remote hosts require `https://`.
- Plaintext `http://` is allowed only for loopback, `*.local`, and the
  Docker hostname `netbox` (local lab NetBox on a Docker network).
- Userinfo is rejected (`https://legit@evil.com`, `user:pass@host`) to block
  URL-confusion credential theft.
- Query strings and fragments are rejected.

### HTTP client

- TLS certificate verification left at requests defaults (`verify=True`).
- Timeouts on all Platform ONE and NetBox calls.
- Redirects disabled (`allow_redirects=False`) so a `307` cannot replay a
  login password body or carry a NetBox token off-origin.
- Platform ONE error bodies are truncated (`truncate_error_body`, 200 chars)
  before they enter exceptions/logs.

### Bootstrap fail-closed

When policy `BOOTSTRAP` is true but `NETBOX_API_URL` / `NETBOX_API_TOKEN` are
missing, the worker **raises** instead of silently skipping schema setup.

### No classic injection sinks

The package does not use `subprocess`, `shell=True`, `eval`/`exec`, `pickle`,
or unsafe YAML load. ConfigState table names come from fixed catalogs, not
from free-form policy strings.

### CI

`.github/workflows/ci.yml` sets `permissions: contents: read`, uses
`pull_request` (not `pull_request_target`), and runs only lint/tests.

---

## Credential handling

| Secret | Where it should live | Notes |
|--------|----------------------|-------|
| `PLATFORMONE_API_TOKEN` or username/password | Agent env / `.env` (gitignored) | Prefer API token over long-lived password when available |
| `DIODE_CLIENT_ID` / `DIODE_CLIENT_SECRET` | Agent env | Diode OAuth client-credentials |
| `NETBOX_API_TOKEN` | Env **only while** `BOOTSTRAP: true` | Least privilege: custom fields + tags; not a permanent superuser token |

**Do not** commit dry-run JSON, inventory exports, or `.env` files. Dry-run
output can contain hostnames, serials, MACs, and management IPs.

Policy config currently wins over environment when both are set
(`_cfg_or_env`). Prefer leaving secret values out of checked-in YAML and
supplying them only via the environment so Orb policy objects never carry
plaintext secrets.

---

## Historical notes

- Inventory data once landed in git via a committed-then-deleted `test.json`
  (purged from branch history; older PR refs may still hold copies — treat as
  sensitive and request GitHub removal if needed).
- An earlier optional local NetBox/Diode Dev Container stack under
  `.devcontainer/` used shared demo credentials in committed env examples;
  that stack has been removed. Do not reuse historical values.

---

## Residual risks / backlog

Ordered by practical impact:

1. **Pin** `orb-agent` and worker dependency images/tags (digest or immutable
   tag) for reproducible deploys.
2. Add a **lockfile** (or exact pins) for production worker installs; current
   `pyproject.toml` uses lower bounds only.
3. Prefer **env-only secrets** in Orb policy (stop winning from `config:` for
   credential keys) if Orb can guarantee env substitution without YAML values.

---

## Operator checklist

- [ ] Secrets only in env / secret store — never in git or screenshots
- [ ] `BOOTSTRAP: false` on all scheduled runs; NetBox bootstrap token removed
- [ ] `PLATFORMONE_API_URL` / `NETBOX_API_URL` point at expected HTTPS hosts
- [ ] Dry-run / inventory output kept out of the repo
