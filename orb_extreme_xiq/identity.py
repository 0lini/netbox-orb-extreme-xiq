"""Stable device naming + XIQ location resolution for XIQ devices.

XIQ's location hierarchy (Location -> Building -> Floor, or similar) maps
onto NetBox's own two-level model: each XIQ *root* location (the XIQ
equivalent of a Meraki network) resolves to a NetBox Site via
`location_site_mapping`, and every XIQ location -- including the root --
becomes a NetBox Location nested under that Site, preserving the XIQ tree
structure exactly. Root Locations are what let multiple XIQ roots
consolidate into one Site (e.g. "HQ" and "Branch A" as sibling Locations
under one Site) without their same-named children colliding.
"""

from __future__ import annotations

# XiqDeviceFunction enum values (xcloudiq-openapi.yaml) -> NetBox device role slug.
ROLE_BY_DEVICE_FUNCTION = {
    "AP": "wireless-ap",
    "SWITCH": "network-switch",
    "SWITCH_HAC": "network-switch",
    "SWITCH_DELL": "network-switch",
    "ROUTER": "router",
    "ROUTER_AS_L2_VPN_GATEWAY": "router",
    "ROUTER_AS_L3_VPN_GATEWAY": "router",
    "L2_VPN_GATEWAY": "router",
    "L3_VPN_GATEWAY": "router",
}
DEFAULT_ROLE = "network-device"


def role_for(device_function: str | None) -> str:
    """Map a XIQ device_function to a NetBox device role slug."""
    if not device_function:
        return DEFAULT_ROLE
    return ROLE_BY_DEVICE_FUNCTION.get(device_function.upper(), DEFAULT_ROLE)


def device_name(device: dict, name_source: str = "hostname") -> str:
    """Deterministic device name; falls back to serial/service tag/MAC/id."""
    serial = device.get("serial_number") or device.get("service_tag")
    if name_source == "serial" and serial:
        return serial
    hostname = device.get("hostname")
    if hostname:
        return hostname
    if serial:
        return serial
    mac = device.get("mac_address")
    if mac:
        return mac
    return f"xiq-{device.get('id')}"


def build_location_index(tree: list[dict]) -> dict[int, dict]:
    """Flatten a `/locations/tree` response into {location_id: {name, root_name, parent_id}}.

    `parent_id` is None for root (top-level) locations.
    """
    index: dict[int, dict] = {}

    def walk(node: dict, root_name: str, parent_id: int | None) -> None:
        loc_id = node.get("id")
        index[loc_id] = {"name": node.get("name", ""), "root_name": root_name, "parent_id": parent_id}
        for child in node.get("children") or []:
            walk(child, root_name, loc_id)

    for root in tree or []:
        walk(root, root.get("name", ""), None)
    return index


def location_ancestor_chain(location_id: int | None, location_index: dict[int, dict]) -> list[int]:
    """Return `location_id`'s ancestor chain, root-first, `location_id` last.

    Empty if `location_id` is unknown (missing/stale, not in the index).
    """
    chain = []
    current = location_id
    while current is not None and current in location_index:
        chain.append(current)
        current = location_index[current]["parent_id"]
    chain.reverse()
    return chain


def resolve_site_name(
    location_id: int | None,
    location_index: dict[int, dict],
    location_site_mapping: dict[str, str],
    default_site: str,
) -> str:
    """Resolve a device's XIQ location_id to a NetBox site name via its root location.

    Falls back to default_site when the location is unknown (missing/stale
    location_id).
    """
    entry = location_index.get(location_id) if location_id is not None else None
    if entry is None:
        return default_site
    return location_site_mapping.get(entry["root_name"], default_site)
