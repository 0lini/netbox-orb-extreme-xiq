# orb-extreme-xiq

An ExtremeCloud IQ (XIQ) discovery worker for the NetBox Labs **Orb Agent**. It
pulls inventory from the XIQ cloud API and ingests it into NetBox via **Diode**,
mirroring the proprietary `nbl_cisco_meraki` integration — but this one is yours
and runs on the free, open-source `netboxlabs/orb-agent:latest` image. No Orb
Agent Pro / private registry required.

```
XIQ cloud API ─► orb_extreme_xiq (collector + mapper) ─► Diode ─► NetBox (+Assurance if licensed)
```

## Assurance-ready by design

Assurance is a **consumer-side** feature: there's no separate API to code
against. Any source that ingests via Diode surfaces as deviations once an
Assurance license is enabled — with **zero code changes** to this worker. So
"future-proofing" just means producing clean, stable Diode output, which this
scaffold does deliberately:

- **Field authority** (`mapper.DEFAULT_AUTHORITY`, overridable in policy config).
  The worker emits *only* fields XIQ owns, so human-owned fields (rack, tenant,
  description) can never generate phantom drift. `site` is XIQ-owned by default
  (Meraki-style); drop it from authority to let humans own it after create.
- **Stable identity** (`identity.py`). Deterministic device names + an immutable
  `xiq_device_id` custom field so a device is always correlatable even if its
  display name changes.
- **Meraki-style site assignment.** Site is asserted every run from an explicit
  `location_site_mapping` (+ `default_site`); many XIQ locations can consolidate
  into one NetBox site, and each site records its XIQ locations in the
  `xiq_locations` custom field (Meraki does this with `meraki_networks`).
- **Bootstrap step** (`bootstrap.py`). Idempotently creates the custom-field
  definitions + `source:xiq` tag before the first sync — the same first-run
  pattern the official integrations use.
- **Stable producer + tags.** Fixed `app_name="orb-extreme-xiq"` and a
  `source:xiq` tag keep your XIQ data cleanly attributable/filterable in
  Assurance.

## Layout

- `client.py` — thin XIQ REST client (token or user/pass, paginated `/devices`, `/locations`).
- `identity.py` — stable device naming + Meraki-style site resolution.
- `mapper.py` — XIQ → Diode entities with field-authority enforcement, custom fields, tags.
- `bootstrap.py` — one-time idempotent NetBox schema setup (custom fields + tag).
- `backend.py` — worker entrypoint + standalone runner.
- `agent.yaml` — example policy (bootstrap, site mapping, field authority).
- `test_mapping.py` — offline tests for the mapping logic (no SDK needed).

## First-run flow (mirrors Meraki/ACI)

1. Set `BOOTSTRAP: true` and provide `NETBOX_API_URL` + `NETBOX_API_TOKEN`
   (one-time, to create the custom fields + tag). Run once.
2. Set `BOOTSTRAP: false` for all scheduled runs afterward.

Bootstrap uses the NetBox REST API because field *definitions* are schema and
that path works regardless of Diode SDK version. If your SDK exposes a
CustomField ingest entity you can move it into the Diode path to match Meraki
exactly. Bootstrap **skips gracefully** if no NetBox token is set.

## Develop without the full agent

```bash
pip install -e ".[dev]"
export XIQ_API_TOKEN=...                 # or XIQ_USERNAME / XIQ_PASSWORD
python -m orb_extreme_xiq.backend        # DRY RUN: fetches from XIQ, maps, prints entities (no Diode push)
python test_mapping.py                   # offline logic tests (stubbed SDK, no network)
```

The Orb Agent worker (`netboxlabs-orb-worker`) owns the Diode client and the
actual ingest entirely — `Backend.run()` only ever *produces* entities. There
is no dev-mode "push to Diode" path here by design; run it inside the real
`orb-agent` container (see `agent.yaml`) to actually ingest.

## Worker Backend contract (verified against `netboxlabs-orb-worker` 1.16.0)

`backend.Backend` subclasses `worker.backend.Backend` and implements:

- `describe()` (classmethod) — returns `Metadata(name, app_name, app_version, description)`
  so the worker can identify the backend before constructing it.
- `run(self, policy_name, policy, **kwargs) -> Iterable[Entity]` — does the
  XIQ fetch + mapping and returns the Diode entities for one tick; the
  PolicyRunner handles scheduling, chunking, and the Diode client itself.

If you're on a different `netboxlabs-orb-worker` version, re-check this
against the installed package: `python -c "import worker.backend as b, inspect; print(inspect.getsource(b.Backend))"`.

## The one thing to keep VERIFIED against your installed Diode SDK

`mapper._device_kwargs()` / `_device_custom_fields()` funnel `custom_fields=`
and `tags=` through one place. As of `netboxlabs-diode-sdk` (generated from
NetBox v4.6.0), `custom_fields` values must be wrapped —
`CustomFieldValue(text=...)` for the text fields, `CustomFieldValue(json=...)`
for `xiq_locations` — a plain string raises `ValueError`. Confirm this still
holds for your SDK version: `python -c "import netboxlabs.diode.sdk.ingester as i; help(i.Device)"`.

## XIQ auth

- **API token** (recommended): XIQ UI → Global Settings → API Token Management.
- **Username/password**: `POST /login` → JWT; the client auto-refreshes.
- Base URL: `https://api.extremecloudiq.com`.

## Config knobs (policy `config:`)

| key | meaning |
|-----|---------|
| `BOOTSTRAP` | run schema setup before sync (first run only) |
| `location_site_mapping` | XIQ location name → NetBox site name (consolidation) |
| `default_site` | site for unmapped locations |
| `name_source` | `hostname` (default) or `serial` |
| `field_authority` / `_add` / `_remove` | what XIQ owns (= what Assurance flags) |
| `scope.sites` | limit sync to specific resolved sites |

## Roadmap

- Switch ports → Interface entities (per-device port endpoint).
- VLANs / Prefixes from network policies.
- Move bootstrap custom-field creation to Diode if your SDK supports it.
