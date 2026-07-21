# netbox-orb-extreme-platformone

[![CI](https://github.com/0lini/netbox-orb-extreme-xiq/actions/workflows/ci.yml/badge.svg)](https://github.com/0lini/netbox-orb-extreme-xiq/actions/workflows/ci.yml)

A discovery worker that synchronizes **Extreme Platform ONE** inventory into
**NetBox**. It runs on the free, open-source [NetBox Labs Orb
Agent](https://github.com/netboxlabs/orb-agent), extracts device, site,
switch-port, LAG, VirtualChassis, and Wi‑Fi data from the Platform ONE cloud
APIs (Assets and ConfigState), transforms it into Diode entities, and lets
Orb ingest into NetBox — the same pattern as NetBox Labs' first-party
workers such as Cisco Meraki. No Orb Agent Pro subscription or private
registry is required.

> **GitHub repo:** [`0lini/netbox-orb-extreme-xiq`](https://github.com/0lini/netbox-orb-extreme-xiq)
> (historical XIQ name). **Python package / Orb `config.package`:**
> `orb_extreme_platformone` / PyPI project name
> `netbox-orb-extreme-platformone`.

```
Platform ONE (Assets + ConfigState)
        │
        ▼
┌───────────────────────────────────────┐
│  orb_extreme_platformone              │
│  client  →  extract  →  transform     │
└───────────────────────────────────────┘
        │
        ▼
   Diode  ──►  NetBox
```

> Migrating from the ExtremeCloud IQ version of this worker? See
> [Migrating from the XIQ worker](#migrating-from-the-xiq-worker).

## Synchronized data

| Platform ONE source | NetBox objects |
|---------------------|----------------|
| Devices (Assets API) | `Device` — name (Assets `host_name` when present), serial, status (`active`/`offline`; unknown → `active`, Meraki-style), role (from Assets `function` when present; no static default), device type and manufacturer, platform (OS family + version), primary IPv4/IPv6 from ConfigState interface IPs (Assets `ip_address` is match-only), provenance tags, `platformone_device_id` custom field |
| Device locations (ConfigState) | `Site` (optional latitude/longitude) plus a nested `Location` chain (building → floor), falling back to the Assets API's flat site name |
| Switch ports (ConfigState) | `Interface` — name, admin state (`enabled`), link state (`mark_connected`), speed/duplex (verified codes), `type` (verified codes else `other`), description, MAC (uppercase), `mgmt_only`, `poe_mode`, untagged/tagged VLANs with 802.1Q `mode`, `platformone_interface_id` custom field |
| VLAN membership (ConfigState) | Interface `untagged_vlan` / `tagged_vlans` by `vid` with `name=str(vid)` (NetBox requires a name; switch-local names are not site-scoped, so VID is the stable placeholder; named VLAN sync via `retrieve-asset-vlan-config` is not used) |
| Interface IP addresses (ConfigState) | `IPAddress` — address + `mask_length`, `status` `active`, assigned to the matching interface (bare addresses without a prefix are skipped; SVI/orphan IPs also emit a minimal Interface named from vlan/port/LAG rows) |
| Link aggregation (ConfigState) | `Interface` — LAG parent (`type=lag`, name, admin `enabled` from duplicate port-config or default up, VLAN trunk/access, `poe_mode` when joined, optional description/MAC from duplicate port rows, interface CFs); member ports use the same physical-port fields plus Diode `Interface.lag` |
| Inferred clusters (ConfigState) | `VirtualChassis` — name from peer names, master = primary member (`device_one`), member `vc_position`, provenance tags, `platformone_cluster_id` custom field |
| AP radios (ConfigState) | `Interface` — radio name, admin `enabled`, `type` (`ieee802.11*` when known including `ieee802.11be`; else `other` without RF fields), `rf_role=ap` + `tx_power` / channel fields only on wireless types, `primary_mac_address` (BSSID, uppercase), linked `wireless_lans`, interface CFs |
| SSIDs / WLANs (ConfigState) | `WirelessLAN` — `ssid`, `status` (`active`/`disabled`; unknown → `active`), `auth_type` / `auth_cipher` (unknown → `open` / `auto`, Meraki-style); deduped by SSID across APs (not site-scoped) |

The worker asserts a **fixed field set**: each field is either always
asserted when Platform ONE reports the underlying data, or never asserted at
all. Fields with no Platform ONE equivalent (rack, tenant, comments, asset
tag, position) remain entirely NetBox-owned and can never generate phantom
drift in NetBox Assurance.

## How a policy tick works

Each Orb policy tick is a short ETL pipeline. Orb owns scheduling and Diode
ingest; this package only produces entities.

1. **Bootstrap (optional)** — when `BOOTSTRAP: true`, create NetBox custom
   fields and provenance tags via the NetBox REST API.
2. **Extract** — list Assets devices, correlate ConfigState devices by
   serial, load locations, then batched ConfigState tables for ports, LAGs,
   wireless/SSID, and inferred clusters (`extract/`).
3. **Transform** — map correlated records to Diode entities: devices,
   sites/locations, interfaces, IPs, VirtualChassis, radios, WirelessLANs
   (`transform/`).
4. **Load** — return entities to the Orb PolicyRunner, which pushes them
   through Diode into NetBox.

Independent ConfigState table retrieves within a phase run concurrently;
later phases wait on IDs from earlier ones (see call phases below).

## Platform ONE APIs used

Both APIs are documented on the [Platform ONE Developer
Portal](https://developer.extremeplatformone.com/api-reference), served from
the same host, and authenticated with the same bearer token (from
username/password login or a static API token):

- **Assets API** (`POST /assets/v1/devices`) — device inventory: hostname,
  serial, MAC, model (`product_type`), OS version, connection state, flat
  site name, management IP, and the `function` value (Switch Engine, Fabric
  Engine, EXOS, VOSS, AP, …) that gates the port sync and drives Device
  role when present (switch OSes → Switch, AP → Wireless AP; never a static
  default).
- **ConfigState API** (`POST /configstate/v1/retrieve-*`) — per-device
  configuration and state tables listed in the call phases below. Every
  filter field accepts a list, so each retrieve covers the whole in-scope
  device (or interface) set in one batched call rather than one call per
  device. No undocumented endpoints are used.

### Extract call phases

Calls run in order. Within a phase, independent retrieves run concurrently.
Later phases exist only because their filter IDs come from earlier results —
they are not separate optional features you turn on or off.

| Phase | Always? | What is called | Why it waits |
|-------|---------|----------------|--------------|
| 1. Inventory | Yes | Assets `POST /assets/v1/devices`; ConfigState `retrieve-asset-device`; `retrieve-asset-location` | — |
| 2. Switch tables | When switches are in scope | Device-filtered port/LAG/VLAN/capabilities/PoE-state tables (`retrieve-asset-port-config`, `-port-state`, `-port-capabilities`, `-interface-vlan-properties`, `-lag-config`, `-lag-state`, `-poe-power-ports-state`). LAG membership comes from nested `member_ports` on lag-config/state rows. | Needs AssetDevice UUIDs from phase 1 |
| 3. Interface extras | When phase 2 collected any `asset_interface_id`s | `retrieve-asset-interface-ip-address` | That table filters by interface UUID only (no device filter), so IDs must come from phase 2 |
| 4. Wireless | When APs are in scope | `retrieve-asset-wireless-interface`, `-wireless-interface-state`, `-ssid-config`, `-ssid-state` | Needs AssetDevice UUIDs from phase 1 |
| 5. Clusters | Yes (degrades if empty/fail) | `retrieve-inferred-device`, then `retrieve-inferred-cluster` twice (`device_one_id` / `device_two_id`) | Cluster member filters are InferredDevice UUIDs, not AssetDevice UUIDs |

Phase 3 is a **second hop** (interface IPs cannot be requested by device id).
It is not a policy knob.

## Repository layout

| Path | Purpose |
|------|---------|
| `orb_extreme_platformone/backend.py` | Orb Agent worker entrypoint: policy tick orchestration (bootstrap → extract → transform). |
| `orb_extreme_platformone/client.py` | Platform ONE HTTP client: `POST /login` (or static token), paginated Assets listing, batched ConfigState `retrieve()`. |
| `orb_extreme_platformone/extract/` | **Extract** — table catalogs, concurrent retrieves, Assets↔ConfigState correlation, port / wireless / cluster phases. |
| `orb_extreme_platformone/extract/tables.py` | ConfigState table catalogs (`PORT_TABLES`, `WIRELESS_TABLES`, LAG/interface filters). |
| `orb_extreme_platformone/extract/retrieve.py` | Concurrent batched ConfigState retrieves with per-table degradation. |
| `orb_extreme_platformone/extract/correlate.py` | Join Assets devices to ConfigState devices (by serial) and locations. |
| `orb_extreme_platformone/extract/ports.py` | Switch port / LAG / PoE / IP extract phases. |
| `orb_extreme_platformone/extract/wireless.py` | AP radio / SSID extract phase. |
| `orb_extreme_platformone/extract/clusters.py` | InferredDevice → InferredCluster extract for VirtualChassis. |
| `orb_extreme_platformone/transform/` | **Transform** — Platform ONE records → Diode entities, split by domain. |
| `orb_extreme_platformone/transform/devices.py` | Devices, sites, locations, platforms, roles, primary IP attachment. |
| `orb_extreme_platformone/transform/ports.py` | Physical ports, VLANs, PoE, interface IPs, LAG parents/members. |
| `orb_extreme_platformone/transform/wireless.py` | AP radio Interfaces and WirelessLAN entities. |
| `orb_extreme_platformone/transform/virtual_chassis.py` | InferredCluster → VirtualChassis + member positions. |
| `orb_extreme_platformone/transform/common.py` | Shared constants, provenance tags, `CustomFieldValue` helper. |
| `orb_extreme_platformone/identity.py` | Device naming, switch/AP detection, site/building/floor resolution, device-type model mapping. |
| `orb_extreme_platformone/bootstrap.py` | Idempotent NetBox schema setup (custom fields and tags). |
| `orb_extreme_platformone/urls.py` | HTTPS URL validation for Platform ONE and NetBox base URLs. |
| `orb_extreme_platformone/__main__.py` | Standalone dry-run runner (`python -m orb_extreme_platformone`). |
| `agent.yaml` | Example Orb Agent policy. |
| `workers.txt` | Packages Orb installs at container start (`INSTALL_WORKERS_PATH`). |
| `tests/` | Offline pytest suite, plus opt-in OpenAPI contract checks. |

## Quick start

Run the stock Orb Agent image with this repository mounted at `/opt/orb`:

```bash
docker run --rm \
  -v $PWD:/opt/orb/ \
  -e INSTALL_WORKERS_PATH=/opt/orb/workers.txt \
  -e DIODE_CLIENT_ID -e DIODE_CLIENT_SECRET \
  -e PLATFORMONE_USERNAME -e PLATFORMONE_PASSWORD \
  -e NETBOX_API_URL -e NETBOX_API_TOKEN \
  netboxlabs/orb-agent:latest run -c /opt/orb/agent.yaml
```

`workers.txt` installs the mounted repo (`.`) so Orb can import
`orb_extreme_platformone`.

First-run procedure:

1. Set `BOOTSTRAP: true` and provide `NETBOX_API_URL` and `NETBOX_API_TOKEN`
   so the custom-field definitions and tags are created. Run once.
2. Set `BOOTSTRAP: false` for all scheduled runs afterward (and drop the
   NetBox token from the runtime environment once bootstrap has succeeded).

Bootstrap uses the NetBox REST API directly because field definitions are
schema rather than data. When `BOOTSTRAP: true`, missing `NETBOX_API_URL` /
`NETBOX_API_TOKEN` fail the tick (fail-closed). Use a least-privilege NetBox
token that can create/update custom fields and tags only — not a full
superuser token — and keep it out of the scheduled worker once `BOOTSTRAP`
is false.

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
| `scope.sites` | Restrict the sync to specific resolved sites; `["*"]` for all. | `["*"]` |

Every credential key can be provided in the policy `config:` or as a
same-named environment variable; policy config takes precedence.

### Authentication

- **Username/password (recommended):** set `PLATFORMONE_USERNAME` and
  `PLATFORMONE_PASSWORD`. The client calls `POST /login` on the Platform ONE
  host, caches the returned bearer token, refreshes it before expiry, and
  retries once on HTTP 401. Prefer this over API keys, which expire quickly.
- **API token:** set `PLATFORMONE_API_TOKEN` instead. Useful for short-lived
  tokens or environments that already mint them; the client does not refresh
  a static token.
- Prefer environment variables (or a local `.env`, which is gitignored) over
  putting secrets in `agent.yaml`.
- **Base URL:** `https://cloudapi.extremecloudiq.com` by default; override
  with `PLATFORMONE_API_URL`. Both `PLATFORMONE_API_URL` and
  `NETBOX_API_URL` must be `https://` for remote hosts. Plaintext `http://`
  is allowed only for local-dev hosts (loopback, `*.local`, Docker hostname
  `netbox`) when bootstrapping against a lab NetBox.

### Security notes

Full posture and residual risks: [`SECURITY.md`](SECURITY.md).

- Keep `BOOTSTRAP: false` on scheduled runs; the NetBox bootstrap token is
  write-capable schema access and should not stay mounted afterward.
- API error logs truncate upstream response bodies so diagnostics stay short.
- Outbound HTTP does not follow redirects (login password / NetBox token stay
  on the configured origin).
- Never commit dry-run JSON or live inventory exports to git.

## Development

```bash
pip install -e ".[dev]"
export PLATFORMONE_USERNAME=...             # or PLATFORMONE_API_TOKEN=...
export PLATFORMONE_PASSWORD=...             # or put both in .env (gitignored)
python -m orb_extreme_platformone           # dry run: extract → transform → print entities
pytest                                      # offline test suite
ruff check . && ruff format --check .       # lint + format
```

The Orb Agent worker (`netboxlabs-orb-worker`) owns the Diode client and the
ingest; `Backend.run()` only produces entities. There is intentionally no
development-mode "push to Diode" path — run inside the `orb-agent` container
(see `agent.yaml`) to ingest. Installing this package (for example via
`workers.txt` and `INSTALL_WORKERS_PATH`) covers every runtime dependency.

### Testing

The default `pytest` run is fully offline:

| Module | What it covers |
|--------|----------------|
| `test_client.py` | Platform ONE HTTP client (mocked with `responses`) |
| `test_backend.py` | Policy tick orchestration (mocked HTTP; live Diode SDK types) |
| `test_transform.py` | Entity transform against stubbed Diode SDK classes |
| `test_identity.py` | Naming, switch/AP detection, model mapping |
| `test_bootstrap.py` | NetBox schema bootstrap (mocked REST) |
| `test_urls.py` | HTTPS URL validation |
| `test_openapi_contract.py` | Opt-in checks against downloaded OpenAPI specs |

The contract checks verify the endpoints, pagination parameters, response
keys, and filter fields this worker hardcodes against the two Platform ONE
OpenAPI specs. The specs sit behind the developer portal's login wall, so
the checks run against local downloads: download the Asset Management and
Config State specs from the
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
  `transform.common._cf_text()` funnels this through one place.

## Design notes

### Device status

Aligned with Cisco Meraki device-status mapping:

| Assets `is_connected` | NetBox Device status |
|-----------------------|----------------------|
| `true` | `active` |
| `false` | `offline` |
| missing / unknown | `active` |

### Wireless LAN status and auth

Aligned with Cisco Meraki SSID mapping:

| Assets / ConfigState | NetBox WirelessLAN |
|----------------------|--------------------|
| SSID `enabled` true / unknown | `status` `active` |
| SSID `enabled` false | `status` `disabled` |
| `encryption` open / unknown | `auth_type` `open`, `auth_cipher` `auto` |
| `encryption` PSK / WPA-personal family | `auth_type` `wpa-personal`, cipher `aes` when WPA2+ |
| `encryption` 802.1X / enterprise family | `auth_type` `wpa-enterprise` |
| `encryption` WEP | `auth_type` `wep`, `auth_cipher` `wep` |

### Custom fields

Same `{product}_{attribute}` pattern as Meraki (`meraki_*`) and ACI (`aci_*`) / Catalyst (`catalyst_*`):

| Custom field | Object types | Purpose |
|--------------|--------------|---------|
| `platformone_device_id` | Device | Assets `device_id` (unique) |
| `platformone_interface_id` | Interface | ConfigState `asset_interface_id` (unique) |
| `platformone_cluster_id` | VirtualChassis | InferredCluster UUID (unique) |

### Assurance-ready output

NetBox Assurance is a consumer-side feature: any source that ingests via
Diode surfaces as deviations once an Assurance license is enabled, with no
producer changes. The worker is designed to produce clean, stable Diode
output accordingly:

- **Fixed field set** — human-owned fields are never asserted, so they can
  never generate phantom drift.
- **Stable identity** — Device `name` is Assets `host_name` when present
  (omitted when Platform ONE sends none — no inventing from serial or id);
  `serial` is asserted natively on the NetBox Device, the same approach used
  by the Cisco Meraki integration and NetBox Labs' generic discovery backends.
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
`EXOS`, `VOSS`) with the reported Assets `os_version`, e.g. `Fabric Engine
9.2.1.0`. When only one of the two parts is known, the Platform is that part
alone; devices reporting neither assert no platform.

### Device type model mapping

Assets prefixes `product_type` with `FabricEngine_` for switches running
Fabric Engine OS (e.g. `FabricEngine_5320_48P_8XE`). The [NetBox Device Type
Library](https://github.com/netbox-community/devicetype-library) places that
marker at the end (`5320-48P-8XE-FabricEngine`), so
`identity.device_type_model_for` moves the prefix to a suffix and converts
underscores to hyphens. Values without the prefix pass through unchanged.
When Assets omits `product_type`, no device type is asserted.

### Primary IP

Device `primary_ip4` / `primary_ip6` come only from ConfigState
`retrieve-asset-interface-ip-address` rows that already carry a real prefix
(`address` + `mask_length`):

1. rows with `is_primary: true`
2. else IPs on interfaces flagged `management_port` in port capabilities
3. else an interface IP whose host matches the Assets management address

Assets `ip_address` is a bare host (OpenAPI: dotted decimal). It is used only
to *match* a ConfigState interface IP in step 3 — never asserted as Device
`primary_ip*`, and never padded with `/32` or `/128`.

The worker emits Device entities **without** `primary_ip*` first (so serial and
custom fields apply cleanly), then Interface + IPAddress entities, then a
follow-up Device that asserts `primary_ip4` / `primary_ip6`. That ordering
avoids a Diode apply failure where NetBox rejects an update that sets
`primary_ip*` before the address is assigned to the device — which previously
dropped sibling fields such as `serial` and `platformone_device_id` from the
same change set. If Diode still parallelizes the follow-up ahead of the
IPAddress apply, the primary IP lands on the next Orb tick.

### Switch ports

Every in-scope device whose Assets `function` is a switch OS has its ports
transformed from ConfigState tables joined on `asset_interface_id`
(capabilities join on `(asset_device_id, port_name)`):

- **Admin state and link state are independent fields.** `enabled` reflects
  real administrative state (`AssetPortConfig.enabled`); link state is
  asserted separately as `mark_connected` (`AssetPortState.oper_state`,
  IF-MIB-style 1 = up), so an admin-down port and a link-down port are
  distinguishable in NetBox.
- **Management-only** comes from `AssetPortCapabilities.management_port`
  (`retrieve-asset-port-capabilities`).
- **PoE mode** is `pse` when `AssetPoePowerPortsState.supported` is true;
  otherwise omitted. Admin `enable` on PoE config is not used. PoE
  `classification` / `standard` → `poe_type` is **not** mapped: those
  integers have no verified value table in the OpenAPI spec.
- **VLANs and 802.1Q mode** come only from
  `retrieve-asset-interface-vlan-properties`: `port_vlan` becomes the
  untagged VLAN, the nested VLAN map (minus the untagged VLAN) becomes the
  tagged VLANs, and `mode` is set to `tagged` or `access` accordingly.
  `AssetPortConfig.native_vlan` / `port_mode` are not used as a fallback.
  Extreme reserved internal VIDs **4060–4094** (inclusive) are
  filtered from ingest: they are omitted from Interface `untagged_vlan` /
  `tagged_vlans`; if a port has only reserved memberships after filtering,
  `mode` is omitted too. VLANs are referenced by `vid` with `name=str(vid)`
  (NetBox requires a non-blank name; switch-local names are not used because
  Diode/NetBox VLANs are site-scoped). VLAN groups are not asserted.
- **Interface IP addresses** from `retrieve-asset-interface-ip-address`
  become Diode `IPAddress` entities with `status` `active` (Meraki/Catalyst),
  assigned to the matching interface, using `address` + `mask_length`. Bare
  addresses without a prefix are skipped (no invented `/32` or `/128`). IPs on
  interfaces with no port/LAG row (e.g. SVIs) also emit a minimal `Interface`
  first so the address has a real assigned object; the Interface name comes
  from vlan-properties / port / LAG rows joined on `asset_interface_id`
  (`AssetInterfaceIpAddress` has no interface name in OpenAPI); `type` is
  `virtual`.
- **Speed, duplex, and connector use verified codes only.** ConfigState
  reports `oper_speed`, `oper_duplex`, and `connector_type` as integer codes
  with no value table in its OpenAPI spec. Only codes confirmed against
  production hardware are mapped (`oper_speed 4` = 1 Gbit/s, `oper_duplex 2`
  = full, `connector_type 1/2` = copper/fiber, yielding `1000base-t` /
  `1000base-x-sfp`); unknown codes leave speed/duplex unset but set
  Interface `type` to `other` (NetBox requires a non-blank type; same
  fallback as AP radios / Meraki). Config-side `speed` /
  `duplex` integers are likewise unverified and are not used as fallbacks.
  MACs are uppercased (Meraki posture).

### LAG interfaces and membership

ConfigState `retrieve-asset-lag-config` / `retrieve-asset-lag-state` (batched
by `asset_device_id`, same pattern as ports) map to NetBox LAG interfaces.
Membership is taken from nested `member_ports` on **lag-config** rows only.

- **LAG parent** is an `Interface` with `type=lag`, name from Platform ONE
  `name` (switches auto-generate one; rows without a name are skipped — no
  invented `lag-{n}`), admin `enabled` from a duplicate port-config row when
  present (otherwise default admin-up — Platform ONE's lag-config `enabled`
  is observed always-false for in-service MLTs), and
  `platformone_interface_id` from `asset_interface_id` (the existing interface
  UUID CF). Shared
  joins on that interface id apply vlan-properties (untagged / tagged VLANs by
  `vid` + `name=str(vid)` and 802.1Q `mode`), PoE (`poe_mode`), and interface IP addresses the
  same way as for physical ports. When AssetPortConfig/State also returns the
  LAG's `asset_interface_id`, description, `mark_connected`, and
  `primary_mac_address` are taken from those rows;
  speed/duplex/connector type are not, so `type=lag` is never overwritten.
  Port-table duplicates are not emitted as a second Interface.
- **Members** set Diode `Interface.lag` to the parent LAG (by device + name)
  and otherwise use the full physical-port field set when port
  config/state/capability/PoE/VLAN data exists. Membership comes from
  lag-config `member_ports` only. Members with no port-config/state row are
  not stubbed.
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

A failed lag-config or lag-state extract degrades that table for the tick;
ports still transform from whichever tables survived. VirtualChassis sync is
unchanged and independent.

### Wireless AP radios and WLANs

Devices whose Assets `function` is `AP` (see `identity.is_ap`) get a batched
ConfigState wireless sync alongside the switch-port path (which stays
switch-only). Tables used:

- `retrieve-asset-wireless-interface` / `retrieve-asset-wireless-interface-state`
- `retrieve-asset-ssid-config` / `retrieve-asset-ssid-state`

Each radio becomes a NetBox `Interface` with native RF fields: `rf_role`
always `"ap"` when `type` is an `ieee802.11*` wireless type, `enabled` from
wireless-interface config, `type` from `radio_mode` (including
`ieee802.11be` for Wi‑Fi 7; unknown/missing mode → `other` **without** RF
fields — NetBox rejects `rf_role` / channel fields on non-wireless types),
`tx_power` from `power`, `primary_mac_address` from `bssid` (uppercase),
`rf_channel_frequency` from IEEE channel formulas on `band` + `channel`
(including string labels such as `BAND_5_GHZ`), and `rf_channel_width` when
`channel_width` is already a standard MHz value (20/40/80/160/320). NetBox's
`rf_channel` string is not asserted.

SSIDs become `WirelessLAN` entities (`ssid`, `status`, `auth_type`,
`auth_cipher` — see status/auth tables above). They are deduped by SSID name
across every AP and are **not** site-scoped (same SSID can broadcast in many
sites). Radios link to WLANs via NetBox's native `wireless_lans` field using
`AssetSsid*.if_names` and any `ssid_name` on wireless interface state.

### VirtualChassis from inferred clusters

ConfigState `retrieve-inferred-cluster` returns two-node clusters
(`InferredCluster`: `device_one_id` / `device_two_id` are **InferredDevice**
UUIDs — the schema calls them "User device" — not AssetDevice UUIDs;
`device_one` is the primary). The worker resolves AssetDevice → InferredDevice
via `retrieve-inferred-device` (`asset_device_id`), queries both cluster
member filters with those InferredDevice IDs, remaps members back to
AssetDevice UUIDs, and transforms each complete in-scope pair to a NetBox
`VirtualChassis`:

- **Name** prefers two distinct peer names (`device_one_peer_name` /
  `device_two_peer_name`) so a primary/backup flip does not rename the
  chassis; identical placeholders like `"Default"` fall through to distinct
  member device names. Clusters with no distinct peer or member names are
  skipped (no invented `cluster-{uuid}`). Duplicate computed names across
  clusters are emitted as-is with a warning: NetBox does not unique on
  VirtualChassis name, so identity relies on the unique
  `platformone_cluster_id` custom field rather than name collision rejection.
- **Master** is `device_one`; members get `vc_position` 1 and 2. Device
  membership entities are emitted before the VirtualChassis `master` field so
  a fresh create does not hit NetBox's "master is not assigned to this virtual
  chassis" validation (Diode applies in iterable order within a batch).
- **`platformone_cluster_id`** stores the InferredCluster UUID for stable
  correlation; provenance tags match other synced objects.
- Clusters where either member is missing from the scoped device set are
  skipped. A failed cluster extract degrades to no VirtualChassis for that
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
| Credentials | `XIQ_API_TOKEN` or username/password | `PLATFORMONE_USERNAME` / `PLATFORMONE_PASSWORD`, or `PLATFORMONE_API_TOKEN` |
| Tags | `extreme-networks`, `xiq`, `discovered` | `extreme-networks`, `platform-one`, `discovered` |
| Custom fields | `xiq_network_policy`, `xiq_port_id` | `platformone_device_id`, `platformone_interface_id`, `platformone_cluster_id` |
| Port admin state / VLANs | not available | `enabled`, `mark_connected`, untagged/tagged VLANs by `vid` + `name=str(vid)`, 802.1Q `mode` |
| Wireless radios / WLANs | synced (XIQ-era fields) | synced (ConfigState wireless + SSID → native Interface RF fields + WirelessLAN) |
| Internal layout | monolithic modules | ETL packages: `client` → `extract` → `transform` |

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
  `connector_type` / wireless `radio_mode` / `encryption`) as more hardware
  is observed.
- LACP attributes on LAG parents (`mode` / `lacp_key` / `load_balance_algo`)
  once OpenAPI publishes integer enums or Diode/NetBox gains matching fields.
- MLAG peer correlation (`retrieve-asset-mlag-*`), if NetBox modeling for
  multi-chassis LAGs is needed beyond single-device LAG membership.
- NetBox `rf_channel` string on radios, and WirelessLAN `scope_site`, once
  live Platform ONE values and NetBox formats are confirmed.
