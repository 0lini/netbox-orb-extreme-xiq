"""Map Extreme Platform ONE records to Diode entities.

Fields are asserted unconditionally whenever Platform ONE reports the
underlying data; fields with no Platform ONE equivalent are never asserted.
Device identity uses the native `serial` field plus deterministic names
(see `identity`), with `platformone_*` custom fields carried as provenance.

Callers pass "device records" pre-joined by backend.py:
{"asset": <Assets Device>, "cs_device_id": str | None,
 "location": <AssetLocation> | None}.
"""

from __future__ import annotations

from collections import defaultdict

from netboxlabs.diode.sdk.ingester import (
    VLAN,
    CustomFieldValue,
    Device,
    DeviceType,
    Entity,
    Interface,
    Location,
    Platform,
    Site,
)

from . import bootstrap
from .identity import (
    device_name,
    device_type_model_for,
    expand_location_paths,
    platform_name,
    resolve_location,
)

__all__ = [
    "devices_to_entities",
    "ports_to_entities",
    "scope_devices",
]

MANUFACTURER = "Extreme Networks"

PROVENANCE_TAGS = [tag["name"] for tag in bootstrap.TAGS]


def _status_for(asset: dict) -> str:
    return "active" if asset.get("is_connected") else "offline"


def _primary_ip4(asset: dict) -> str | None:
    ip = asset.get("ip_address")
    if not ip:
        return None
    return ip if "/" in ip else f"{ip}/32"


def _cf_text(value: str) -> CustomFieldValue:
    return CustomFieldValue(text=value)


def _device_kwargs(asset: dict, *, site_name: str, location: Location | None, name_source: str) -> dict:
    kwargs = {
        "name": device_name(asset, name_source),
        "serial": asset.get("serial_number") or None,
        "status": _status_for(asset),
        "site": Site(name=site_name),
        "custom_fields": {"platformone_device_id": _cf_text(str(asset["device_id"]))}
        if asset.get("device_id") is not None
        else {},
        "tags": PROVENANCE_TAGS,
    }
    if location is not None:
        kwargs["location"] = location
    if asset.get("product_type"):
        kwargs["device_type"] = DeviceType(
            model=device_type_model_for(asset["product_type"]), manufacturer=MANUFACTURER
        )
        kwargs["manufacturer"] = MANUFACTURER
    platform = platform_name(asset.get("function"), asset.get("os_version"))
    if platform:
        kwargs["platform"] = Platform(name=platform, manufacturer=MANUFACTURER)
    primary_ip4 = _primary_ip4(asset)
    if primary_ip4:
        kwargs["primary_ip4"] = primary_ip4
    return kwargs


def scope_devices(records: list[dict], *, default_site: str, site_scope: set[str] | None) -> list[dict]:
    """Return the device records whose resolved site is in site_scope (all, if no scope).

    Single source of truth for scope filtering: any caller that fans out
    per-device API calls afterwards (wired ports) must filter through it too,
    or Interface entities would reference devices that were never emitted and
    Diode would recreate them.
    """
    if not site_scope:
        return records
    return [
        record
        for record in records
        if resolve_location(record.get("location"), record["asset"], default_site)[0] in site_scope
    ]


def devices_to_entities(
    records: list[dict],
    *,
    default_site: str,
    name_source: str = "hostname",
    site_scope: set[str] | None = None,
) -> list[Entity]:
    """Map device records to Diode entities: one Site per distinct site, one
    nested Location per Building/Floor level in use, one Device per device.
    """
    entities: list[Entity] = []
    resolved: list[tuple[dict, str, list[str]]] = []
    site_names: set[str] = set()
    location_paths: set[tuple[str, tuple[str, ...]]] = set()

    scoped = scope_devices(records, default_site=default_site, site_scope=site_scope)
    for record in scoped:
        site_name, location_path = resolve_location(record.get("location"), record["asset"], default_site)
        resolved.append((record["asset"], site_name, location_path))
        site_names.add(site_name)
        if location_path:
            location_paths.add((site_name, tuple(location_path)))

    for site_name in sorted(site_names):
        entities.append(Entity(site=Site(name=site_name)))

    # expand_location_paths orders ancestors before descendants, so one pass
    # can thread `parent` through the cache.
    location_cache: dict[tuple[str, tuple[str, ...]], Location] = {}
    for site_name, path in expand_location_paths(location_paths):
        parent = location_cache.get((site_name, path[:-1])) if len(path) > 1 else None
        location = Location(name=path[-1], site=site_name, parent=parent)
        location_cache[(site_name, path)] = location
        entities.append(Entity(location=location))

    for asset, site_name, location_path in resolved:
        location = location_cache.get((site_name, tuple(location_path))) if location_path else None
        kwargs = _device_kwargs(asset, site_name=site_name, location=location, name_source=name_source)
        entities.append(Entity(device=Device(**kwargs)))

    return entities


# ConfigState reports oper_speed / oper_duplex / connector_type as integer
# codes with no value table in its OpenAPI spec. Only codes verified against
# production hardware are mapped; unknown codes assert nothing. oper_state is
# the exception: its schema description matches IF-MIB ifOperStatus.
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
    """Join key across ConfigState port tables: asset_interface_id, falling
    back to the front-panel port name for rows without it."""
    return str(record.get("asset_interface_id") or record.get("name") or record.get("interface_name") or "")


def _by_key(records: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        key = _record_key(record)
        if key:
            grouped[key].append(record)
    return grouped


def _vlan_fields(vlan_records: list[dict]) -> dict:
    """untagged_vlan / tagged_vlans / mode from AssetInterfaceVlanProperties rows.

    `port_vlan` is the untagged VLAN; the nested `vlans` list is every VLAN
    mapped onto the interface, so the tagged set is that list minus the
    untagged VLAN. Interfaces with no VLAN rows assert none of the three:
    on Fabric Engine a port can be mapped straight into an I-SID instead of
    a VLAN, and inventing an access mode would misrepresent configuration.
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
    tagged = sorted(mapped - {untagged} if untagged is not None else mapped)

    fields: dict = {}
    if untagged is not None:
        fields["untagged_vlan"] = VLAN(vid=untagged)
    if tagged:
        fields["tagged_vlans"] = [VLAN(vid=vid) for vid in tagged]
        fields["mode"] = "tagged"
    elif untagged is not None:
        fields["mode"] = "access"
    return fields


def _port_kwargs(
    *, device: str, name: str, interface_id: str | None, config: dict, state: dict, vlan_records: list[dict]
) -> dict:
    kwargs: dict = {
        "device": device,
        "name": name,
        "custom_fields": {"platformone_interface_id": _cf_text(interface_id)} if interface_id else {},
        "tags": PROVENANCE_TAGS,
    }

    enabled = config.get("enabled")
    if isinstance(enabled, bool):
        kwargs["enabled"] = enabled

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

    kwargs.update(_vlan_fields(vlan_records))
    return kwargs


def ports_to_entities(tables: dict[str, list[dict]], *, device: str) -> list[Entity]:
    """Map one switch's ConfigState port tables to Interface entities.

    `tables` holds the device's "port_configs", "port_states", and
    "vlan_properties" rows. The port list is the union of config+state rows
    joined on asset_interface_id (name as fallback): config alone still
    yields a port, state alone still yields link state.
    """
    configs = _by_key(tables.get("port_configs") or [])
    states = _by_key(tables.get("port_states") or [])
    vlans = _by_key(tables.get("vlan_properties") or [])

    entities: list[Entity] = []
    for key in sorted(set(configs) | set(states)):
        config = configs.get(key, [{}])[0]
        state = states.get(key, [{}])[0]
        name = config.get("name") or state.get("name")
        if not name:
            continue
        interface_id = config.get("asset_interface_id") or state.get("asset_interface_id")
        kwargs = _port_kwargs(
            device=device,
            name=str(name),
            interface_id=str(interface_id) if interface_id else None,
            config=config,
            state=state,
            vlan_records=vlans.get(key, []),
        )
        entities.append(Entity(interface=Interface(**kwargs)))
    return entities
