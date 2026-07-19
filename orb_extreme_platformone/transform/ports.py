"""Switch port, LAG, VLAN, PoE, and interface-IP mapping."""

from __future__ import annotations

import ipaddress
from collections import defaultdict

from netboxlabs.diode.sdk.ingester import VLAN, Entity, Interface, IPAddress

from orb_extreme_platformone.identity import SLASH_PORT_FUNCTIONS, native_port_name

from .common import PROVENANCE_TAGS, _cf_text, logger

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
        mask = _coerce_int(row.get("mask_length"))
        # Require an explicit prefix from ConfigState (mask_length or inline /n);
        # never accept ip_interface's implicit /32 or /128 on a bare host.
        if not raw or ("/" not in raw and not (mask is not None and 0 <= mask <= 128)):
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
    native = _coerce_int(config.get("native_vlan"))
    if native is None or native <= 0 or _is_extreme_reserved_vlan(native):
        return {}
    fields: dict = {"untagged_vlan": VLAN(vid=native)}
    port_mode = config.get("port_mode")
    # port_mode True means trunk on Fabric Engine, but without a tagged member
    # list we must not assert mode=tagged (empty tagged_vlans). Leave mode
    # unset in that case; False is a real access port.
    if port_mode is False:
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


def _coerce_int(value) -> int | None:
    """Accept JSON ints or digit-only strings; reject floats/bools/garbage."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
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
    mask = _coerce_int(row.get("mask_length"))
    if "/" not in raw:
        if mask is None or not 0 <= mask <= 128:
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
    config has no members for that LAG.
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
