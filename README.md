# netbox-orb-extreme-platformone

[![CI](https://github.com/0lini/netbox-orb-extreme-platformone/actions/workflows/ci.yml/badge.svg)](https://github.com/0lini/netbox-orb-extreme-platformone/actions/workflows/ci.yml)

An **Extreme Platform ONE** discovery worker for the NetBox Labs **Orb
Agent**. It pulls device, site, and switch-port inventory from the Platform
ONE cloud APIs (Assets + ConfigState) and ingests it into NetBox via
**Diode**, following the same patterns as NetBox Labs' proprietary
integrations (e.g. Cisco Meraki) — but it runs on the free, open-source
`netboxlabs/orb-agent` image. No Orb Agent Pro or private registry required.

```
Platform ONE APIs (Assets + ConfigState) ─► orb_extreme_platformone (collector + mapper) ─► Diode ─► NetBox (+ Assurance if licensed)
```

> **Coming from the ExtremeCloud IQ version of this worker?** See
> [Migrating from the XIQ worker](#migrating-from-the-xiq-worker).

## What gets synced

| Platform ONE data | NetBox objects |
|----------|----------------|
| Devices (Assets API) | `Device` — name, serial, status, device type + manufacturer, platform, primary IPv4, tags, `platformone_device_id` custom field |
| Device locations (ConfigState) | `Site` + nested `Location` chain (building → floor), falling back to the Assets API's flat site name |
| Switch ports (ConfigState) | `Interface` — name, **admin state (`enabled`)**, link state (`mark_connected`), speed/duplex/type (verified codes only), description, MAC, **untagged/tagged VLANs + 802.1Q `mode`**, `platformone_interface_id` custom field |

The worker asserts a **fixed field set**: a field is either always asserted
when Platform ONE reports the underlying data, or never asserted at all —
there is no configurable field-authority system. Fields with no Platform ONE
equivalent (rack, tenant, comments, asset tag, position, role, …) stay
entirely NetBox/human-owned, so they can never generate phantom drift.

Wireless sync (AP radios / WLANs) is deliberately **not** ported from the
XIQ version: Platform ONE Networking is switch/fabric-first and its
ConfigState wireless tables are still much thinner than XIQ's
radio-information endpoint. It can return once those tables mature.

## The two Platform ONE APIs used

Both are documented on the [Platform ONE Developer
Portal](https://developer.extremeplatformone.com/api-reference), served from
the same host, and take the same bearer token:

- **Assets API** (`POST /assets/v1/devices`) — the device inventory:
  hostname, serial, MAC, model (`product_type`), OS version, connection
  state, flat site name, and the `function` value (Switch Engine / Fabric
  Engine / EXOS / VOSS / AP / …) that gates the port sync.
- **ConfigState API** (`POST /configstate/v1/retrieve-*`) — per-device
  configuration/state tables collected from each switch: the port list
  (`retrieve-asset-port-config` / `retrieve-asset-port-state`), VLAN
  membership (`retrieve-asset-interface-vlan-properties`), and the
  site/building/floor location record (`retrieve-asset-location`).

Every ConfigState filter field takes a **list**, so the per-tick call budget
is flat, not per-device: one paginated Assets listing, one paginated
ConfigState device listing (for correlation), one `retrieve-asset-location`
call, and one call per port table covering **every in-scope switch at once**.

Unlike the XIQ version of this worker, no undocumented endpoint is used
anywhere — the legacy `/xiq/v0/monitor/device/wired/portlist` dependency is
gone.

## Repository layout

| Path | Purpose |
|------|---------|
| `orb_extreme_platformone/client.py` | Thin Platform ONE client on plain `requests`: paginated Assets device listing + a generic batched ConfigState `retrieve()`. Bearer-token auth. |
| `orb_extreme_platformone/identity.py` | Stable device naming, switch detection (Assets `function` enum), site/building/floor resolution, device-type model mapping. |
| `orb_extreme_platformone/mapper.py` | Platform ONE → Diode entity mapping: devices, sites, locations, switch ports (incl. VLANs). |
| `orb_extreme_platformone/bootstrap.py` | One-time idempotent NetBox schema setup (custom fields + tags). |
| `orb_extreme_platformone/backend.py` | Orb Agent worker entrypoint: Assets↔ConfigState correlation, batched table fetches, standalone dry-run runner. |
| `agent.yaml` | Example Orb Agent policy (bootstrap, classification, scope). |
| `tests/` | Offline pytest suite (HTTP mocked with `responses`), plus opt-in contract checks against downloaded Platform ONE OpenAPI specs. |

## Quick start

Run the stock Orb Agent image with this repository mounted:

```bash
docker run --rm \
  -v $PWD:/opt/orb/ \
  -e INSTALL_WORKERS_PATH=/opt/orb/workers.txt \
  -e DIODE_CLIENT_ID -e DIODE_CLIENT_SECRET \
  -e PLATFORMONE_API_TOKEN \
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
| `classification` | Assets device filter: `SWITCH` (default), `WIRELESS`, `ROUTER`, … or `ALL`. Port sync only ever runs for switch-OS devices regardless. |
| `default_site` | Site for devices when neither ConfigState nor Assets names one. |
| `name_source` | `hostname` (default) or `serial`. |
| `scope.sites` | Limit sync to specific resolved sites (`["*"]` for all). |

### Platform ONE authentication

- **API token**: create one in Extreme Platform ONE and set
  `PLATFORMONE_API_TOKEN`. All calls use the same `Authorization: Bearer`
  header; there is no username/password login flow in this worker.
- Base URL: `https://cloudapi.extremecloudiq.com` (override with
  `PLATFORMONE_API_URL`).

Every credential key can come from the policy `config:` or from a same-named
environment variable; policy config wins.

## Development

```bash
pip install -e ".[dev]"
export PLATFORMONE_API_TOKEN=...
python -m orb_extreme_platformone.backend  # dry run: fetch, map, print entities (no Diode push)
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
mock the Platform ONE HTTP endpoints with `responses`,
`test_mapper.py`/`test_identity.py` use plain fixtures, and
`test_bootstrap.py` mocks the NetBox REST API.

The contract checks in `tests/test_openapi_contract.py` verify the
endpoints, pagination params, response keys, and filter fields this worker
hardcodes against the two Platform ONE OpenAPI specs. The specs sit behind
the developer portal's login wall (no stable unauthenticated URL), so the
checks run against **local downloads**: fetch the Asset Management and
Config State specs from the
[portal](https://developer.extremeplatformone.com/api-reference), then

```bash
export PLATFORMONE_ASSETS_SPEC=/path/to/assets-openapi.json
export PLATFORMONE_CONFIGSTATE_SPEC=/path/to/configstate-openapi.json
pytest -m contract
```

They're marked `contract`, excluded from the default run, and skip
themselves when the env vars are unset.

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
- **Stable producer + tags** — fixed `app_name="netbox-orb-extreme-platformone"`
  and flat `extreme-networks` / `platform-one` / `discovered` tags
  (mirroring Meraki's `cisco` / `meraki` / `discovered` pattern) keep
  Platform ONE data cleanly attributable and filterable in Assurance.

### Assets ↔ ConfigState correlation

The Assets API identifies devices by a numeric `device_id`; ConfigState by
its own UUID. The worker joins the two per tick on **serial number**
(case-insensitive), falling back to base MAC (normalized to bare hex —
Assets sends `aabbccddeeff`, ConfigState may use separators) and then
management IP. Devices Assets knows but ConfigState doesn't (onboarding
pending, collection not finished) still sync as Devices — just with the flat
Assets site and no ports — and heal automatically on a later tick.

### Sites and nested locations

A device's ConfigState `AssetLocation` record (site / building / floor
names) becomes its NetBox **Site** plus a nested **Location** chain, with
the device assigned to the most specific level present. Devices without a
ConfigState location fall back to the Assets API's flat `site_name` (no
Location chain); `default_site` is used only when neither source names a
site. There is no location-tree API to walk the way XIQ's
`/locations/tree` was — both sources carry resolved names per device.

### Device type model mapping

Assets `product_type` keeps XIQ's convention of prefixing `FabricEngine_`
onto the model code for switches running Fabric Engine OS (e.g.
`FabricEngine_5320_48P_8XE`). The
[NetBox Device Type Library](https://github.com/netbox-community/devicetype-library)
puts that marker at the *end* (e.g. `5320-48P-8XE-FabricEngine`), so
`identity.device_type_model_for` moves the prefix to a suffix and turns
underscores into hyphens for every `FabricEngine_`-prefixed code. Values
without the prefix are passed through unchanged rather than guessed at.

### Wired switch ports

Every in-scope device whose Assets `function` is a switch OS
(`identity.SWITCH_DEVICE_FUNCTIONS`) has its ports mapped from three
ConfigState tables, joined on `asset_interface_id`:

- **`enabled` is real admin state** (`AssetPortConfig.enabled`) — the field
  the old XIQ portlist endpoint never exposed. Link state is asserted
  separately as `mark_connected` (`AssetPortState.oper_state`), so an
  admin-down port and a link-down port are finally distinguishable in
  NetBox.
- **VLANs and 802.1Q mode** come from
  `retrieve-asset-interface-vlan-properties`: `port_vlan` → untagged VLAN,
  the nested VLAN map (minus the untagged VLAN) → tagged VLANs, and `mode`
  is `tagged`/`access` accordingly. Interfaces with **no** VLAN rows assert
  none of the three — on Fabric Engine FLEX-UNI/Fabric-Attach deployments a
  port can be mapped straight into an I-SID instead of a VLAN, and
  inventing an access mode would misrepresent real configuration. VLANs are
  referenced by bare `vid` (no VLAN names/groups asserted).
- **Speed/duplex/connector are verified-code-only.** ConfigState publishes
  `oper_speed`/`oper_duplex`/`connector_type` as bare integer enums with
  *no value table anywhere in its OpenAPI spec* (verified: the spec contains
  zero `enum` definitions). Only codes confirmed against a production
  Fabric Engine device are mapped (`oper_speed 4` = 1 Gbit/s,
  `oper_duplex 2` = full, `connector_type 1/2` = copper/fiber →
  `1000base-t` / `1000base-x-sfp`); every unknown code asserts nothing
  rather than guessing. `oper_state` is the exception: its schema
  description is copied verbatim from IF-MIB `ifOperStatus`, so standard
  IF-MIB numbering (1=up, 2=down, …) is relied on.

### Migrating from the XIQ worker

This project replaced its ExtremeCloud IQ backend (undocumented
`/xiq/v0/monitor/device/wired/portlist` + XIQ REST API) with the documented
Platform ONE APIs. Operational differences:

| | XIQ worker (old) | Platform ONE worker |
|---|---|---|
| Package / `config.package` | `orb_extreme_xiq` | `orb_extreme_platformone` |
| Credentials | `XIQ_API_TOKEN` or username/password | `PLATFORMONE_API_TOKEN` only |
| Tags | `extreme-networks`, `xiq`, `discovered` | `extreme-networks`, `platform-one`, `discovered` |
| Custom fields | `xiq_network_policy`, `xiq_port_id` | `platformone_device_id`, `platformone_interface_id` |
| Port admin state / VLANs | not available | `enabled`, untagged/tagged VLANs, `mode` |
| Wireless radios / WLANs | synced | not synced (see above) |

NetBox objects created by the old worker keep their `xiq` tag and
`xiq_network_policy`/`xiq_port_id` custom-field values until you remove
them manually — bootstrap only ever creates definitions, it never deletes.
Device/interface identity is unchanged (device name + native serial,
interface name), so re-running this worker over an XIQ-populated NetBox
updates the same objects instead of duplicating them.

## Roadmap

- LLDP neighbors → NetBox cables/topology, via
  `retrieve-asset-lldp-neighbor-state` (or the pre-correlated
  `retrieve-inferred-physical-link`).
- LAG interfaces + membership (`retrieve-asset-lag-config/-state`).
- PoE draw per port (`retrieve-asset-poe-power-ports-state`).
- I-SID / Fabric-Attach service mapping
  (`retrieve-asset-l2-vsn-suni-config`, `-tuni-config` — documented in
  ConfigState, unlike in XIQ).
- Extend the verified enum tables (`oper_speed`/`oper_duplex`/
  `connector_type`) as more hardware is observed, unlocking more `speed`/
  `type` assertions.
- Wireless sync, once ConfigState's wireless tables carry enough to match
  the old XIQ radio/WLAN mapping.
- Device role assertion (switch / AP / router), if a role-slug convention is
  settled on.
- Move bootstrap custom-field creation into the Diode path if a future SDK
  supports CustomField ingest entities.
