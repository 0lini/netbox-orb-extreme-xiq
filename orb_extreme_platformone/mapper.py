"""Map Extreme Platform ONE records to Diode entities.

Fields are asserted unconditionally whenever Platform ONE reports the
underlying data; fields with no Platform ONE equivalent are never asserted.
Device identity uses the native `serial` field plus deterministic names
(see `identity`), with `platformone_*` custom fields carried as provenance.

Callers pass "device records" pre-joined by backend.py:
{"asset": <Assets Device>, "cs_device_id": str | None,
 "cs_device": <ConfigState AssetDevice> | None,
 "location": <AssetLocation> | None}.

InferredCluster rows (ConfigState retrieve-inferred-cluster) map to
VirtualChassis via `virtual_chassis_to_entities`. LAG interfaces and
membership come from AssetLagConfig / AssetLagState (and nested or
fetched member ports) via `ports_to_entities`. AP radios and WLANs come
from wireless-interface / SSID tables via `radios_to_entities`.
"""

from __future__ import annotations

import ipaddress
import logging
from collections import defaultdict

from netboxlabs.diode.sdk.ingester import (
    VLAN,
    CustomFieldValue,
    Device,
    DeviceRole,
    DeviceType,
    Entity,
    Interface,
    IPAddress,
    Location,
    Platform,
    Site,
    VirtualChassis,
    WirelessLAN,
)

from . import bootstrap
from .identity import (
    SLASH_PORT_FUNCTIONS,
    device_name,
    device_type_model_for,
    expand_location_paths,
    native_port_name,
    platform_name,
    resolve_location,
    role_for,
)

__all__ = [
    "PORT_ENTITY_TABLE_KEYS",
    "WIRELESS_ENTITY_TABLE_KEYS",
    "devices_to_entities",
    "ports_to_entities",
    "primary_ips_from_tables",
    "radios_to_entities",
    "scope_devices",
    "virtual_chassis_to_entities",
]

logger = logging.getLogger(__name__)

MANUFACTURER = "Extreme Networks"

PROVENANCE_TAGS = [tag["name"] for tag in bootstrap.TAGS]

# Extreme Networks reserves VIDs 4060–4094 for internal use (e.g. Fabric
# Engine). These are filtered from Interface untagged/tagged memberships.
EXTREME_RESERVED_VLAN_VID_MIN = 4060
EXTREME_RESERVED_VLAN_VID_MAX = 4094


def _is_extreme_reserved_vlan(vid: int) -> bool:
    """True for Extreme reserved internal VLAN IDs (4060–4094 inclusive)."""
    return EXTREME_RESERVED_VLAN_VID_MIN <= vid <= EXTREME_RESERVED_VLAN_VID_MAX


# Keys `ports_to_entities` reads from its `tables` dict. Kept in sync with
# backend.PORT_TABLES ∪ backend.INTERFACE_ID_TABLES (see unit test).
PORT_ENTITY_TABLE_KEYS = frozenset(
    {
        "port_configs",
        "port_states",
        "vlan_properties",
        "lag_configs",
        "lag_states",
        "port_capabilities",
        "poe_configs",
        "poe_states",
        "interface_ips",
    }
)

# Keys `radios_to_entities` reads from each device's wireless tables dict.
WIRELESS_ENTITY_TABLE_KEYS = frozenset(
    {
        "wireless_interfaces",
        "wireless_states",
        "ssid_configs",
        "ssid_states",
    }
)


def _status_for(asset: dict) -> str | None:
    """Map Assets `is_connected` to Device status; omit when unknown.

    Only an explicit bool is asserted — a missing/null value must not become
    ``offline`` the way a falsy check would.
    """
    connected = asset.get("is_connected")
    if connected is True:
        return "active"
    if connected is False:
        return "offline"
    return None


def _primary_ips_from_asset(asset: dict) -> dict[str, str]:
    """Split Assets `ip_address` into primary_ip4 vs primary_ip6.

    Assets reports a bare host with no mask. Inventing /32 or /128 would
    create misleading host prefixes in NetBox, so a bare address is skipped.
    Only values that already include a prefix length are asserted (fallback
    when ConfigState did not yield a primary). Invalid values assert nothing.
    """
    raw = (asset.get("ip_address") or "").strip()
    if not raw or "/" not in raw:
        return {}
    try:
        iface = ipaddress.ip_interface(raw)
    except ValueError:
        return {}
    key = "primary_ip4" if iface.version == 4 else "primary_ip6"
    return {key: str(iface)}


def _mgmt_interface_ids(tables: dict[str, list[dict]]) -> set[str]:
    """Interface UUIDs flagged management_port via port capabilities + port rows."""
    mgmt_ports = {
        (str(cap.get("asset_device_id") or ""), str(cap.get("port_name") or ""))
        for cap in tables.get("port_capabilities") or []
        if cap.get("management_port") is True and cap.get("port_name")
    }
    if not mgmt_ports:
        return set()
    ids: set[str] = set()
    for row in (*(tables.get("port_configs") or []), *(tables.get("port_states") or [])):
        key = (str(row.get("asset_device_id") or ""), str(row.get("name") or ""))
        interface_id = str(row.get("asset_interface_id") or "")
        if interface_id and key in mgmt_ports:
            ids.add(interface_id)
    return ids


def _pick_primary_cidr(candidates: list[tuple[int, str]]) -> dict[str, str]:
    """Keep the first CIDR per address family from ranked candidates."""
    result: dict[str, str] = {}
    for version, cidr in candidates:
        key = "primary_ip4" if version == 4 else "primary_ip6"
        result.setdefault(key, cidr)
    return result


def primary_ips_from_tables(
    tables: dict[str, list[dict]],
    *,
    asset_ip: str | None = None,
) -> dict[str, str]:
    """Derive Device primary_ip4/primary_ip6 from ConfigState interface IPs.

    Prefers rows with ``is_primary`` True, then IPs on ``management_port``
    interfaces, then an interface IP whose host matches Assets ``ip_address``.
    Every candidate must have a real prefix (``mask_length`` / CIDR); bare
    hosts are never padded with /32 or /128.
    """
    rows_with_cidr: list[tuple[dict, str, ipaddress.IPv4Interface | ipaddress.IPv6Interface]] = []
    for row in tables.get("interface_ips") or []:
        raw = str(row.get("address") or "").strip()
        mask = row.get("mask_length")
        # Require an explicit prefix from ConfigState (mask_length or inline /n);
        # never accept ip_interface's implicit /32 or /128 on a bare host.
        if not raw or ("/" not in raw and not (isinstance(mask, int) and 0 <= mask <= 128)):
            continue
        cidr = _interface_ip_cidr(row)
        if not cidr:
            continue
        try:
            iface = ipaddress.ip_interface(cidr)
        except ValueError:
            continue
        rows_with_cidr.append((row, cidr, iface))

    if not rows_with_cidr:
        return {}

    ranked: list[tuple[int, str]] = []
    for row, cidr, iface in rows_with_cidr:
        if row.get("is_primary") is True:
            ranked.append((iface.version, cidr))
    if ranked:
        return _pick_primary_cidr(ranked)

    mgmt_ids = _mgmt_interface_ids(tables)
    if mgmt_ids:
        for row, cidr, iface in rows_with_cidr:
            if str(row.get("asset_interface_id") or "") in mgmt_ids:
                ranked.append((iface.version, cidr))
        if ranked:
            return _pick_primary_cidr(ranked)

    asset_host = (asset_ip or "").strip()
    if asset_host and "/" in asset_host:
        asset_host = asset_host.split("/", 1)[0]
    if asset_host:
        for row, cidr, iface in rows_with_cidr:
            if str(iface.ip) == asset_host or str(row.get("address") or "").split("/", 1)[0] == asset_host:
                ranked.append((iface.version, cidr))
        if ranked:
            return _pick_primary_cidr(ranked)

    return {}


def _cf_text(value: str) -> CustomFieldValue:
    return CustomFieldValue(text=value)


def _coord(value) -> float | None:
    """Return a finite float coordinate, or None when unset/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _site_kwargs(site_name: str, coords: tuple[float | None, float | None] | None) -> dict:
    kwargs: dict = {"name": site_name}
    if coords:
        lat, lon = coords
        if lat is not None:
            kwargs["latitude"] = lat
        if lon is not None:
            kwargs["longitude"] = lon
    return kwargs


def _device_kwargs(
    asset: dict,
    *,
    site_name: str,
    location: Location | None,
    name_source: str,
    cs_device: dict | None = None,
    vc_membership: dict | None = None,
    primary_ips: dict[str, str] | None = None,
) -> dict:
    custom_fields: dict = {}
    if asset.get("device_id") is not None:
        custom_fields["platformone_device_id"] = _cf_text(str(asset["device_id"]))

    kwargs = {
        "name": device_name(asset, name_source),
        "serial": asset.get("serial_number") or None,
        "site": Site(name=site_name),
        "custom_fields": custom_fields,
        "tags": PROVENANCE_TAGS,
    }
    status = _status_for(asset)
    if status is not None:
        kwargs["status"] = status
    if location is not None:
        kwargs["location"] = location

    role = role_for(asset.get("function"))
    if role:
        role_name, role_slug = role
        kwargs["role"] = DeviceRole(name=role_name, slug=role_slug)

    # Assets product_type / os_version preferred; ConfigState model_name /
    # firmware_version fill gaps when Assets omitted them.
    cs = cs_device or {}
    product_type = asset.get("product_type") or cs.get("model_name")
    if product_type:
        kwargs["device_type"] = DeviceType(
            model=device_type_model_for(product_type), manufacturer=MANUFACTURER
        )
        kwargs["manufacturer"] = MANUFACTURER
    os_version = asset.get("os_version") or cs.get("firmware_version")
    platform = platform_name(asset.get("function"), os_version)
    if platform:
        kwargs["platform"] = Platform(name=platform, manufacturer=MANUFACTURER)
    # ConfigState interface IPs (with mask_length) win; Assets CIDR is fallback.
    kwargs.update(primary_ips or _primary_ips_from_asset(asset))
    if vc_membership:
        kwargs["virtual_chassis"] = VirtualChassis(name=vc_membership["name"])
        kwargs["vc_position"] = vc_membership["position"]
    return kwargs


def _virtual_chassis_name(cluster: dict, device_one_name: str, device_two_name: str) -> str:
    """Stable VirtualChassis name from InferredCluster peer names or member device names.

    Requires two distinct peer names so a shared placeholder like "Default" does
    not collapse every chassis to the same NetBox name. Falls back to distinct
    member device names, then the cluster UUID.
    """
    peers = sorted(
        {name for name in (cluster.get("device_one_peer_name"), cluster.get("device_two_peer_name")) if name}
    )
    if len(peers) >= 2:
        return " / ".join(peers)
    members = sorted({device_one_name, device_two_name})
    if len(members) >= 2:
        return " / ".join(members)
    return f"cluster-{cluster.get('id')}"


def virtual_chassis_to_entities(
    clusters: list[dict],
    *,
    records_by_cs_id: dict[str, dict],
    name_source: str = "hostname",
) -> tuple[list[Entity], dict[str, dict]]:
    """Map ConfigState InferredCluster rows to VirtualChassis entities + memberships.

    `device_one_id` / `device_two_id` must already be AssetDevice UUIDs
    (backend remaps from InferredDevice IDs). Both members must be present in
    `records_by_cs_id` (already site-scoped); partial clusters are skipped so
    Diode never creates an orphan half-chassis.

    Returns (VC entities, {cs_device_id: {"name", "position"}}) for
    `devices_to_entities` to attach `virtual_chassis` / `vc_position`.
    device_one is the primary/master per the InferredCluster schema.
    """
    entities: list[Entity] = []
    memberships: dict[str, dict] = {}
    used_names: set[str] = set()

    for cluster in clusters:
        one_id = str(cluster.get("device_one_id") or "")
        two_id = str(cluster.get("device_two_id") or "")
        if not one_id or not two_id:
            continue
        record_one = records_by_cs_id.get(one_id)
        record_two = records_by_cs_id.get(two_id)
        if record_one is None or record_two is None:
            continue

        name_one = device_name(record_one["asset"], name_source)
        name_two = device_name(record_two["asset"], name_source)
        chassis_name = _virtual_chassis_name(cluster, name_one, name_two)
        # Colliding names are emitted as-is: the unique platformone_cluster_id
        # custom field makes NetBox reject the merge at ingest, surfacing the
        # upstream data problem (e.g. stale Assets hostnames) instead of
        # hiding it behind an invented suffix.
        if chassis_name in used_names:
            logger.warning(
                "Duplicate VirtualChassis name %r (cluster %s); NetBox uniqueness will reject it at ingest",
                chassis_name,
                cluster.get("id"),
            )
        used_names.add(chassis_name)

        vc_kwargs: dict = {
            "name": chassis_name,
            "master": name_one,
            "tags": PROVENANCE_TAGS,
        }
        if cluster.get("id"):
            vc_kwargs["custom_fields"] = {"platformone_cluster_id": _cf_text(str(cluster["id"]))}
        entities.append(Entity(virtual_chassis=VirtualChassis(**vc_kwargs)))

        memberships[one_id] = {"name": chassis_name, "position": 1}
        memberships[two_id] = {"name": chassis_name, "position": 2}

    return entities, memberships


def _iter_scoped_devices(records: list[dict], *, site_scope: set[str] | None):
    """Yield (record, site_name, location_path) for devices that pass scope.

    Single resolve_location pass used by both `scope_devices` and
    `devices_to_entities`. Platform ONE assigns every device a site itself, so
    a record without one is unexpected and skipped (with a warning).
    """
    for record in records:
        site_name, location_path = resolve_location(record.get("location"), record["asset"])
        if site_name is None:
            asset = record["asset"]
            logger.warning(
                "Skipping device %s: Platform ONE reports no site for it",
                asset.get("host_name") or asset.get("serial_number") or asset.get("device_id"),
            )
            continue
        if site_scope and site_name not in site_scope:
            continue
        yield record, site_name, location_path


def scope_devices(records: list[dict], *, site_scope: set[str] | None) -> list[dict]:
    """Return the device records whose resolved site is in site_scope (all, if no scope).

    Ownership: the backend scopes once up front (port fan-out must match the
    device list). Pass the result to `devices_to_entities` with
    `site_scope=None` so mapping does not re-filter by site. Direct callers
    that have not scoped yet may pass `site_scope` into `devices_to_entities`
    instead.
    """
    return [record for record, _, _ in _iter_scoped_devices(records, site_scope=site_scope)]


def _merge_site_coords(
    site_coords: dict[str, tuple[float | None, float | None]],
    site_name: str,
    location: dict | None,
) -> None:
    """Keep the first non-null lat/lon seen per site name."""
    if not location:
        return
    lat = _coord(location.get("site_latitude"))
    lon = _coord(location.get("site_longitude"))
    if lat is None and lon is None:
        return
    existing = site_coords.get(site_name)
    if existing is None:
        site_coords[site_name] = (lat, lon)
        return
    prev_lat, prev_lon = existing
    site_coords[site_name] = (
        prev_lat if prev_lat is not None else lat,
        prev_lon if prev_lon is not None else lon,
    )


def devices_to_entities(
    records: list[dict],
    *,
    name_source: str = "hostname",
    site_scope: set[str] | None = None,
    virtual_chassis_entities: list[Entity] | None = None,
    vc_memberships: dict[str, dict] | None = None,
    primary_ips_by_cs_id: dict[str, dict[str, str]] | None = None,
) -> list[Entity]:
    """Map device records to Diode entities: one Site per distinct site, one
    nested Location per Building/Floor level in use, VirtualChassis (if any),
    then one Device per device.

    When the caller has already run `scope_devices` (backend tick path), pass
    `site_scope=None` so this does not re-filter by site. When calling
    directly with an unscoped list, pass `site_scope` here instead.

    `vc_memberships` is keyed by ConfigState device UUID (`cs_device_id`).
    `primary_ips_by_cs_id` supplies Device primary_ip4/primary_ip6 from
    ConfigState interface IPs (see `primary_ips_from_tables`); when absent,
    Assets `ip_address` is used only if it already includes a prefix.
    VirtualChassis entities are emitted after locations and before devices so
    Diode can resolve membership references in the same ingest batch.
    """
    entities: list[Entity] = []
    resolved: list[tuple[dict, str, list[str]]] = []
    site_names: set[str] = set()
    location_paths: set[tuple[str, tuple[str, ...]]] = set()
    site_coords: dict[str, tuple[float | None, float | None]] = {}

    # One pass: filter (if site_scope set) + resolve. Does not call
    # scope_devices separately (avoids a second filter pass inside the mapper).
    for record, site_name, location_path in _iter_scoped_devices(records, site_scope=site_scope):
        resolved.append((record, site_name, location_path))
        site_names.add(site_name)
        _merge_site_coords(site_coords, site_name, record.get("location"))
        if location_path:
            location_paths.add((site_name, tuple(location_path)))

    for site_name in sorted(site_names):
        entities.append(Entity(site=Site(**_site_kwargs(site_name, site_coords.get(site_name)))))

    # expand_location_paths orders ancestors before descendants, so one pass
    # can thread `parent` through the cache.
    location_cache: dict[tuple[str, tuple[str, ...]], Location] = {}
    for site_name, path in expand_location_paths(location_paths):
        parent = location_cache.get((site_name, path[:-1])) if len(path) > 1 else None
        location = Location(name=path[-1], site=site_name, parent=parent)
        location_cache[(site_name, path)] = location
        entities.append(Entity(location=location))

    if virtual_chassis_entities:
        entities.extend(virtual_chassis_entities)

    for record, site_name, location_path in resolved:
        location = location_cache.get((site_name, tuple(location_path))) if location_path else None
        cs_device_id = record.get("cs_device_id")
        membership = (vc_memberships or {}).get(cs_device_id) if cs_device_id else None
        primary_ips = (primary_ips_by_cs_id or {}).get(cs_device_id) if cs_device_id else None
        kwargs = _device_kwargs(
            record["asset"],
            site_name=site_name,
            location=location,
            name_source=name_source,
            cs_device=record.get("cs_device"),
            vc_membership=membership,
            primary_ips=primary_ips,
        )
        entities.append(Entity(device=Device(**kwargs)))

    return entities


# ConfigState reports oper_speed / oper_duplex / connector_type as integer
# codes with no value table in its OpenAPI spec. Only codes verified against
# production hardware (or fixtures derived from that gear) are mapped;
# unknown codes assert nothing. oper_state is the exception: its schema
# description matches IF-MIB ifOperStatus.
#
# Verified in-repo today: oper_speed 4, oper_duplex 2, connector_type 1/2.
# Config-side speed/duplex integers remain unverified and are not used.
VERIFIED_OPER_SPEED_KBPS = {4: 1_000_000}
VERIFIED_DUPLEX = {2: "full"}
OPER_STATE_UP = 1

# (oper_speed, connector_type) -> NetBox interface type. connector_type:
# 1 = copper, 2 = fiber. Unlisted combinations leave `type` unset.
_TYPE_BY_SPEED_AND_CONNECTOR = {
    (4, 1): "1000base-t",
    (4, 2): "1000base-x-sfp",
}


def _record_key(record: dict) -> str:
    """Join key across ConfigState port tables: the row's asset_interface_id."""
    return str(record.get("asset_interface_id") or "")


def _by_key(records: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        key = _record_key(record)
        if key:
            grouped[key].append(record)
    return grouped


def _first_row(grouped: dict[str, list[dict]], key: str, *, table: str) -> dict:
    """First row for a join key, or `{}` when the key is absent.

    Warns when multiple rows share the key: callers that take only the first
    row would otherwise silently drop siblings.
    """
    rows = grouped.get(key)
    if not rows:
        return {}
    if len(rows) > 1:
        logger.warning(
            "Multiple %s rows share join key %r (%d rows); using the first",
            table,
            key,
            len(rows),
        )
    return rows[0]


def _optional_first_row(grouped: dict[str, list[dict]], key: str, *, table: str) -> dict | None:
    """First row when the key is present, else None (distinguishes missing PoE)."""
    if key not in grouped:
        return None
    return _first_row(grouped, key, table=table)


def _capabilities_by_port(records: list[dict]) -> dict[tuple[str, str], dict]:
    """Index AssetPortCapabilities by (asset_device_id, port_name).

    Capabilities have no asset_interface_id. `port_name` alone is reused across
    every switch (e.g. `1:43`), so the ConfigState device id must be part of
    the join key — same device scope other port tables get from backend
    bucketing / asset_interface_id.
    """
    by_key: dict[tuple[str, str], dict] = {}
    for record in records:
        name = record.get("port_name")
        if not name:
            continue
        device_id = str(record.get("asset_device_id") or "")
        key = (device_id, str(name))
        if key in by_key:
            logger.warning(
                "Multiple port_capabilities rows share port_name %r on device %r; using the first",
                str(name),
                device_id or "?",
            )
            continue
        by_key[key] = record
    return by_key


def _vlan_fields(vlan_records: list[dict]) -> dict:
    """untagged_vlan / tagged_vlans / mode from AssetInterfaceVlanProperties rows.

    `port_vlan` is the untagged VLAN; the nested `vlans` list is every VLAN
    mapped onto the interface, so the tagged set is that list minus the
    untagged VLAN. Extreme reserved VIDs (4060–4094) are omitted from both.
    Interfaces with no VLAN rows — or only reserved VIDs after filtering —
    assert none of the three: on Fabric Engine a port can be mapped straight
    into an I-SID instead of a VLAN, and inventing an access mode would
    misrepresent configuration. VLAN refs are bare `vid` only (names are
    switch-local and Diode/NetBox VLANs are site-scoped).
    """
    untagged: int | None = None
    mapped: set[int] = set()
    for record in vlan_records:
        port_vlan = record.get("port_vlan")
        if untagged is None and isinstance(port_vlan, int) and port_vlan > 0:
            untagged = port_vlan
        for vlan_map in record.get("vlans") or []:
            number = vlan_map.get("vlan_number") if isinstance(vlan_map, dict) else None
            if isinstance(number, int) and number > 0:
                mapped.add(number)
    if untagged is not None and _is_extreme_reserved_vlan(untagged):
        untagged = None
    tagged = sorted(
        vid
        for vid in (mapped - {untagged} if untagged is not None else mapped)
        if not _is_extreme_reserved_vlan(vid)
    )

    fields: dict = {}
    if untagged is not None:
        fields["untagged_vlan"] = VLAN(vid=untagged)
    if tagged:
        fields["tagged_vlans"] = [VLAN(vid=vid) for vid in tagged]
        fields["mode"] = "tagged"
    elif untagged is not None:
        fields["mode"] = "access"
    return fields


def _vlan_fields_from_port_config(config: dict) -> dict:
    """Fallback VLANs from AssetPortConfig when vlan-properties rows are absent.

    `native_vlan` is the untagged VLAN on a trunk; `port_mode` True enables
    tagging (Fabric Engine). Applied only as a fallback. Extreme reserved
    VIDs (4060–4094) are omitted entirely (no VLAN fields, no mode).
    """
    native = config.get("native_vlan")
    if not isinstance(native, int) or native <= 0 or _is_extreme_reserved_vlan(native):
        return {}
    fields: dict = {"untagged_vlan": VLAN(vid=native)}
    port_mode = config.get("port_mode")
    if port_mode is True:
        fields["mode"] = "tagged"
    elif port_mode is False:
        fields["mode"] = "access"
    return fields


def _poe_mode(config: dict, state: dict) -> str | None:
    """NetBox poe_mode=pse when the port is a PoE PSE; omit otherwise.

    `supported` (state) is authoritative; `enable` (config) True also implies
    PSE. classification/standard → poe_type is intentionally not mapped:
    OpenAPI has no verified value table for those integers.
    """
    if state.get("supported") is True:
        return "pse"
    if config.get("enable") is True:
        return "pse"
    return None


def _interface_ip_cidr(row: dict) -> str | None:
    """Build address/prefix for AssetInterfaceIpAddress → Diode IPAddress.

    `address` is a bare address and `mask_length` its prefix length. Without
    an explicit prefix (inline ``/n`` or usable ``mask_length``), return
    None — never invent /32 or /128.
    """
    raw = str(row.get("address") or "").strip()
    if not raw:
        return None
    mask = row.get("mask_length")
    if "/" not in raw:
        if not (isinstance(mask, int) and 0 <= mask <= 128):
            return None
        raw = f"{raw}/{mask}"
    try:
        return str(ipaddress.ip_interface(raw))
    except ValueError:
        return None


def _iface_base_kwargs(
    *,
    device: str,
    name: str,
    interface_id: str | None,
    config: dict,
    poe_config: dict | None = None,
    poe_state: dict | None = None,
) -> dict:
    """Shared identity / admin / PoE fields for physical ports and LAG parents."""
    kwargs: dict = {
        "device": device,
        "name": name,
        "custom_fields": {"platformone_interface_id": _cf_text(interface_id)} if interface_id else {},
        "tags": PROVENANCE_TAGS,
    }
    enabled = config.get("enabled")
    if isinstance(enabled, bool):
        kwargs["enabled"] = enabled
    poe = _poe_mode(poe_config or {}, poe_state or {})
    if poe is not None:
        kwargs["poe_mode"] = poe
    return kwargs


def _port_kwargs(
    *,
    device: str,
    name: str,
    interface_id: str | None,
    config: dict,
    state: dict,
    vlan_records: list[dict],
    capability: dict | None = None,
    poe_config: dict | None = None,
    poe_state: dict | None = None,
) -> dict:
    kwargs = _iface_base_kwargs(
        device=device,
        name=name,
        interface_id=interface_id,
        config=config,
        poe_config=poe_config,
        poe_state=poe_state,
    )

    # Link state maps to mark_connected, never to `enabled` -- admin state is
    # asserted separately above.
    oper_state = state.get("oper_state")
    if oper_state is not None:
        kwargs["mark_connected"] = oper_state == OPER_STATE_UP

    speed = VERIFIED_OPER_SPEED_KBPS.get(state.get("oper_speed"))
    if speed is not None:
        kwargs["speed"] = speed
    duplex = VERIFIED_DUPLEX.get(state.get("oper_duplex"))
    if duplex is not None:
        kwargs["duplex"] = duplex
    port_type = _TYPE_BY_SPEED_AND_CONNECTOR.get((state.get("oper_speed"), state.get("connector_type")))
    if port_type is not None:
        kwargs["type"] = port_type

    if config.get("description"):
        kwargs["description"] = config["description"]
    if state.get("mac_address"):
        kwargs["primary_mac_address"] = state["mac_address"]

    if capability is not None and isinstance(capability.get("management_port"), bool):
        kwargs["mgmt_only"] = capability["management_port"]

    vlan_fields = _vlan_fields(vlan_records)
    if not vlan_fields:
        vlan_fields = _vlan_fields_from_port_config(config)
    kwargs.update(vlan_fields)
    return kwargs


def _lag_name(config: dict, state: dict) -> str | None:
    """LAG Interface name: prefer `name`, else `lag-{lag_number}`."""
    name = config.get("name") or state.get("name")
    if name:
        return str(name)
    lag_number = config.get("lag_number") or state.get("lag_number")
    if lag_number is not None and str(lag_number):
        return f"lag-{lag_number}"
    return None


def _member_interface_names(lag_row: dict) -> list[str]:
    """Member port names from a nested `member_ports` list on a LAG row."""
    names: list[str] = []
    seen: set[str] = set()
    for member in lag_row.get("member_ports") or []:
        if not isinstance(member, dict):
            continue
        name = member.get("interface_name")
        if name and str(name) not in seen:
            seen.add(str(name))
            names.append(str(name))
    return names


def _lag_membership(configs: list[dict], states: list[dict]) -> dict[str, str]:
    """Map member interface name → LAG interface name.

    Config membership is authoritative; state membership fills gaps when
    config rows omit nested members.
    """
    membership: dict[str, str] = {}
    for config in configs:
        lag = _lag_name(config, {})
        if not lag:
            continue
        for member in _member_interface_names(config):
            membership.setdefault(member, lag)
    for state in states:
        lag = _lag_name({}, state)
        if not lag:
            continue
        for member in _member_interface_names(state):
            membership.setdefault(member, lag)
    return membership


def _lag_kwargs(
    *,
    device: str,
    name: str,
    interface_id: str | None,
    config: dict,
    vlan_records: list[dict],
    poe_config: dict | None = None,
    poe_state: dict | None = None,
    port_config: dict | None = None,
    port_state: dict | None = None,
) -> dict:
    """Build Diode kwargs for a LAG parent interface.

    Native fields from AssetLagConfig/State: `type=lag`, name, admin
    `enabled`, `platformone_interface_id` (`asset_interface_id`). Shared joins
    on that interface id fill VLAN trunk/access, PoE, and (separately)
    IPAddress entities. When port config/state also lists the same id, pull
    fields lag tables lack (`description`, `mark_connected`, MAC, port-config
    VLAN fallback) — never speed/duplex/connector `type`, which would
    overwrite `type=lag`.

    AssetLagConfig also carries LACP `mode` / `lacp_key` / `load_balance_algo`
    / `dynamic`, but Diode's Interface has no matching fields and the mode /
    algo integers have no published value table — leave them unmapped (see
    README). `lag_number` is naming-only (`lag-{n}` fallback); it is not a
    second custom field (redundant with `platformone_interface_id`).
    """
    kwargs = _iface_base_kwargs(
        device=device,
        name=name,
        interface_id=interface_id,
        config=config,
        poe_config=poe_config,
        poe_state=poe_state,
    )
    kwargs["type"] = "lag"

    vlan_fields = _vlan_fields(vlan_records)
    if not vlan_fields and port_config:
        vlan_fields = _vlan_fields_from_port_config(port_config)
    kwargs.update(vlan_fields)

    if port_config and port_config.get("description"):
        kwargs["description"] = port_config["description"]
    if port_state:
        oper_state = port_state.get("oper_state")
        if oper_state is not None:
            kwargs["mark_connected"] = oper_state == OPER_STATE_UP
        if port_state.get("mac_address"):
            kwargs["primary_mac_address"] = port_state["mac_address"]
    return kwargs


def _ip_entities_for_interface(
    *,
    device: str,
    interface_name: str,
    rows: list[dict],
) -> list[Entity]:
    entities: list[Entity] = []
    seen: set[str] = set()
    for row in rows:
        cidr = _interface_ip_cidr(row)
        if not cidr or cidr in seen:
            continue
        seen.add(cidr)
        entities.append(
            Entity(
                ip_address=IPAddress(
                    address=cidr,
                    assigned_object_interface=Interface(device=device, name=interface_name),
                    tags=PROVENANCE_TAGS,
                )
            )
        )
    return entities


def _lag_entities(
    *,
    device: str,
    lag_configs: list[dict],
    lag_states: list[dict],
    vlans: dict[str, list[dict]],
    poe_configs: dict[str, list[dict]],
    poe_states: dict[str, list[dict]],
    interface_ips: dict[str, list[dict]],
    port_configs: dict[str, list[dict]] | None = None,
    port_states: dict[str, list[dict]] | None = None,
) -> tuple[list[Entity], set[str], set[str], dict[str, str], dict[str, str]]:
    """Emit LAG parent interfaces. Returns entities plus join bookkeeping."""
    lag_configs_by_key = _by_key(lag_configs)
    lag_states_by_key = _by_key(lag_states)
    lag_keys = set(lag_configs_by_key) | set(lag_states_by_key)
    port_configs = port_configs or {}
    port_states = port_states or {}

    lag_interface_ids = {
        str(record["asset_interface_id"])
        for record in (*lag_configs, *lag_states)
        if record.get("asset_interface_id")
    }
    membership = _lag_membership(lag_configs, lag_states)
    lag_names: set[str] = set()
    entities: list[Entity] = []
    emitted_keys: dict[str, str] = {}

    for key in sorted(lag_keys):
        config = _first_row(lag_configs_by_key, key, table="lag_configs")
        state = _first_row(lag_states_by_key, key, table="lag_states")
        name = _lag_name(config, state)
        if not name:
            continue
        lag_names.add(name)
        interface_id = config.get("asset_interface_id") or state.get("asset_interface_id")
        kwargs = _lag_kwargs(
            device=device,
            name=name,
            interface_id=str(interface_id) if interface_id else None,
            config=config,
            vlan_records=vlans.get(key, []),
            poe_config=_optional_first_row(poe_configs, key, table="poe_configs"),
            poe_state=_optional_first_row(poe_states, key, table="poe_states"),
            port_config=_optional_first_row(port_configs, key, table="port_configs"),
            port_state=_optional_first_row(port_states, key, table="port_states"),
        )
        entities.append(Entity(interface=Interface(**kwargs)))
        emitted_keys[key] = name
        entities.extend(
            _ip_entities_for_interface(device=device, interface_name=name, rows=interface_ips.get(key, []))
        )

    return entities, lag_names, lag_interface_ids, membership, emitted_keys


def _physical_port_entities(
    *,
    device: str,
    configs: dict[str, list[dict]],
    states: dict[str, list[dict]],
    vlans: dict[str, list[dict]],
    capabilities: dict[tuple[str, str], dict],
    poe_configs: dict[str, list[dict]],
    poe_states: dict[str, list[dict]],
    interface_ips: dict[str, list[dict]],
    lag_names: set[str],
    lag_interface_ids: set[str],
    membership: dict[str, str],
) -> tuple[list[Entity], set[str], dict[str, str]]:
    """Emit physical (non-LAG) port interfaces joined on asset_interface_id."""
    entities: list[Entity] = []
    emitted_port_names: set[str] = set(lag_names)
    emitted_keys: dict[str, str] = {}

    for key in sorted(set(configs) | set(states)):
        config = _first_row(configs, key, table="port_configs")
        state = _first_row(states, key, table="port_states")
        name = str(config.get("name") or state.get("name") or "")
        if not name:
            continue
        interface_id = config.get("asset_interface_id") or state.get("asset_interface_id")
        # Skip rows that are the LAG interface itself (same asset_interface_id).
        if interface_id and str(interface_id) in lag_interface_ids:
            continue
        if name in lag_names:
            continue
        port_device_id = str(config.get("asset_device_id") or state.get("asset_device_id") or "")
        kwargs = _port_kwargs(
            device=device,
            name=name,
            interface_id=str(interface_id) if interface_id else None,
            config=config,
            state=state,
            vlan_records=vlans.get(key, []),
            capability=capabilities.get((port_device_id, name)),
            poe_config=_optional_first_row(poe_configs, key, table="poe_configs"),
            poe_state=_optional_first_row(poe_states, key, table="poe_states"),
        )
        lag_parent = membership.get(name)
        if lag_parent:
            kwargs["lag"] = Interface(device=device, name=lag_parent)
        entities.append(Entity(interface=Interface(**kwargs)))
        emitted_port_names.add(name)
        emitted_keys[key] = name
        entities.extend(
            _ip_entities_for_interface(device=device, interface_name=name, rows=interface_ips.get(key, []))
        )

    return entities, emitted_port_names, emitted_keys


def _orphan_member_entities(
    *,
    device: str,
    membership: dict[str, str],
    emitted_port_names: set[str],
) -> list[Entity]:
    """Members known only from LAG membership (no port-config/state row yet)."""
    entities: list[Entity] = []
    for member_name, lag_parent in sorted(membership.items()):
        if member_name in emitted_port_names:
            continue
        entities.append(
            Entity(
                interface=Interface(
                    device=device,
                    name=member_name,
                    lag=Interface(device=device, name=lag_parent),
                    tags=PROVENANCE_TAGS,
                )
            )
        )
        emitted_port_names.add(member_name)
    return entities


def _orphan_ip_entities(
    *,
    device: str,
    interface_ips: dict[str, list[dict]],
    emitted_keys: dict[str, str],
) -> list[Entity]:
    """IPs on interfaces that got no Interface entity above (e.g. VLAN/SVI
    interfaces, which appear in vlan_properties but not the port tables).

    Emits a minimal Interface first so the IPAddress has a real assigned
    object, then the IP entities. Interface ``type`` is left unset (no
    verified SVI/virtual enum from ConfigState).
    """
    entities: list[Entity] = []
    emitted_names: set[str] = set()
    for key, rows in sorted(interface_ips.items()):
        if key in emitted_keys:
            continue
        name = next((row["interface_name"] for row in rows if row.get("interface_name")), None)
        if not name:
            continue
        name = str(name)
        if name not in emitted_names:
            interface_id = next(
                (str(row["asset_interface_id"]) for row in rows if row.get("asset_interface_id")),
                key or None,
            )
            iface_kwargs: dict = {
                "device": device,
                "name": name,
                "tags": PROVENANCE_TAGS,
            }
            if interface_id:
                iface_kwargs["custom_fields"] = {"platformone_interface_id": _cf_text(str(interface_id))}
            entities.append(Entity(interface=Interface(**iface_kwargs)))
            emitted_names.add(name)
        entities.extend(_ip_entities_for_interface(device=device, interface_name=name, rows=rows))
    return entities


# Row fields that carry a front-panel port name across the ConfigState port
# tables (port/LAG `name`, vlan/IP/member `interface_name`, capabilities
# `port_name`). Normalized together so name-based joins stay consistent.
_PORT_NAME_FIELDS = ("name", "interface_name", "port_name")


def _native_port_name_row(row: dict, function: str | None) -> dict:
    new = dict(row)
    for field in _PORT_NAME_FIELDS:
        value = new.get(field)
        if isinstance(value, str):
            new[field] = native_port_name(value, function)
    if isinstance(new.get("member_ports"), list):
        new["member_ports"] = [
            _native_port_name_row(member, function) if isinstance(member, dict) else member
            for member in new["member_ports"]
        ]
    return new


def _native_port_name_tables(tables: dict[str, list[dict]], function: str | None) -> dict[str, list[dict]]:
    """Copy `tables` with slot:port names rewritten to the OS-native notation.

    Rows are copied (not mutated) so callers' table dicts stay untouched.
    """
    return {key: [_native_port_name_row(row, function) for row in rows or []] for key, rows in tables.items()}


def ports_to_entities(
    tables: dict[str, list[dict]],
    *,
    device: str,
    function: str | None = None,
) -> list[Entity]:
    """Map one switch's ConfigState port + LAG + VLAN tables to Diode entities.

    `tables` holds the device's "port_configs", "port_states",
    "vlan_properties", "lag_configs", "lag_states", optional
    "port_capabilities", "poe_configs", "poe_states", and "interface_ips"
    rows. Physical ports are the union of config+state rows joined on
    asset_interface_id. LAG interfaces come from lag
    config/state (type `lag`); member ports get Diode `Interface.lag`
    pointing at the parent LAG. Interface IP rows become Diode IPAddress
    entities assigned to the matching interface. VLAN membership refs use
    bare `vid` only (no names — switch-local names are not site-scoped).

    `function` (the Assets OS family) rewrites ConfigState's slot:port
    notation to the OS-native form (1:52 -> 1/52 on Fabric Engine / VOSS)
    before any joining, so every emitted name and cross-reference agrees.
    """
    if function and function.upper() in SLASH_PORT_FUNCTIONS:
        tables = _native_port_name_tables(tables, function)
    configs = _by_key(tables.get("port_configs") or [])
    states = _by_key(tables.get("port_states") or [])
    vlans = _by_key(tables.get("vlan_properties") or [])
    capabilities = _capabilities_by_port(tables.get("port_capabilities") or [])
    poe_configs = _by_key(tables.get("poe_configs") or [])
    poe_states = _by_key(tables.get("poe_states") or [])
    interface_ips = _by_key(tables.get("interface_ips") or [])
    lag_configs = tables.get("lag_configs") or []
    lag_states = tables.get("lag_states") or []

    lag_entities, lag_names, lag_interface_ids, membership, emitted_keys = _lag_entities(
        device=device,
        lag_configs=lag_configs,
        lag_states=lag_states,
        vlans=vlans,
        poe_configs=poe_configs,
        poe_states=poe_states,
        interface_ips=interface_ips,
        port_configs=configs,
        port_states=states,
    )
    entities = list(lag_entities)

    port_entities, emitted_port_names, port_keys = _physical_port_entities(
        device=device,
        configs=configs,
        states=states,
        vlans=vlans,
        capabilities=capabilities,
        poe_configs=poe_configs,
        poe_states=poe_states,
        interface_ips=interface_ips,
        lag_names=lag_names,
        lag_interface_ids=lag_interface_ids,
        membership=membership,
    )
    entities.extend(port_entities)
    emitted_keys.update(port_keys)

    entities.extend(
        _orphan_member_entities(device=device, membership=membership, emitted_port_names=emitted_port_names)
    )
    entities.extend(
        _orphan_ip_entities(device=device, interface_ips=interface_ips, emitted_keys=emitted_keys)
    )
    return entities


# ---------------------------------------------------------------------------
# Wireless AP radios + WLANs (native NetBox Interface RF fields + WirelessLAN)
# ---------------------------------------------------------------------------

# ConfigState radio_mode strings observed / documented in ExtremeCloud IQ
# style; unknown modes leave Interface.type unset.
_RADIO_TYPE_BY_MODE = {
    "_11a": "ieee802.11a",
    "_11bg": "ieee802.11g",
    "_11an": "ieee802.11n",
    "_11ng": "ieee802.11n",
    "_11ac": "ieee802.11ac",
    "_11ax_2g": "ieee802.11ax",
    "_11ax_5g": "ieee802.11ax",
    "_11ax_6g": "ieee802.11ax",
    "11a": "ieee802.11a",
    "11bg": "ieee802.11g",
    "11an": "ieee802.11n",
    "11ng": "ieee802.11n",
    "11ac": "ieee802.11ac",
    "11ax": "ieee802.11ax",
    "11ax_2g": "ieee802.11ax",
    "11ax_5g": "ieee802.11ax",
    "11ax_6g": "ieee802.11ax",
    "ieee802.11a": "ieee802.11a",
    "ieee802.11b": "ieee802.11b",
    "ieee802.11g": "ieee802.11g",
    "ieee802.11n": "ieee802.11n",
    "ieee802.11ac": "ieee802.11ac",
    "ieee802.11ax": "ieee802.11ax",
}

# channel_width is an integer in ConfigState; only values that are already
# standard IEEE channel widths in MHz are asserted.
_VERIFIED_CHANNEL_WIDTH_MHZ = frozenset({20, 40, 80, 160, 320})


def _channel_frequency_mhz(band: str | None, channel: int | None) -> float | None:
    """Channel-center frequency in MHz from band label + channel number.

    Uses standard IEEE 802.11 channel-numbering formulas (not Extreme-specific):
    2.4 GHz = 2407 + 5*channel; 5 GHz = 5000 + 5*channel; 6 GHz = 5950 + 5*channel.
    """
    if channel is None:
        return None
    try:
        channel_number = int(channel)
    except (TypeError, ValueError):
        return None
    if not band:
        return None
    normalized = str(band).casefold().replace(" ", "")
    if "6g" in normalized or normalized in {"6", "band_6", "band6"}:
        offset = 5950.0
    elif "2.4" in normalized or "2,4" in normalized or normalized in {"24g", "2g", "band_2_4"}:
        offset = 2407.0
    elif "5g" in normalized or normalized in {"5", "band_5", "band5"}:
        offset = 5000.0
    else:
        return None
    return offset + 5.0 * channel_number


def _radio_type(radio_mode: str | None) -> str | None:
    if not radio_mode:
        return None
    key = str(radio_mode).strip()
    mapped = _RADIO_TYPE_BY_MODE.get(key) or _RADIO_TYPE_BY_MODE.get(key.casefold())
    if mapped:
        return mapped
    compact = key.casefold().replace(" ", "").replace("-", "").replace(".", "")
    # Wi-Fi 7 / 11be has no confirmed NetBox Interface type here — leave unset.
    if "11be" in compact:
        return None
    for needle, iface_type in (
        ("11ax", "ieee802.11ax"),
        ("11ac", "ieee802.11ac"),
        ("11n", "ieee802.11n"),
        ("11g", "ieee802.11g"),
        ("11b", "ieee802.11b"),
        ("11a", "ieee802.11a"),
    ):
        if needle in compact:
            return iface_type
    return None


def _channel_width_mhz(value) -> float | None:
    try:
        width = int(value)
    except (TypeError, ValueError):
        return None
    if width in _VERIFIED_CHANNEL_WIDTH_MHZ:
        return float(width)
    return None


def _tx_power(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _auth_type_from_encryption(encryption: str | None) -> str | None:
    """Map AssetSsidState.encryption to NetBox WirelessLAN auth_type.

    Unknown / empty values leave auth_type unset (no invented "open").
    """
    if not encryption or not str(encryption).strip():
        return None
    compact = str(encryption).casefold().replace(" ", "").replace("-", "").replace("_", "")
    if compact in {"open", "enhancedopen", "none", "owe"} or compact.startswith("open"):
        return "open"
    if "wep" in compact:
        return "wep"
    if any(token in compact for token in ("8021x", "enterprise", "radius", "eap", "dot1x")):
        return "wpa-enterprise"
    if any(token in compact for token in ("psk", "ppsk", "sae", "personal", "wpa2", "wpa3")):
        return "wpa-personal"
    return None


def _split_if_names(value) -> list[str]:
    """Normalize AssetSsid*.if_names into a list of interface name strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        # JSON-ish list serialized as a string.
        inner = text[1:-1].strip()
        if not inner:
            return []
        parts = [part.strip().strip("'\"") for part in inner.split(",")]
        return [part for part in parts if part]
    for sep in (",", ";", "|"):
        if sep in text:
            return [part.strip() for part in text.split(sep) if part.strip()]
    return [text]


def _wireless_radio_key(row: dict) -> str | None:
    interface_id = str(row.get("asset_interface_id") or "").strip()
    if interface_id:
        return f"id:{interface_id}"
    device_id = str(row.get("asset_device_id") or "").strip()
    name = str(row.get("name") or "").strip()
    if device_id and name:
        return f"name:{device_id}:{name}"
    return None


def _wlan_status(enabled) -> str | None:
    if enabled is True:
        return "active"
    if enabled is False:
        return "disabled"
    return None


def _wlan_kwargs(ssid: str, *, enabled, encryption: str | None) -> dict:
    kwargs: dict = {
        "ssid": ssid,
        "tags": PROVENANCE_TAGS,
    }
    status = _wlan_status(enabled)
    if status is not None:
        kwargs["status"] = status
    elif enabled is None:
        # Seen on a live radio / SSID table without an enabled flag — treat as
        # currently present, matching the Meraki/XIQ "broadcasting → active"
        # convention when only presence is known.
        kwargs["status"] = "active"
    auth_type = _auth_type_from_encryption(encryption)
    if auth_type is not None:
        kwargs["auth_type"] = auth_type
    return kwargs


def _radio_interface_kwargs(
    *,
    device: str,
    name: str,
    config: dict,
    state: dict,
    ssids: list[str],
) -> dict:
    interface_id = str(config.get("asset_interface_id") or state.get("asset_interface_id") or "")
    kwargs: dict = {
        "device": device,
        "name": name,
        "rf_role": "ap",
        "tags": PROVENANCE_TAGS,
        "custom_fields": {"platformone_interface_id": _cf_text(interface_id)} if interface_id else {},
    }
    if "enabled" in config and isinstance(config.get("enabled"), bool):
        kwargs["enabled"] = config["enabled"]
    radio_type = _radio_type(state.get("radio_mode") or config.get("radio_mode"))
    if radio_type is not None:
        kwargs["type"] = radio_type
    tx_power = _tx_power(state.get("power"))
    if tx_power is not None:
        kwargs["tx_power"] = tx_power
    bssid = state.get("bssid")
    if bssid:
        kwargs["primary_mac_address"] = str(bssid)
    frequency = _channel_frequency_mhz(state.get("band"), state.get("channel"))
    if frequency is not None:
        kwargs["rf_channel_frequency"] = frequency
    width = _channel_width_mhz(state.get("channel_width"))
    if width is not None:
        kwargs["rf_channel_width"] = width
    if ssids:
        kwargs["wireless_lans"] = ssids
    return kwargs


def radios_to_entities(
    tables_by_device: dict[str, dict[str, list[dict]]],
    *,
    device_names: dict[str, str],
) -> list[Entity]:
    """Map ConfigState wireless + SSID tables to Interface and WirelessLAN entities.

    `tables_by_device` maps ConfigState AssetDevice UUID -> wireless table
    buckets (`wireless_interfaces`, `wireless_states`, `ssid_configs`,
    `ssid_states`). `device_names` maps the same UUID to the NetBox device
    name already used for Device entities.

    Each radio becomes an Interface with native RF fields (`rf_role`,
    `tx_power`, `rf_channel_frequency`, `rf_channel_width`, `type`,
    `primary_mac_address`, `wireless_lans`). Each distinct SSID becomes a
    WirelessLAN (`ssid`, `status`, `auth_type` when encryption maps cleanly).
    WLANs are not site-scoped: the same SSID can broadcast from APs in many
    sites. SSIDs link to radios via `AssetSsid*.if_names` and any
    `ssid_name` on wireless interface state rows.
    """
    wlans: dict[str, dict] = {}
    ssids_by_radio: dict[tuple[str, str], list[str]] = defaultdict(list)
    radio_rows: dict[tuple[str, str], dict] = {}

    for device_id, tables in tables_by_device.items():
        if device_id not in device_names:
            continue
        configs = tables.get("wireless_interfaces") or []
        states = tables.get("wireless_states") or []
        ssid_configs = tables.get("ssid_configs") or []
        ssid_states = tables.get("ssid_states") or []

        radios: dict[str, dict] = {}
        for row in configs:
            key = _wireless_radio_key(row)
            if not key:
                continue
            radios.setdefault(key, {"config": {}, "states": []})["config"] = row
        for row in states:
            key = _wireless_radio_key(row)
            if not key:
                continue
            radios.setdefault(key, {"config": {}, "states": []})["states"].append(row)

        name_to_key: dict[str, str] = {}
        for key, radio in radios.items():
            config = radio["config"]
            state = (radio["states"] or [{}])[0]
            name = str(config.get("name") or state.get("name") or "").strip()
            if not name:
                continue
            name_to_key[name] = key
            radio_rows[(device_id, key)] = {
                "device": device_names[device_id],
                "name": name,
                "config": config,
                "states": radio["states"],
            }
            for state_row in radio["states"]:
                ssid = str(state_row.get("ssid_name") or "").strip()
                if ssid and ssid not in ssids_by_radio[(device_id, key)]:
                    ssids_by_radio[(device_id, key)].append(ssid)
                    wlans.setdefault(ssid, {"enabled": None, "encryption": None})

        encryption_by_ssid = {
            str(row.get("name") or "").strip(): row.get("encryption")
            for row in ssid_states
            if str(row.get("name") or "").strip()
        }
        for row in ssid_configs:
            ssid = str(row.get("name") or "").strip()
            if not ssid:
                continue
            entry = wlans.setdefault(ssid, {"enabled": None, "encryption": None})
            if isinstance(row.get("enabled"), bool):
                entry["enabled"] = row["enabled"]
            if entry.get("encryption") is None and encryption_by_ssid.get(ssid) is not None:
                entry["encryption"] = encryption_by_ssid[ssid]
            for if_name in _split_if_names(row.get("if_names")):
                radio_key = name_to_key.get(if_name)
                if radio_key and ssid not in ssids_by_radio[(device_id, radio_key)]:
                    ssids_by_radio[(device_id, radio_key)].append(ssid)
        for row in ssid_states:
            ssid = str(row.get("name") or "").strip()
            if not ssid:
                continue
            entry = wlans.setdefault(ssid, {"enabled": None, "encryption": None})
            if entry.get("encryption") is None and row.get("encryption") is not None:
                entry["encryption"] = row.get("encryption")
            for if_name in _split_if_names(row.get("if_names")):
                radio_key = name_to_key.get(if_name)
                if radio_key and ssid not in ssids_by_radio[(device_id, radio_key)]:
                    ssids_by_radio[(device_id, radio_key)].append(ssid)

    entities = [
        Entity(
            wireless_lan=WirelessLAN(
                **_wlan_kwargs(ssid, enabled=meta.get("enabled"), encryption=meta.get("encryption"))
            )
        )
        for ssid, meta in sorted(wlans.items())
    ]
    for (device_id, key), radio in sorted(
        radio_rows.items(), key=lambda item: (item[1]["device"], item[1]["name"])
    ):
        state = next((row for row in radio["states"] if row), {})
        entities.append(
            Entity(
                interface=Interface(
                    **_radio_interface_kwargs(
                        device=radio["device"],
                        name=radio["name"],
                        config=radio["config"],
                        state=state,
                        ssids=ssids_by_radio.get((device_id, key), []),
                    )
                )
            )
        )
    return entities
