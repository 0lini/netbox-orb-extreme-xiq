# netbox-orb-extreme-platformone

[![CI](https://github.com/0lini/netbox-orb-extreme-platformone/actions/workflows/ci.yml/badge.svg)](https://github.com/0lini/netbox-orb-extreme-platformone/actions/workflows/ci.yml)

A discovery worker that synchronizes **Extreme Platform ONE** inventory into
**NetBox**. It runs on the free, open-source [NetBox Labs Orb
Agent](https://github.com/netboxlabs/orb-agent), pulls device, site, and
switch-port data from the Platform ONE cloud APIs (Assets and ConfigState),
and ingests it into NetBox via **Diode** — following the same integration
patterns as NetBox Labs' first-party workers such as Cisco Meraki. No Orb
Agent Pro subscription or private registry is required.

```
Platform ONE APIs (Assets + ConfigState) ──► orb_extreme_platformone ──► Diode ──► NetBox
```

> Migrating from the ExtremeCloud IQ version of this worker? See
> [Migrating from the XIQ worker](#migrating-from-the-xiq-worker).

## Synchronized data

| Platform ONE source | NetBox objects |
|---------------------|----------------|
| Devices (Assets API) | `Device` — name, serial, status, device type and manufacturer, platform (OS family + version), primary IPv4, provenance tags, `platformone_device_id` custom field |
| Device locations (ConfigState) | `Site` plus a nested `Location` chain (building → floor), falling back to the Assets API's flat site name |
| Switch ports (ConfigState) | `Interface` — name, admin state (`enabled`), link state (`mark_connected`), speed/duplex/type, description, MAC address, untagged/tagged VLANs with 802.1Q `mode`, `platformone_interface_id` custom field |

The worker asserts a **fixed field set**: each field is either always
asserted when Platform ONE reports the underlying data, or never asserted at
all. Fields with no Platform ONE equivalent (rack, tenant, comments, asset
tag, position, role) remain entirely NetBox-owned and can never generate
phantom drift in NetBox Assurance.

Wireless synchronization (AP radios, WLANs) is not currently implemented:
Platform ONE's ConfigState wireless tables do not yet carry enough data to
match the previous XIQ radio mapping. See the [roadmap](#roadmap).

## Platform ONE APIs used

Both APIs are documented on the [Platform ONE Developer
Portal](https://developer.extremeplatformone.com/api-reference), served from
the same host, and authenticated with the same bearer token:

- **Assets API** (`POST /assets/v1/devices`) — device inventory: hostname,
  serial, MAC, model (`product_type`), OS version, connection state, flat
  site name, and the `function` value (Switch Engine, Fabric Engine, EXOS,
  VOSS, AP, …) that gates the port sync.
- **ConfigState API** (`POST /configstate/v1/retrieve-*`) — per-device
  configuration and state tables: port configuration and state
  (`retrieve-asset-port-config`, `retrieve-asset-port-state`), VLAN
  membership (`retrieve-asset-interface-vlan-properties`), and the
  site/building/floor location record (`retrieve-asset-location`).

Every ConfigState filter field accepts a list, so the per-tick API call
budget is flat rather than per-device: one paginated Assets listing, one
paginated ConfigState device listing (for correlation), one location call,
and one call per port table covering every in-scope switch at once. No
undocumented endpoints are used.

## Repository layout

| Path | Purpose |
|------|---------|
| `orb_extreme_platformone/client.py` | Platform ONE HTTP client: paginated Assets device listing and a generic batched ConfigState `retrieve()`. |
| `orb_extreme_platformone/identity.py` | Device naming, switch detection, site/building/floor resolution, device-type model mapping. |
| `orb_extreme_platformone/mapper.py` | Platform ONE → Diode entity mapping: devices, sites, locations, switch ports, VLANs. |
| `orb_extreme_platformone/bootstrap.py` | Idempotent NetBox schema setup (custom fields and tags). |
| `orb_extreme_platformone/backend.py` | Orb Agent worker entrypoint: Assets↔ConfigState correlation, batched table fetches, standalone dry-run runner. |
| `agent.yaml` | Example Orb Agent policy. |
| `tests/` | Offline pytest suite, plus opt-in contract checks against downloaded Platform ONE OpenAPI specs. |

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

First-run procedure:

1. Set `BOOTSTRAP: true` and provide `NETBOX_API_URL` and `NETBOX_API_TOKEN`
   so the custom-field definitions and tags are created. Run once.
2. Set `BOOTSTRAP: false` for all scheduled runs afterward.

Bootstrap uses the NetBox REST API directly because field definitions are
schema rather than data; it skips gracefully when no NetBox token is set.

> **Tip:** set `common.diode.dry_run: true` in `agent.yaml` first to inspect
> the generated JSON before ingesting anything.

## Configuration

Policy `config:` keys (see `agent.yaml` for a complete example):

| Key | Description | Default |
|-----|-------------|---------|
| `BOOTSTRAP` | Run schema setup before the sync (first run only). | `false` |
| `classification` | Assets device filter: `SWITCH`, `WIRELESS`, `ROUTER`, … or `ALL`. Port sync only runs for switch-OS devices regardless. | `SWITCH` |
| `default_site` | Site assigned when neither ConfigState nor Assets names one. | `PlatformONE-Unmapped` |
| `name_source` | Device naming source: `hostname` or `serial`. | `hostname` |
| `scope.sites` | Restrict the sync to specific resolved sites; `["*"]` for all. | `["*"]` |

Every credential key can be provided in the policy `config:` or as a
same-named environment variable; policy config takes precedence.

### Authentication

- **API token:** create one in Extreme Platform ONE and set
  `PLATFORMONE_API_TOKEN`. All calls use the same `Authorization: Bearer`
  header; there is no username/password flow.
- **Base URL:** `https://cloudapi.extremecloudiq.com` by default; override
  with `PLATFORMONE_API_URL`.

## Development

```bash
pip install -e ".[dev]"
export PLATFORMONE_API_TOKEN=...            # or put it in .env (gitignored)
python -m orb_extreme_platformone.backend   # dry run: fetch, map, print entities
pytest                                      # offline test suite
ruff check . && ruff format --check .       # lint + format
```

The Orb Agent worker (`netboxlabs-orb-worker`) owns the Diode client and the
ingest; `Backend.run()` only produces entities. There is intentionally no
development-mode "push to Diode" path — run inside the `orb-agent` container
(see `agent.yaml`) to ingest. Installing this package (for example via
`workers.txt` and `INSTALL_WORKERS_PATH`) covers every runtime dependency.

### Testing

The default `pytest` run is fully offline: `test_client.py` and
`test_backend.py` mock the Platform ONE HTTP endpoints with `responses`,
`test_mapper.py` and `test_identity.py` use plain fixtures, and
`test_bootstrap.py` mocks the NetBox REST API.

The contract checks in `tests/test_openapi_contract.py` verify the endpoints,
pagination parameters, response keys, and filter fields this worker hardcodes
against the two Platform ONE OpenAPI specs. The specs sit behind the
developer portal's login wall, so the checks run against local downloads:
fetch the Asset Management and Config State specs from the
[portal](https://developer.extremeplatformone.com/api-reference), then:

```bash
export PLATFORMONE_ASSETS_SPEC=/path/to/assets-openapi.json
export PLATFORMONE_CONFIGSTATE_SPEC=/path/to/configstate-openapi.json
pytest -m contract
```

The contract tests are excluded from the default run and skip themselves when
the environment variables are unset.

### Upstream SDK contracts

Two upstream interfaces are generated code that can change between releases —
re-verify them when changing SDK versions:

- **Worker backend** (verified against `netboxlabs-orb-worker` 1.16.0):
  `backend.Backend` implements `describe()` (classmethod returning
  `Metadata`) and `run(self, policy_name, policy, **kwargs) -> Iterable[Entity]`.
  The PolicyRunner handles scheduling, chunking, and the Diode client.
- **Diode custom-field values** (verified against `netboxlabs-diode-sdk`
  generated from NetBox v4.6.0): `custom_fields` values must be wrapped in
  `CustomFieldValue(text=...)`; a plain string raises `ValueError`.
  `mapper._cf_text()` funnels this through one place.

## Design notes

### Assurance-ready output

NetBox Assurance is a consumer-side feature: any source that ingests via
Diode surfaces as deviations once an Assurance license is enabled, with no
producer changes. The worker is designed to produce clean, stable Diode
output accordingly:

- **Fixed field set** — human-owned fields are never asserted, so they can
  never generate phantom drift.
- **Stable identity** — deterministic device names; `serial` is asserted
  natively on the NetBox Device, the same approach used by the Cisco Meraki
  integration and NetBox Labs' generic discovery backends.
- **Stable producer and tags** — a fixed
  `app_name="netbox-orb-extreme-platformone"` and flat `extreme-networks` /
  `platform-one` / `discovered` tags keep Platform ONE data attributable and
  filterable.

### Assets ↔ ConfigState correlation

The Assets API identifies devices by a numeric `device_id`; ConfigState uses
its own UUID. The worker joins the two on each tick by **serial number**
(case-insensitive), falling back to base MAC address (normalized to bare
hex, since the two APIs format MACs differently) and then management IP.
Devices known to Assets but not yet to ConfigState still sync as Devices —
with the flat Assets site and no ports — and pick up full detail on a later
tick. A ConfigState outage likewise degrades the sync to Assets-only data
rather than failing it.

### Sites and nested locations

A device's ConfigState `AssetLocation` record (site, building, and floor
names) becomes its NetBox **Site** plus a nested **Location** chain, with
the device assigned to the most specific level present. Devices without a
ConfigState location fall back to the Assets API's flat `site_name`;
`default_site` applies only when neither source names a site.

### Platform and OS version

NetBox `Platform` objects are flat — the data model has no parent/child
nesting — so a "Fabric Engine → 9.2.1.0" hierarchy cannot be expressed
directly. The worker instead asserts one flat Platform combining the OS
family (from the Assets `function` value: `Fabric Engine`, `Switch Engine`,
`EXOS`, `VOSS`) with the reported `os_version`, e.g. `Fabric Engine
9.2.1.0`. When only one of the two is known, the Platform is that part
alone; devices reporting neither assert no platform.

### Device type model mapping

Assets prefixes `product_type` with `FabricEngine_` for switches running
Fabric Engine OS (e.g. `FabricEngine_5320_48P_8XE`). The [NetBox Device Type
Library](https://github.com/netbox-community/devicetype-library) places that
marker at the end (`5320-48P-8XE-FabricEngine`), so
`identity.device_type_model_for` moves the prefix to a suffix and converts
underscores to hyphens. Values without the prefix pass through unchanged.

### Switch ports

Every in-scope device whose Assets `function` is a switch OS has its ports
mapped from three ConfigState tables joined on `asset_interface_id`:

- **Admin state and link state are independent fields.** `enabled` reflects
  real administrative state (`AssetPortConfig.enabled`); link state is
  asserted separately as `mark_connected` (`AssetPortState.oper_state`), so
  an admin-down port and a link-down port are distinguishable in NetBox.
- **VLANs and 802.1Q mode** come from
  `retrieve-asset-interface-vlan-properties`: `port_vlan` becomes the
  untagged VLAN, the nested VLAN map (minus the untagged VLAN) becomes the
  tagged VLANs, and `mode` is set to `tagged` or `access` accordingly.
  Interfaces with no VLAN rows assert none of the three — on Fabric Engine
  deployments a port can be mapped directly into an I-SID instead of a VLAN,
  and inventing an access mode would misrepresent the configuration. VLANs
  are referenced by bare `vid`; no VLAN names or groups are asserted.
- **Speed, duplex, and connector use verified codes only.** ConfigState
  reports `oper_speed`, `oper_duplex`, and `connector_type` as integer codes
  with no value table in its OpenAPI spec. Only codes confirmed against
  production hardware are mapped (`oper_speed 4` = 1 Gbit/s, `oper_duplex 2`
  = full, `connector_type 1/2` = copper/fiber, yielding `1000base-t` /
  `1000base-x-sfp`); unknown codes assert nothing. `oper_state` is the
  exception: its schema description matches IF-MIB `ifOperStatus`, so
  standard IF-MIB numbering (1 = up, 2 = down) applies.

## Migrating from the XIQ worker

This project replaced its ExtremeCloud IQ backend (which depended on the
undocumented `/xiq/v0/monitor/device/wired/portlist` endpoint) with the
documented Platform ONE APIs. Operational differences:

| | XIQ worker (old) | Platform ONE worker |
|---|---|---|
| Package / `config.package` | `orb_extreme_xiq` | `orb_extreme_platformone` |
| Credentials | `XIQ_API_TOKEN` or username/password | `PLATFORMONE_API_TOKEN` only |
| Tags | `extreme-networks`, `xiq`, `discovered` | `extreme-networks`, `platform-one`, `discovered` |
| Custom fields | `xiq_network_policy`, `xiq_port_id` | `platformone_device_id`, `platformone_interface_id` |
| Port admin state / VLANs | not available | `enabled`, untagged/tagged VLANs, `mode` |
| Wireless radios / WLANs | synced | not synced (see [roadmap](#roadmap)) |

NetBox objects created by the old worker keep their `xiq` tag and
`xiq_network_policy` / `xiq_port_id` custom-field values until removed
manually — bootstrap only creates definitions, it never deletes. Device and
interface identity is unchanged (device name plus native serial, interface
name), so re-running this worker over an XIQ-populated NetBox updates the
same objects instead of duplicating them.

## Roadmap

- LLDP neighbors → NetBox cables/topology
  (`retrieve-asset-lldp-neighbor-state` or `retrieve-inferred-physical-link`).
- LAG interfaces and membership (`retrieve-asset-lag-config` / `-state`).
- PoE draw per port (`retrieve-asset-poe-power-ports-state`).
- I-SID / Fabric-Attach service mapping
  (`retrieve-asset-l2-vsn-suni-config`, `-tuni-config`).
- Extended verified enum tables (`oper_speed` / `oper_duplex` /
  `connector_type`) as more hardware is observed, unlocking more `speed` and
  `type` assertions.
- Wireless sync, once ConfigState's wireless tables carry enough data to
  match the previous XIQ radio/WLAN mapping.
- Device role assertion (switch / AP / router), once a role-slug convention
  is settled.
