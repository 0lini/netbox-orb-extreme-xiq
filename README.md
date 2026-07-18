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
| Devices (Assets API) | `Device` — name, serial, status, role (from Assets `function` when present; no static default), device type and manufacturer, platform (OS family + version), primary IPv4 or IPv6, provenance tags, `platformone_id` custom field |
| Device locations (ConfigState) | `Site` (optional latitude/longitude) plus a nested `Location` chain (building → floor), falling back to the Assets API's flat site name |
| Switch ports (ConfigState) | `Interface` — name, admin state (`enabled`), link state (`mark_connected`), speed/duplex/type, description, MAC address, `mgmt_only`, `poe_mode`, untagged/tagged VLANs with 802.1Q `mode`, `platformone_id` custom field |
| VLAN definitions (ConfigState) | `VLAN` — `vid` from `vlan_number`, `name` from `vlan_name` when non-empty, optional `site` from the device's resolved site, provenance tags |
| Interface IP addresses (ConfigState) | `IPAddress` — address/prefix assigned to the matching interface |
| Link aggregation (ConfigState) | `Interface` — LAG parent (`type=lag`, name, admin `enabled`, VLAN trunk/access, `poe_mode` when joined, optional description/link/MAC from duplicate port rows, `platformone_id`); member ports use the same physical-port fields plus Diode `Interface.lag` |
| Inferred clusters (ConfigState) | `VirtualChassis` — name from peer names, master = primary member (`device_one`), member `vc_position`, provenance tags, `platformone_id` custom field |

The worker asserts a **fixed field set**: each field is either always
asserted when Platform ONE reports the underlying data, or never asserted at
all. Fields with no Platform ONE equivalent (rack, tenant, comments, asset
tag, position) remain entirely NetBox-owned and can never generate phantom
drift in NetBox Assurance.

Wireless synchronization (AP radios, WLANs) is not currently implemented:
Platform ONE's ConfigState wireless tables do not yet carry enough data to
match the previous XIQ radio mapping. See the [roadmap](#roadmap).

## Platform ONE APIs used

Both APIs are documented on the [Platform ONE Developer
Portal](https://developer.extremeplatformone.com/api-reference), served from
the same host, and authenticated with the same bearer token:

- **Assets API** (`POST /assets/v1/devices`) — device inventory: hostname,
  serial, MAC, model (`product_type`), OS version, connection state, flat
  site name, management IP, and the `function` value (Switch Engine, Fabric
  Engine, EXOS, VOSS, AP, …) that gates the port sync and drives Device
  role when present (switch OSes → Switch, AP → Wireless AP; never a static
  default).
- **ConfigState API** (`POST /configstate/v1/retrieve-*`) — per-device
  configuration and state tables: port configuration and state
  (`retrieve-asset-port-config`, `retrieve-asset-port-state`), port
  capabilities (`retrieve-asset-port-capabilities`), VLAN membership
  (`retrieve-asset-interface-vlan-properties`), PoE
  (`retrieve-asset-poe-power-ports-state`,
  `retrieve-asset-poe-power-ports-config`), interface IP addresses
  (`retrieve-asset-interface-ip-address`), LAG configuration and state
  (`retrieve-asset-lag-config`, `retrieve-asset-lag-state`, plus member-port
  tables when nested members are empty), the site/building/floor location
  record (`retrieve-asset-location`), and inferred two-node clusters
  (`retrieve-inferred-device` then `retrieve-inferred-cluster`) mapped to
  NetBox VirtualChassis.

Every ConfigState filter field accepts a list, so the per-tick API call
budget is flat rather than per-device: one paginated Assets listing, one
paginated ConfigState device listing (for correlation), one location call,
one call per device-filtered port/LAG/VLAN/capabilities/PoE-state table covering
every in-scope switch at once (those independent table retrieves run
concurrently to cut tick wall-clock), optional LAG member-port calls when
nested `member_ports` are absent, optional PoE-config and interface-IP calls
filtered by collected interface UUIDs (also concurrent within that phase),
one InferredDevice call (`asset_device_id`), and two InferredCluster calls
(`device_one_id` / `device_two_id` as InferredDevice UUIDs) covering the same
device set. Dependent phases stay sequential (device IDs before port tables,
lag IDs before member-port tables, interface UUIDs before PoE-config / IP).
No undocumented endpoints are used.

## Repository layout

| Path | Purpose |
|------|---------|
| `orb_extreme_platformone/client.py` | Platform ONE HTTP client: paginated Assets device listing and a generic batched ConfigState `retrieve()`. |
| `orb_extreme_platformone/identity.py` | Device naming, switch detection, site/building/floor resolution, device-type model mapping. |
| `orb_extreme_platformone/mapper.py` | Platform ONE → Diode entity mapping: devices, sites, locations, switch ports, LAGs, VLANs, VirtualChassis. |
| `orb_extreme_platformone/bootstrap.py` | Idempotent NetBox schema setup (custom fields and tags). |
| `orb_extreme_platformone/backend.py` | Orb Agent worker entrypoint: Assets↔ConfigState correlation, batched table fetches. |
| `orb_extreme_platformone/__main__.py` | Standalone dry-run runner (`python -m orb_extreme_platformone`). |
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
2. Set `BOOTSTRAP: false` for all scheduled runs afterward (and drop the
   NetBox token from the runtime environment once bootstrap has succeeded).

Bootstrap uses the NetBox REST API directly because field definitions are
schema rather than data; it skips gracefully when no NetBox token is set.
Use a least-privilege NetBox token that can create/update custom fields and
tags only — not a full superuser token — and keep it out of the scheduled
worker once `BOOTSTRAP` is false.

> **Tip:** set `common.diode.dry_run: true` in `agent.yaml` first to inspect
> the generated JSON before ingesting anything. Do **not** commit dry-run
> output (hostnames, serials, MACs, IPs); `.gitignore` already excludes
> `.env` and `test.json`.

## Configuration

Policy `config:` keys (see `agent.yaml` for a complete example):

| Key | Description | Default |
|-----|-------------|---------|
| `BOOTSTRAP` | Run schema setup before the sync (first run only). | `false` |
| `classification` | Assets device filter: `ALL`, `SWITCH`, `WIRELESS`, `ROUTER`, …. Port sync only runs for switch-OS devices regardless. | `ALL` |
| `name_source` | Device naming source: `hostname` or `serial`. | `hostname` |
| `scope.sites` | Restrict the sync to specific resolved sites; `["*"]` for all. | `["*"]` |

Every credential key can be provided in the policy `config:` or as a
same-named environment variable; policy config takes precedence.

### Authentication

- **API token:** create one in Extreme Platform ONE and set
  `PLATFORMONE_API_TOKEN`. All calls use the same `Authorization: Bearer`
  header; there is no username/password flow. Prefer environment variables
  (or a local `.env`, which is gitignored) over putting secrets in
  `agent.yaml`.
- **Base URL:** `https://cloudapi.extremecloudiq.com` by default; override
  with `PLATFORMONE_API_URL`. Both `PLATFORMONE_API_URL` and
  `NETBOX_API_URL` must be `https://` URLs; plaintext `http://` is rejected.

### Security notes

- Keep `BOOTSTRAP: false` on scheduled runs; the NetBox bootstrap token is
  write-capable schema access and should not stay mounted afterward.
- API error logs truncate upstream response bodies so diagnostics stay short.
- Never commit dry-run JSON or live inventory exports to git.

## Development

```bash
pip install -e ".[dev]"
export PLATFORMONE_API_TOKEN=...            # or put it in .env (gitignored)
python -m orb_extreme_platformone           # dry run: fetch, map, print entities
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
(case-insensitive) — the shared primary key between the two APIs; there is
deliberately no MAC or IP fallback.
Devices known to Assets but not yet to ConfigState still sync as Devices —
with the flat Assets site and no ports — and pick up full detail on a later
tick. A ConfigState outage likewise degrades the sync to Assets-only data
rather than failing it.

### Sites and nested locations

A device's ConfigState `AssetLocation` record (site, building, and floor
names) becomes its NetBox **Site** plus a nested **Location** chain, with
the device assigned to the most specific level present. When
`site_latitude` / `site_longitude` are present they are asserted on the
Site (NetBox Locations have no coordinate fields). Devices without a
ConfigState location fall back to the Assets API's flat `site_name`. There
is no worker-side fallback site: Platform ONE assigns every device a site
itself ("Default Site"), so a device without one from either source is
unexpected and skipped with a warning.

### Device role

Assets `function` maps to a NetBox DeviceRole when Platform ONE reports a
real value. Switch OS families (`Fabric Engine`, `Switch Engine`, `EXOS`,
`VOSS`) collapse to role **Switch** (`switch`); `AP` becomes **Wireless AP**
(`wireless-ap`). Other non-empty functions pass through as the role name
with a slugified form (e.g. `Router` → `router`). Empty / missing function,
the Assets sentinel `Unknown`, or a value that cannot form a valid slug
assert **no** role — there is no static default (`network`, `unknown`, …).
Diode treats `Device.role` as optional, so omitting it leaves role
NetBox-owned (same Assurance posture as omitting `Interface.type` when the
connector code is unverified). NetBox's UI still requires a role for
manually created devices; that does not force the worker to invent one.

### Platform and OS version

NetBox `Platform` objects are flat — the data model has no parent/child
nesting — so a "Fabric Engine → 9.2.1.0" hierarchy cannot be expressed
directly. The worker instead asserts one flat Platform combining the OS
family (from the Assets `function` value: `Fabric Engine`, `Switch Engine`,
`EXOS`, `VOSS`) with the reported `os_version`, e.g. `Fabric Engine
9.2.1.0`. When Assets omits `os_version`, ConfigState
`AssetDevice.firmware_version` is used as a fallback. When only one of the
two parts is known, the Platform is that part alone; devices reporting
neither assert no platform.

### Device type model mapping

Assets prefixes `product_type` with `FabricEngine_` for switches running
Fabric Engine OS (e.g. `FabricEngine_5320_48P_8XE`). The [NetBox Device Type
Library](https://github.com/netbox-community/devicetype-library) places that
marker at the end (`5320-48P-8XE-FabricEngine`), so
`identity.device_type_model_for` moves the prefix to a suffix and converts
underscores to hyphens. Values without the prefix pass through unchanged.
When Assets omits `product_type`, ConfigState `AssetDevice.model_name` is
used with the same mapping.

### Primary IP

Device `primary_ip4` / `primary_ip6` prefer ConfigState
`retrieve-asset-interface-ip-address` rows that already carry a real prefix
(`address` + `mask_length`):

1. rows with `is_primary: true`
2. else IPs on interfaces flagged `management_port` in port capabilities
3. else an interface IP whose host matches the Assets management address

Assets `ip_address` is only used when it already includes a prefix length.
Bare Assets hosts are never padded with `/32` or `/128`.

### Switch ports

Every in-scope device whose Assets `function` is a switch OS has its ports
mapped from ConfigState tables joined on `asset_interface_id` (capabilities
join on `(asset_device_id, port_name)`):

- **Admin state and link state are independent fields.** `enabled` reflects
  real administrative state (`AssetPortConfig.enabled`); link state is
  asserted separately as `mark_connected` (`AssetPortState.oper_state`), so
  an admin-down port and a link-down port are distinguishable in NetBox.
- **Management-only** comes from `AssetPortCapabilities.management_port`
  (`retrieve-asset-port-capabilities`).
- **PoE mode** is `pse` when `AssetPoePowerPortsState.supported` is true or
  `AssetPoePowerPortsConfig.enable` is true; otherwise omitted. PoE
  `classification` / `standard` → `poe_type` is **not** mapped: those
  integers have no verified value table in the OpenAPI spec.
- **VLANs and 802.1Q mode** prefer
  `retrieve-asset-interface-vlan-properties`: `port_vlan` becomes the
  untagged VLAN, the nested VLAN map (minus the untagged VLAN) becomes the
  tagged VLANs, and `mode` is set to `tagged` or `access` accordingly.
  When vlan-properties rows are absent, `AssetPortConfig.native_vlan` plus
  `port_mode` are used as a fallback (`port_mode` True → `tagged`, False →
  `access`). Interfaces with neither source assert none of the three —
  on Fabric Engine a port can be mapped directly into an I-SID instead of a
  VLAN. Extreme reserved internal VIDs **4060–4094** (inclusive) are
  filtered from ingest: they are omitted from Interface `untagged_vlan` /
  `tagged_vlans`; if a port has only reserved memberships after filtering,
  `mode` is omitted too. VLANs are referenced by bare `vid` only (names are
  switch-local while Diode/NetBox VLANs are site-scoped). VLAN groups are
  not asserted.
- **Interface IP addresses** from `retrieve-asset-interface-ip-address`
  become Diode `IPAddress` entities assigned to the matching interface, using
  `address` + `mask_length`.
- **Speed, duplex, and connector use verified codes only.** ConfigState
  reports `oper_speed`, `oper_duplex`, and `connector_type` as integer codes
  with no value table in its OpenAPI spec. Only codes confirmed against
  production hardware are mapped (`oper_speed 4` = 1 Gbit/s, `oper_duplex 2`
  = full, `connector_type 1/2` = copper/fiber, yielding `1000base-t` /
  `1000base-x-sfp`); unknown codes assert nothing. Config-side `speed` /
  `duplex` integers are likewise unverified and are not used as fallbacks.
  `oper_state` is the exception: its schema description matches IF-MIB
  `ifOperStatus`, so standard IF-MIB numbering (1 = up, 2 = down) applies.

### LAG interfaces and membership

ConfigState `retrieve-asset-lag-config` / `retrieve-asset-lag-state` (batched
by `asset_device_id`, same pattern as ports) map to NetBox LAG interfaces.
When nested `member_ports` are empty on retrieve, the worker falls back to
`retrieve-asset-lag-config-member-port` / `retrieve-asset-lag-state-member-port`
filtered by lag row id.

- **LAG parent** is an `Interface` with `type=lag`, name from `name` (or
  `lag-{lag_number}` when name is absent), admin `enabled` from config, and
  `platformone_id` from `asset_interface_id` (the existing interface UUID CF —
  `lag_number` is naming-only, not a second custom field). Shared joins on
  that interface id apply vlan-properties (untagged / tagged VLANs by bare
  `vid` and 802.1Q `mode`), PoE (`poe_mode`), and interface IP addresses the
  same way as for physical ports. When AssetPortConfig/State also returns the
  LAG's `asset_interface_id`, description, `mark_connected`, and
  `primary_mac_address` are taken from those rows (and port-config
  `native_vlan` is a VLAN fallback); speed/duplex/connector type are not,
  so `type=lag` is never overwritten. Port-table duplicates are not emitted
  as a second Interface.
- **Members** set Diode `Interface.lag` to the parent LAG (by device + name)
  and otherwise use the full physical-port field set when port
  config/state/capability/PoE/VLAN data exists. Membership prefers config
  member ports; state members fill gaps. A member named only on the LAG (no
  port-config/state row) is still emitted with device, name, `lag`, and
  provenance tags so membership is not lost.
- **Not mapped (LACP / MLT extras):** AssetLagConfig also reports `mode`
  (schema: STATIC / LACP / VLACP on Fabric Engine; STATIC / LACP /
  HEALTH_CHECK on Switch Engine), `lacp_key` (string, VOSS only),
  `load_balance_algo` (integer; VOSS always CUSTOM), and `dynamic`. Diode's
  Interface exposes `lag` for membership and `mode` for **802.1Q**
  access/tagged only — there is no `lacp_key`, load-balance, or LACP-mode
  field. The `mode` / `load_balance_algo` integers have no published value
  table in OpenAPI (same rule as unverified `oper_speed` codes), so they
  are not guessed into `description` or invented custom fields. Revisit
  when Platform ONE publishes enum tables or Diode/NetBox gains first-class
  LACP attributes. Also not mapped: MLAG peer tables
  (`retrieve-asset-mlag-*`), RSMLT, or `InferredLag` (the asset lag tables
  already carry name, admin state, and membership under AssetDevice UUIDs).
  AssetLagState has no oper_state / MAC / speed of its own — those only
  appear if port tables also list the LAG interface id.

A failed lag-config or lag-state fetch degrades that table for the tick;
ports still map from whichever tables survived. VirtualChassis sync is
unchanged and independent.

### VirtualChassis from inferred clusters

ConfigState `retrieve-inferred-cluster` returns two-node clusters
(`InferredCluster`: `device_one_id` / `device_two_id` are **InferredDevice**
UUIDs — the schema calls them "User device" — not AssetDevice UUIDs;
`device_one` is the primary). The worker resolves AssetDevice → InferredDevice
via `retrieve-inferred-device` (`asset_device_id`), queries both cluster
member filters with those InferredDevice IDs, remaps members back to
AssetDevice UUIDs, and maps each complete in-scope pair to a NetBox
`VirtualChassis`:

- **Name** prefers two distinct peer names (`device_one_peer_name` /
  `device_two_peer_name`) so a primary/backup flip does not rename the
  chassis; identical placeholders like `"Default"` fall through to distinct
  member device names, then the cluster UUID. Duplicate computed names across
  clusters are emitted as-is with a warning: the unique `platformone_id`
  custom field makes NetBox reject the collision at ingest, surfacing the
  upstream data problem instead of hiding it behind an invented suffix.
- **Master** is `device_one`; members get `vc_position` 1 and 2.
- **`platformone_id`** stores the InferredCluster UUID for stable
  correlation; provenance tags match other synced objects.
- Clusters where either member is missing from the scoped device set are
  skipped. A failed cluster fetch degrades to no VirtualChassis for that
  tick rather than aborting the sync.

Not mapped (no sensible Platform ONE source, or intentionally NetBox-owned):
`description`, `domain`, `comments`, `owner`, Diode `metadata`, Device
`vc_priority`. The OpenAPI `type` integer has no published value table, so
it is not mapped.

## Migrating from the XIQ worker

This project replaced its ExtremeCloud IQ backend (which depended on the
undocumented `/xiq/v0/monitor/device/wired/portlist` endpoint) with the
documented Platform ONE APIs. Operational differences:

| | XIQ worker (old) | Platform ONE worker |
|---|---|---|
| Package / `config.package` | `orb_extreme_xiq` | `orb_extreme_platformone` |
| Credentials | `XIQ_API_TOKEN` or username/password | `PLATFORMONE_API_TOKEN` only |
| Tags | `extreme-networks`, `xiq`, `discovered` | `extreme-networks`, `platform-one`, `discovered` |
| Custom fields | `xiq_network_policy`, `xiq_port_id` | `platformone_id` (device / interface / virtual chassis) |
| Port admin state / VLANs | not available | `enabled`, untagged/tagged VLANs with names, `mode` |
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
- PoE draw / `poe_type` once classification and standard codes are verified
  against live gear (`retrieve-asset-poe-power-ports-state`).
- I-SID / Fabric-Attach service mapping
  (`retrieve-asset-l2-vsn-suni-config`, `-tuni-config`).
- Extended verified enum tables (`oper_speed` / `oper_duplex` /
  `connector_type`) as more hardware is observed, unlocking more `speed` and
  `type` assertions.
- Wireless sync, once ConfigState's wireless tables carry enough data to
  match the previous XIQ radio/WLAN mapping.
- LACP attributes on LAG parents (`mode` / `lacp_key` / `load_balance_algo`)
  once OpenAPI publishes integer enums or Diode/NetBox gains matching fields.
- MLAG peer correlation (`retrieve-asset-mlag-*`), if NetBox modeling for
  multi-chassis LAGs is needed beyond single-device LAG membership.
