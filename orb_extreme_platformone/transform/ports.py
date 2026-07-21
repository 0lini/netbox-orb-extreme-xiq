"""Switch port, LAG, VLAN, PoE, and interface-IP mapping."""

from __future__ import annotations

import ipaddress
from collections import defaultdict

from netboxlabs.diode.sdk.ingester import VLAN, Entity, Interface, IPAddress

from orb_extreme_platformone.extract.tables import INTERFACE_ID_TABLES, PORT_TABLES
from orb_extreme_platformone.identity import SLASH_PORT_FUNCTIONS, native_port_name

from .common import (
    PROVENANCE_TAGS,
    _coerce_bool,
    _coerce_int,
    _device_ref,
    _explicit_cidr,
    _interface_identity_kwargs,
    _normalized_mac,
    logger,
)

# Extreme Networks reserves VIDs 4060–4094 for internal use (e.g. Fabric
# Engine). These are filtered from Interface untagged/tagged memberships.
EXTREME_RESERVED_VLAN_VID_MIN = 4060
EXTREME_RESERVED_VLAN_VID_MAX = 4094


def _is_extreme_reserved_vlan(vid: int) -> bool:
    """True for Extreme reserved internal VLAN IDs (4060–4094 inclusive)."""
    return EXTREME_RESERVED_VLAN_VID_MIN <= vid <= EXTREME_RESERVED_VLAN_VID_MAX


def _vlan_ref(vid: int) -> VLAN:
    """Diode VLAN membership ref with NetBox-required name.

    NetBox rejects blank VLAN names on create. Switch-local names are not
    site-scoped, so use the VID string as a stable placeholder (same VID on
    every switch at a site shares one NetBox VLAN).
    """
    return VLAN(vid=vid, name=str(vid))


# Keys `ports_to_entities` reads from its `tables` dict — derived from extract
# catalogs so the sets cannot drift.
PORT_ENTITY_TABLE_KEYS = frozenset(PORT_TABLES) | frozenset(INTERFACE_ID_TABLES)

# NetBox requires Interface.type. When ConfigState has no verified
# speed/connector mapping (or the row is an SVI / stub LAG member), use the
# same ``other`` fallback as AP radios / Cisco Meraki.
DEFAULT_INTERFACE_TYPE = "other"
VIRTUAL_INTERFACE_TYPE = "virtual"


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
        # Require an explicit prefix from ConfigState (mask_length or inline /n);
        # never accept ip_interface's implicit /32 or /128 on a bare host.
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
        try:
            asset_address = ipaddress.ip_address(asset_host)
        except ValueError:
            asset_address = None
        if asset_address is not None:
            for _row, cidr, iface in rows_with_cidr:
                if iface.ip == asset_address:
                    ranked.append((iface.version, cidr))
            if ranked:
                return _pick_primary_cidr(ranked)

    return {}


# ConfigState reports oper_speed / oper_duplex / connector_type as integer
# codes with no value table in its OpenAPI spec. Only codes verified against
# production hardware (or fixtures derived from that gear) are mapped;
# unknown codes assert nothing. Admin `enabled` and link `mark_connected`
# (from IF-MIB-style oper_state) are both asserted so admin-down vs link-down
# stay distinguishable.
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


def _vlan_records_for(vlans_by_id: dict[str, list[dict]], *, interface_id: str | None) -> list[dict]:
    """VLAN rows for an interface, joined only on asset_interface_id."""
    if interface_id and interface_id in vlans_by_id:
        return vlans_by_id[interface_id]
    return []


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
    misrepresent configuration. VLAN refs use `vid` plus `name=str(vid)`
    (NetBox requires a name; switch-local names are not site-scoped).
    """
    untagged: int | None = None
    mapped: set[int] = set()
    for record in vlan_records:
        port_vlan = _coerce_int(record.get("port_vlan"))
        if untagged is None and port_vlan is not None and port_vlan > 0:
            untagged = port_vlan
        for vlan_map in record.get("vlans") or []:
            number = _coerce_int(vlan_map.get("vlan_number")) if isinstance(vlan_map, dict) else None
            if number is not None and number > 0:
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
        fields["untagged_vlan"] = _vlan_ref(untagged)
    if tagged:
        fields["tagged_vlans"] = [_vlan_ref(vid) for vid in tagged]
        fields["mode"] = "tagged"
    elif untagged is not None:
        fields["mode"] = "access"
    return fields


def _poe_mode(state: dict) -> str | None:
    """NetBox poe_mode=pse when AssetPoePowerPortsState.supported is true.

    ``AssetPoePowerPortsConfig.enable`` is not used: admin enable alone does
    not mean the port is a PSE. classification/standard → poe_type is
    intentionally not mapped (no verified OpenAPI value table).
    """
    if state.get("supported") is True:
        return "pse"
    return None


def _interface_ip_cidr(row: dict) -> str | None:
    """Build address/prefix for AssetInterfaceIpAddress → Diode IPAddress.

    `address` is a bare address and `mask_length` its prefix length. Without
    an explicit prefix (inline ``/n`` or usable ``mask_length``), return
    None — never invent /32 or /128.
    """
    return _explicit_cidr(row.get("address"), row.get("mask_length"))


def _iface_base_kwargs(
    *,
    device: str,
    name: str,
    interface_id: str | None,
    config: dict,
    poe_state: dict | None = None,
) -> dict:
    """Shared identity / admin / PoE fields for physical ports and LAG parents."""
    kwargs = _interface_identity_kwargs(
        device=device,
        name=name,
        interface_id=interface_id,
        enabled=config.get("enabled"),
    )
    poe = _poe_mode(poe_state or {})
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
    poe_state: dict | None = None,
) -> dict:
    kwargs = _iface_base_kwargs(
        device=device,
        name=name,
        interface_id=interface_id,
        config=config,
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
    # NetBox requires type; verified speed+connector wins, else ``other``.
    kwargs["type"] = (
        _TYPE_BY_SPEED_AND_CONNECTOR.get((state.get("oper_speed"), state.get("connector_type")))
        or DEFAULT_INTERFACE_TYPE
    )

    if config.get("description"):
        kwargs["description"] = config["description"]
    mac = _normalized_mac(state.get("mac_address"))
    if mac:
        kwargs["primary_mac_address"] = mac

    if capability is not None and isinstance(capability.get("management_port"), bool):
        kwargs["mgmt_only"] = capability["management_port"]

    kwargs.update(_vlan_fields(vlan_records))
    return kwargs


def _lag_name(config: dict, state: dict) -> str | None:
    """LAG Interface name from Platform ONE ``name`` (switches always set one).

    No ``lag-{n}`` invention from ``lag_number`` — NetBox requires a name, but
    inventing one would diverge from the switch's auto-generated LAG name.
    """
    name = config.get("name") or state.get("name")
    return str(name) if name else None


def _lag_admin_enabled(port_config: dict | None = None) -> bool:
    """Admin state for a LAG parent — always an explicit bool.

    Prefer ``AssetPortConfig.enabled`` when the LAG interface id also appears
    in port tables (same admin signal as physical ports). Bare
    ``AssetLagConfig.enabled`` is false for every in-service MLT in production
    dry-runs while member ports are admin-up; trusting that value disables all
    LAG parents in NetBox. Diode/protobuf also maps an omitted bool to false,
    so this helper never leaves ``enabled`` unset (default admin-up).
    """
    if port_config:
        port_enabled = _coerce_bool(port_config.get("enabled"))
        if port_enabled is not None:
            return port_enabled
    return True


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
    """Map lag-config member names to LAG names from paired config/state rows.

    Membership lists live on lag-config; the LAG ``name`` may appear only on
    the paired lag-state row (same ``asset_interface_id``).
    """
    states_by_key = _by_key(states)
    membership: dict[str, str] = {}
    for config in configs:
        state = _first_row(states_by_key, _record_key(config), table="lag_states")
        lag = _lag_name(config, state)
        if not lag:
            continue
        for member in _member_interface_names(config):
            membership.setdefault(member, lag)
    return membership


def _lag_kwargs(
    *,
    device: str,
    name: str,
    interface_id: str | None,
    config: dict,
    vlan_records: list[dict],
    poe_state: dict | None = None,
    port_config: dict | None = None,
    port_state: dict | None = None,
) -> dict:
    """Build Diode kwargs for a LAG parent interface.

    Native fields from AssetLagConfig/State: `type=lag`, name, and
    `platformone_interface_id` (`asset_interface_id`). Admin `enabled` prefers
    a duplicate AssetPortConfig row when present; otherwise defaults to True
    (Platform ONE's AssetLagConfig.enabled is observed always-false for
    in-service MLTs, and Diode maps an omitted bool to false). Shared joins
    on that interface id fill VLAN trunk/access, PoE, and (separately)
    IPAddress entities. When port config/state also lists the
    same id, pull fields lag tables lack (`description`, `mark_connected`,
    MAC) — never speed/duplex/connector `type`, which would overwrite
    `type=lag`. VLANs come only from vlan-properties.

    AssetLagConfig also carries LACP `mode` / `lacp_key` / `load_balance_algo`
    / `dynamic`, but Diode's Interface has no matching fields and the mode /
    algo integers have no published value table — leave them unmapped (see
    README). `lag_number` is unused for NetBox naming (switches always supply
    `name`); it is not a second custom field (redundant with
    `platformone_interface_id`).
    """
    kwargs = _iface_base_kwargs(
        device=device,
        name=name,
        interface_id=interface_id,
        config=config,
        poe_state=poe_state,
    )
    kwargs["type"] = "lag"
    kwargs["enabled"] = _lag_admin_enabled(port_config)

    kwargs.update(_vlan_fields(vlan_records))

    if port_config and port_config.get("description"):
        kwargs["description"] = port_config["description"]
    if port_state:
        oper_state = port_state.get("oper_state")
        if oper_state is not None:
            kwargs["mark_connected"] = oper_state == OPER_STATE_UP
        mac = _normalized_mac(port_state.get("mac_address"))
        if mac:
            kwargs["primary_mac_address"] = mac
    return kwargs


def _ip_entities_for_interface(
    *,
    device: str,
    interface_name: str,
    rows: list[dict],
    interface_type: str = DEFAULT_INTERFACE_TYPE,
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
                    status="active",
                    assigned_object_interface=Interface(
                        device=device,
                        name=interface_name,
                        type=interface_type,
                    ),
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

    # Only suppress duplicate physical-port rows for LAGs we actually emit.
    # Unnamed LAG rows are skipped below; their interface ids must still be
    # free to surface as ordinary ports when port tables also list them.
    lag_interface_ids: set[str] = set()
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
        # `key` is asset_interface_id (required on lag config/state).
        kwargs = _lag_kwargs(
            device=device,
            name=name,
            interface_id=key,
            config=config,
            vlan_records=_vlan_records_for(vlans, interface_id=key),
            poe_state=_optional_first_row(poe_states, key, table="poe_states"),
            port_config=_optional_first_row(port_configs, key, table="port_configs"),
            port_state=_optional_first_row(port_states, key, table="port_states"),
        )
        entities.append(Entity(interface=Interface(**kwargs)))
        emitted_keys[key] = name
        lag_interface_ids.add(key)
        entities.extend(
            _ip_entities_for_interface(
                device=device,
                interface_name=name,
                rows=interface_ips.get(key, []),
                interface_type="lag",
            )
        )

    return entities, lag_names, lag_interface_ids, membership, emitted_keys


def _physical_port_entities(
    *,
    device: str,
    configs: dict[str, list[dict]],
    states: dict[str, list[dict]],
    vlans: dict[str, list[dict]],
    capabilities: dict[tuple[str, str], dict],
    poe_states: dict[str, list[dict]],
    interface_ips: dict[str, list[dict]],
    lag_names: set[str],
    lag_interface_ids: set[str],
    membership: dict[str, str],
) -> tuple[list[Entity], dict[str, str]]:
    """Emit physical (non-LAG) port interfaces joined on asset_interface_id."""
    entities: list[Entity] = []
    emitted_keys: dict[str, str] = {}

    for key in sorted(set(configs) | set(states)):
        config = _first_row(configs, key, table="port_configs")
        state = _first_row(states, key, table="port_states")
        name = str(config.get("name") or state.get("name") or "")
        if not name:
            continue
        # `key` is asset_interface_id (required on port config/state).
        if key in lag_interface_ids:
            continue
        if name in lag_names:
            continue
        port_device_id = str(config.get("asset_device_id") or state.get("asset_device_id") or "")
        kwargs = _port_kwargs(
            device=device,
            name=name,
            interface_id=key,
            config=config,
            state=state,
            vlan_records=_vlan_records_for(vlans, interface_id=key),
            capability=capabilities.get((port_device_id, name)),
            poe_state=_optional_first_row(poe_states, key, table="poe_states"),
        )
        lag_parent = membership.get(name)
        if lag_parent:
            kwargs["lag"] = Interface(device=device, name=lag_parent, type="lag")
        entities.append(Entity(interface=Interface(**kwargs)))
        emitted_keys[key] = name
        entities.extend(
            _ip_entities_for_interface(
                device=device,
                interface_name=name,
                rows=interface_ips.get(key, []),
                interface_type=str(kwargs.get("type") or DEFAULT_INTERFACE_TYPE),
            )
        )

    return entities, emitted_keys


def _orphan_ip_entities(
    *,
    device: str,
    interface_ips: dict[str, list[dict]],
    emitted_keys: dict[str, str],
    interface_names: dict[str, str],
) -> list[Entity]:
    """IPs on interfaces that got no Interface entity above (e.g. VLAN/SVI
    interfaces, which appear in vlan_properties but not the port tables).

    Emits a minimal Interface first so the IPAddress has a real assigned
    object, then the IP entities. ``type=virtual`` for these non-port rows
    (SVIs); NetBox requires a non-blank type.

    Interface names come from already-fetched port/LAG/VLAN rows keyed by
    ``asset_interface_id``. ``AssetInterfaceIpAddress`` has no interface_name
    field in OpenAPI — do not invent one from the IP row.
    """
    entities: list[Entity] = []
    emitted_names: set[str] = set()
    for key, rows in sorted(interface_ips.items()):
        if key in emitted_keys:
            continue
        name = interface_names.get(key)
        if not name:
            continue
        if name not in emitted_names:
            interface_id = next(
                (str(row["asset_interface_id"]) for row in rows if row.get("asset_interface_id")),
                key or None,
            )
            entities.append(
                Entity(
                    interface=Interface(
                        **{
                            **_interface_identity_kwargs(
                                device=device,
                                name=name,
                                interface_id=str(interface_id) if interface_id else None,
                            ),
                            "type": VIRTUAL_INTERFACE_TYPE,
                        }
                    )
                )
            )
            emitted_names.add(name)
        entities.extend(
            _ip_entities_for_interface(
                device=device,
                interface_name=name,
                rows=rows,
                interface_type=VIRTUAL_INTERFACE_TYPE,
            )
        )
    return entities


def _interface_names_by_id(tables: dict[str, list[dict]]) -> dict[str, str]:
    """Map asset_interface_id → interface name from port/LAG/VLAN rows.

    Prefer port/LAG ``name``, then vlan-properties ``interface_name``. First
    non-empty name wins so later tables do not rename an already-known id.
    """
    names: dict[str, str] = {}
    for key in ("port_configs", "port_states", "lag_configs", "lag_states"):
        for row in tables.get(key) or []:
            interface_id = str(row.get("asset_interface_id") or "")
            name = str(row.get("name") or "").strip()
            if interface_id and name:
                names.setdefault(interface_id, name)
    for row in tables.get("vlan_properties") or []:
        interface_id = str(row.get("asset_interface_id") or "")
        name = str(row.get("interface_name") or "").strip()
        if interface_id and name:
            names.setdefault(interface_id, name)
    return names


# Row fields that carry a front-panel port name across the ConfigState port
# tables (port/LAG `name`, vlan/member `interface_name`, capabilities
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
    site_name: str | None = None,
    product_type: str | None = None,
) -> list[Entity]:
    """Map one switch's ConfigState port + LAG + VLAN tables to Diode entities.

    `tables` holds the device's "port_configs", "port_states",
    "vlan_properties", "lag_configs", "lag_states", optional
    "port_capabilities", "poe_states", and "interface_ips"
    rows. Physical ports are the union of config+state rows joined on
    asset_interface_id. LAG interfaces come from lag
    config/state (type `lag`); member ports get Diode `Interface.lag`
    pointing at the parent LAG (membership from lag-config only; members
    without a port row are not stubbed). Interface IP rows become Diode
    IPAddress entities assigned to the matching interface. VLAN membership
    refs use `vid` plus `name=str(vid)` (NetBox requires a name;
    switch-local names are not site-scoped). Physical ports without a
    verified connector map default to type `other`; SVI stubs use `virtual`.

    Nested Interface ``device`` refs include site/role/device_type when
    known — Diode rejects name-only Device stubs during generate-diff.

    `function` (the Assets OS family) rewrites ConfigState's slot:port
    notation to the OS-native form (1:52 -> 1/52 on Fabric Engine / VOSS)
    before any joining, so every emitted name and cross-reference agrees.
    """
    if function and function.upper() in SLASH_PORT_FUNCTIONS:
        tables = _native_port_name_tables(tables, function)
    device_ref = _device_ref(
        name=device,
        site_name=site_name,
        function=function,
        product_type=product_type,
    )
    configs = _by_key(tables.get("port_configs") or [])
    states = _by_key(tables.get("port_states") or [])
    vlan_rows = tables.get("vlan_properties") or []
    vlans = _by_key(vlan_rows)
    capabilities = _capabilities_by_port(tables.get("port_capabilities") or [])
    poe_states = _by_key(tables.get("poe_states") or [])
    interface_ips = _by_key(tables.get("interface_ips") or [])
    lag_configs = tables.get("lag_configs") or []
    lag_states = tables.get("lag_states") or []

    lag_entities, lag_names, lag_interface_ids, membership, emitted_keys = _lag_entities(
        device=device_ref,
        lag_configs=lag_configs,
        lag_states=lag_states,
        vlans=vlans,
        poe_states=poe_states,
        interface_ips=interface_ips,
        port_configs=configs,
        port_states=states,
    )
    entities = list(lag_entities)

    port_entities, port_keys = _physical_port_entities(
        device=device_ref,
        configs=configs,
        states=states,
        vlans=vlans,
        capabilities=capabilities,
        poe_states=poe_states,
        interface_ips=interface_ips,
        lag_names=lag_names,
        lag_interface_ids=lag_interface_ids,
        membership=membership,
    )
    entities.extend(port_entities)
    emitted_keys.update(port_keys)

    entities.extend(
        _orphan_ip_entities(
            device=device_ref,
            interface_ips=interface_ips,
            emitted_keys=emitted_keys,
            interface_names=_interface_names_by_id(tables),
        )
    )
    return entities
