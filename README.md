# orb-extreme-xiq

[![CI](https://github.com/0lini/netbox-orb-extreme-xiq/actions/workflows/ci.yml/badge.svg)](https://github.com/0lini/netbox-orb-extreme-xiq/actions/workflows/ci.yml)

An **ExtremeCloud IQ (XIQ)** discovery worker for the NetBox Labs **Orb
Agent**. It pulls device, site, switch-port, and wireless inventory from the
XIQ cloud API and ingests it into NetBox via **Diode**, following the same
patterns as NetBox Labs' proprietary integrations (e.g. Cisco Meraki) — but it
runs on the free, open-source `netboxlabs/orb-agent` image. No Orb Agent Pro
or private registry required.

```
XIQ cloud API ─► orb_extreme_xiq (collector + mapper) ─► Diode ─► NetBox (+ Assurance if licensed)
```

## What gets synced

| XIQ data | NetBox objects |
|----------|----------------|
| Devices | `Device` — name, serial, status, device type + manufacturer, platform, description, primary IPv4, tags, `xiq_network_policy` custom field |
| Location hierarchy | `Site` (tree root) + nested `Location` chain (building → floor → …) |
| Switch wired ports | `Interface` — name, link state, speed, duplex, type (best-effort), description, `xiq_port_id` custom field |
| AP radios | `Interface` — name, type (802.11 standard), RF role, TX power, MAC, channel frequency/width |
| Broadcast SSIDs | `WirelessLAN` — SSID, auth type, status, `xiq_network_policy` custom field, linked from radio interfaces |

The worker asserts a **fixed field set**: a field is either always asserted
when XIQ reports the underlying data, or never asserted at all — there is no
configurable field-authority system. Fields with no XIQ equivalent (rack,
tenant, comments, asset tag, position, role, …) stay entirely
NetBox/human-owned, so they can never generate phantom drift.

## Repository layout

| Path | Purpose |
|------|---------|
| `orb_extreme_xiq/client.py` | Thin XIQ API client on plain `requests`: paginated `/devices`, `/locations/tree`, `/devices/radio-information`, and the legacy per-switch `/xiq/v0/monitor/device/wired/portlist` endpoint. Token or username/password auth, auto-refresh, retry-once-on-401. |
| `orb_extreme_xiq/identity.py` | Stable device naming, switch/AP classification, site + nested-Location resolution, device-type model mapping. |
| `orb_extreme_xiq/mapper.py` | XIQ → Diode entity mapping: devices, sites, locations, switch ports, AP radios, WLANs. |
| `orb_extreme_xiq/bootstrap.py` | One-time idempotent NetBox schema setup (custom fields + tags). |
| `orb_extreme_xiq/backend.py` | Orb Agent worker entrypoint + standalone dry-run runner. |
| `agent.yaml` | Example Orb Agent policy (bootstrap, site mapping, scope). |
| `tests/` | Offline pytest suite (HTTP mocked with `responses`), plus an opt-in live OpenAPI contract check. |

## Quick start

Run the stock Orb Agent image with this repository mounted:

```bash
docker run --rm \
  -v $PWD:/opt/orb/ \
  -e INSTALL_WORKERS_PATH=/opt/orb/workers.txt \
  -e DIODE_CLIENT_ID -e DIODE_CLIENT_SECRET \
  -e XIQ_API_TOKEN \
  -e NETBOX_API_URL -e NETBOX_API_TOKEN \
  netboxlabs/orb-agent:latest run -c /opt/orb/agent.yaml
```

First-run flow (mirrors the Meraki/ACI integrations):

1. Set `BOOTSTRAP: true` and provide `NETBOX_API_URL` + `NETBOX_API_TOKEN` so
   the custom-field definitions and tags are created. Run once.
2. Set `BOOTSTRAP: false` for all scheduled runs afterward.

Bootstrap uses the NetBox REST API directly because field *definitions* are
schema, not data, and that path works regardless of Diode SDK version. It
skips gracefully when no NetBox token is set.

> **Tip:** set `common.diode.dry_run: true` in `agent.yaml` first to inspect
> the generated JSON before ingesting anything.

## Configuration

Policy `config:` keys (see `agent.yaml` for a complete example):

| Key | Meaning |
|-----|---------|
| `BOOTSTRAP` | Run schema setup before sync (first run only). |
| `default_site` | Site for devices with a missing/unresolvable location. |
| `name_source` | `hostname` (default) or `serial`. |
| `scope.sites` | Limit sync to specific resolved sites (`["*"]` for all). |

### XIQ authentication

- **API token** (recommended): XIQ UI → Global Settings → API Token Management.
  Set `XIQ_API_TOKEN`.
- **Username/password**: set `XIQ_USERNAME` / `XIQ_PASSWORD`; the client logs
  in via `POST /login` and auto-refreshes the JWT.
- Base URL: `https://api.extremecloudiq.com` (override with `XIQ_API_URL`).
- The wired-port-list call is the one exception: it lives on
  `https://cloudapi.extremecloudiq.com` (`client.LEGACY_BASE_URL`), an older
  host not covered by the current XIQ OpenAPI spec, but takes the same bearer
  token.

Every credential key can come from the policy `config:` or from a same-named
environment variable; policy config wins.

## Development

```bash
pip install -e ".[dev]"
export XIQ_API_TOKEN=...              # or XIQ_USERNAME / XIQ_PASSWORD
python -m orb_extreme_xiq.backend     # dry run: fetch, map, print entities (no Diode push)
pytest                                # offline test suite
ruff check . && ruff format --check . # lint + format
```

The Orb Agent worker (`netboxlabs-orb-worker`) owns the Diode client and the
actual ingest entirely — `Backend.run()` only ever *produces* entities. There
is deliberately no dev-mode "push to Diode" path; run inside the real
`orb-agent` container (see `agent.yaml`) to actually ingest.

**Deploying via the Orb Agent container:** this project's own install
(`pip install .`, e.g. via `workers.txt` and `INSTALL_WORKERS_PATH`) covers
every runtime dependency — there is no separate SDK install step to run
inside the container.

### Testing

The default `pytest` run is fully offline: `test_client.py`/`test_backend.py`
mock XIQ's HTTP endpoints with `responses`, `test_mapper.py`/`test_identity.py`
use plain fixtures, and `test_bootstrap.py` mocks the NetBox REST API.

The one exception is `tests/test_openapi_contract.py`: it fetches the live
XIQ OpenAPI spec (`https://api.extremecloudiq.com/openapi`, unauthenticated)
and asserts `/devices` and `/locations/tree` still expose the query params
`client.py` hardcodes (`page`/`limit`/`views`/`locationIds`,
`parentId`/`expandChildren`) — a tripwire for upstream renames or removals.
It's marked `contract` and excluded from the default run (`addopts = "-m 'not
contract'"`); run it explicitly with `pytest -m contract`, or rely on the
scheduled weekly `contract` job in `.github/workflows/ci.yml`.

The contract check is deliberately scoped to paths and query-param *names*,
not response-body fields: the public spec's `XiqDevice` schema is far thinner
than the ~34-field payload XIQ actually returns at runtime (confirmed against
real recorded responses), so it is not a reliable signal for response-shape
drift. `mapper.py`'s field assumptions are instead exercised against real
recorded responses in `test_mapper.py`/`test_backend.py`.

### Verifying the SDK contracts

Two upstream interfaces are generated code that can move between releases —
re-check them if you change SDK versions:

- **Worker backend** (verified against `netboxlabs-orb-worker` 1.16.0):
  `backend.Backend` implements `describe()` (classmethod returning `Metadata`)
  and `run(self, policy_name, policy, **kwargs) -> Iterable[Entity]`. The
  PolicyRunner handles scheduling, chunking, and the Diode client. Inspect
  with `python -c "import worker.backend as b, inspect; print(inspect.getsource(b.Backend))"`.
- **Diode custom-field values** (verified against `netboxlabs-diode-sdk`
  generated from NetBox v4.6.0): `custom_fields` values must be wrapped —
  `CustomFieldValue(text=...)`; a plain string raises `ValueError`.
  `mapper._cf_text()` funnels this through one place. Inspect with
  `python -c "import netboxlabs.diode.sdk.ingester as i; help(i.Device)"`.

## Design notes

### Assurance-ready by design

NetBox Assurance is a consumer-side feature: any source that ingests via
Diode surfaces as deviations once an Assurance license is enabled, with zero
code changes to the producer. "Future-proofing" therefore means producing
clean, stable Diode output:

- **Fixed field set** — human-owned fields are never asserted, so they can
  never generate phantom drift (see [What gets synced](#what-gets-synced)).
- **Stable identity** (`identity.py`) — deterministic device names; `serial`
  is asserted natively on the NetBox Device rather than via a separate
  immutable-ID custom field, the same approach the Cisco Meraki integration
  and NetBox Labs' generic discovery backends use.
- **Stable producer + tags** — fixed `app_name="orb-extreme-xiq"` and flat
  `extreme-networks` / `xiq` / `discovered` tags (mirroring Meraki's
  `cisco` / `meraki` / `discovered` pattern) keep XIQ data cleanly
  attributable and filterable in Assurance.

### Sites and nested locations

The root of a device's XIQ location tree (Site → Building → Floor, …) becomes
its NetBox **Site**; everything below the root becomes a chain of nested
NetBox **Location** entities, and the device is assigned to the most specific
one. `default_site` is used only when a device's location is missing or
unresolvable, in which case no Location is asserted either.

### Device type model mapping

XIQ's `product_type` prefixes `FabricEngine_` onto the model code for any
switch running Fabric Engine OS (e.g. `FabricEngine_5320_48P_8XE`). The
[NetBox Device Type Library](https://github.com/netbox-community/devicetype-library)
puts that marker at the *end* (e.g. `5320-48P-8XE-FabricEngine`), so
`identity.device_type_model_for` moves the prefix to a suffix and turns
underscores into hyphens for every `FabricEngine_`-prefixed code.
`product_type` values without the prefix (e.g. `VSP_SWITCH`, a generic code
that doesn't identify a specific physical model) are passed through unchanged
rather than guessed at.

### Wired switch ports

Every device whose `device_function` is a switch
(`identity.SWITCH_DEVICE_FUNCTIONS` / `identity.is_switch`) gets one
`get_wired_portlist` call, mapped to NetBox `Interface` entities: `name`,
link state, `speed` (parsed from `portSpeed`, Kbps), `duplex`, `description`
(`ifAlias`), and the `xiq_port_id` custom field.

- **Link state is asserted as `mark_connected`, not `enabled`.** XIQ's port
  `status` is link/operational state, not administrative shut/no-shut state —
  the endpoint doesn't expose admin state at all. `enabled` conventionally
  means administrative state in NetBox, so asserting it from link state would
  misrepresent a link-down port as "shut down by an operator".
  `mark_connected` is NetBox's field for exactly this ("is this interface
  physically connected to something"); `enabled` is left unset.
- **`type` is a best-effort guess** from XIQ's negotiated `portSpeed` alone
  (e.g. `1000base-t`, `10gbase-x-sfpp`) — XIQ doesn't expose a capability
  list or an SFP-vs-copper signal — and is left unset when the speed is
  unrecognized (e.g. `SPEED_AUTO`).
- **`mode` is deliberately not asserted.** On FLEX-UNI / Fabric-Attach
  deployments a port is mapped straight into an I-SID rather than a VLAN, so
  `portMode`/`taggedVlans` don't describe real port configuration there, and
  no documented XIQ API path exposes I-SID membership to assert instead.
  VLAN data (`taggedVlans`) isn't currently mapped at all.

### Wireless AP radios and WLANs

Every device whose `device_function` is an AP (`identity.AP_DEVICE_FUNCTIONS`
/ `identity.is_ap`) has its radios fetched via one bulk
`GET /devices/radio-information` call covering every AP in the same sync.
Each radio maps to a NetBox `Interface`: `name`, `type` (from `mode`, e.g.
`_11ax_5g` → `ieee802.11ax`; `_11be_*`/Wi-Fi 7 has no confirmed NetBox choice
yet and is left unset), `rf_role` (always `"ap"`), `tx_power`,
`primary_mac_address`, and `rf_channel_frequency`/`rf_channel_width` computed
via the standard IEEE 802.11 channel-numbering formulas. NetBox's
`rf_channel` string field isn't asserted — its exact valid format hasn't been
confirmed against a live NetBox instance.

Each radio's currently broadcast SSIDs become NetBox `WirelessLAN` entities,
deduped by SSID name across every AP in the sync and linked from the radio
`Interface` via NetBox's native `wireless_lans` field:

- `auth_type` is mapped from `ssid_security_type` (`TYPE_802DOT1X` →
  `wpa-enterprise`, `PSK`/`PPSK` → `wpa-personal`, `WEP` → `wep`, anything
  else including `OPEN`/`ENHANCED_OPEN` → `open`, mirroring the Cisco Meraki
  integration's fallback convention).
- `auth_cipher` is not set — that would require an additional `GET /ssids`
  call this worker doesn't make (see Roadmap).
- `status` is always `"active"`: the per-radio `ssid_status` field
  (`OPEN`/`CLOSED`) reads as broadcast visibility (hidden vs. advertised),
  not the SSID's configured enabled/disabled state, so it isn't used.
- WLANs aren't site-scoped (no `scope_site`): unlike a Meraki network, a XIQ
  network policy isn't inherently 1:1 with one site, and the same SSID can
  legitimately broadcast from APs in different sites.

> **Not yet verified against a live XIQ tenant.** The radio/WLAN field names
> and enum values above come from XIQ's documented OpenAPI spec, not from a
> real recorded response. This project's practice is to confirm mapper
> assumptions against real API output before trusting them (see
> `test_mapper.py`'s recorded-response fixtures for the device/port paths);
> that verification is still outstanding for `/devices/radio-information`.
> Spot-check a real response before relying on this in production.

## Roadmap

- Verify the AP radio/WLAN field mapping against a real recorded
  `/devices/radio-information` response (see note above).
- `xiq_tagged_vlans` / `xiq_lldp_neighbor` port custom fields, if a
  deployment needs them.
- I-SID / Fabric-Attach service mapping, if XIQ exposes it via a documented
  endpoint.
- VLANs / prefixes from network policies.
- Device role assertion (switch / AP / router), if a role-slug convention is
  settled on.
- `auth_cipher` on synced WLANs, via an additional `GET /ssids` call for
  `access_security.encryption_method`.
- NetBox's `rf_channel` string field on radio interfaces, once its exact
  valid format is confirmed against a live NetBox instance.
- Move bootstrap custom-field creation into the Diode path if a future SDK
  supports CustomField ingest entities.
