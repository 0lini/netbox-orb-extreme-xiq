"""Platform ONE -> Diode entities: device/site/location inventory + interfaces.

Asserts name, serial, status, site, location, device_type/manufacturer,
platform, and primary_ip4 whenever the Assets API reports the underlying
field, and interface config/state whenever ConfigState reports it -- all
unconditionally (no configurable field-authority system, no opt-in flags),
matching this worker's "just always sync what's available" convention.

`custom_fields` and `tags` are always emitted alongside -- they're
provenance metadata (extreme-networks/platform-one/discovered tags,
platformone_device_id / platformone_interface_id), not fields a human would
meaningfully contest. Identity relies on the native `serial` field (see
`_device_kwargs`) rather than a separate immutable ID custom field --
neither the real Cisco Meraki integration nor NetBox Labs' generic
discovery backends carry one; they rely on native `serial` the same way.

Input shape: backend.py correlates each Assets device with its ConfigState
device UUID and AssetLocation record up front, and passes "device records"
-- {"asset": <Assets Device>, "cs_device_id": str|None, "location":
<AssetLocation>|None} -- so every mapper here works from one joined view.
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
    resolve_location,
)

__all__ = [
    "devices_to_entities",
    "ports_to_entities",
    "scope_devices",
]

MANUFACTURER = "Extreme Networks"

# Vendor/product/lifecycle tags, mirroring the flat-tag pattern NetBox Labs'
# own Cisco Meraki integration uses (e.g. "cisco", "meraki", "discovered")
# rather than one namespaced "source:platform-one" tag. Derived from
# bootstrap.TAGS so the two can't drift apart.
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
    if asset.get("os_version"):
        kwargs["platform"] = Platform(name=asset["os_version"], manufacturer=MANUFACTURER)
    primary_ip4 = _primary_ip4(asset)
    if primary_ip4:
        kwargs["primary_ip4"] = primary_ip4
    return kwargs


def scope_devices(records: list[dict], *, default_site: str, site_scope: set[str] | None) -> list[dict]:
    """Return the device records whose resolved site is in site_scope (all, if no scope).

    The single source of truth for scope filtering: devices_to_entities uses
    it, and any caller that fans out per-device API calls afterwards (wired
    ports) must filter through it too -- otherwise an out-of-scope device's
    Interface entities would reference a Device that was never emitted,
    recreating the device in NetBox through Diode's implicit reference
    handling.
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
    nested Location per Building/Floor level actually in use, plus one
    Device per device.
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

    # expand_location_paths orders every path's ancestors before itself, so a
    # single pass can thread `parent` through a cache instead of rebuilding
    # each prefix's chain from scratch (O(total locations), not O(depth^2)).
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


# ConfigState publishes oper_speed / oper_duplex / connector_type as bare
# integer enumerations with NO value table anywhere in its OpenAPI spec
# (verified: zero `enum` definitions across the whole document). The maps
# below therefore contain only codes verified against a production Fabric
# Engine device; unknown codes assert nothing rather than guessing.
# oper_state is the exception: its schema description is lifted verbatim
# from IF-MIB ifOperStatus, so standard IF-MIB numbering applies.
VERIFIED_OPER_SPEED_KBPS = {4: 1_000_000}  # 4 = 1 Gbit/s
VERIFIED_DUPLEX = {2: "full"}  # 1 = not-applicable (link down): assert nothing
OPER_STATE_UP = 1  # IF-MIB ifOperStatus: 1=up, 2=down, ...

# NetBox interface type from (verified oper_speed code, verified
# connector_type code). connector_type: 1 = copper, 2 = fiber (verified as
# above). Combinations not listed (including any unknown code) leave `type`
# unset rather than misrepresenting hardware.
_TYPE_BY_SPEED_AND_CONNECTOR = {
    (4, 1): "1000base-t",
    (4, 2): "1000base-x-sfp",
}


def _record_key(record: dict) -> str:
    """Join key across ConfigState port tables: the shared asset_interface_id,
    falling back to the front-panel port name for tables/rows without it."""
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

    `port_vlan` is the untagged VLAN; the nested `vlans` list
    (AssetInterfaceVlanMap) is every VLAN mapped onto the interface, so the
    tagged set is that list minus the untagged VLAN. `mode` follows NetBox
    convention: any tagged VLANs -> "tagged", only an untagged VLAN ->
    "access". Interfaces with no VLAN rows assert none of the three -- on
    Fabric Engine FLEX-UNI/Fabric-Attach deployments a port can be mapped
    straight into an I-SID instead of a VLAN, and inventing an access mode
    for it would misrepresent real configuration.
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

    # Admin state -- the field the legacy XIQ portlist endpoint never had.
    enabled = config.get("enabled")
    if isinstance(enabled, bool):
        kwargs["enabled"] = enabled

    oper_state = state.get("oper_state")
    if oper_state is not None:
        # Link state as mark_connected ("physically connected to something"),
        # never as `enabled` -- admin state is asserted separately above.
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

    `tables` holds this device's rows of "port_configs"
    (retrieve-asset-port-config), "port_states" (retrieve-asset-port-state),
    and "vlan_properties" (retrieve-asset-interface-vlan-properties). The
    port list is the union of config+state rows joined on
    asset_interface_id (name as fallback): config alone still yields a port
    (admin state, description, VLANs), state alone still yields link state.
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
