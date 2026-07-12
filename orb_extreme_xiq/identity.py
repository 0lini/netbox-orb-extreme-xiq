"""Stable device naming + site resolution for XIQ devices.

XIQ's location hierarchy is always Site -> Building -> Floor
(`XiqLocationType`). `build_location_index` flattens the tree returned by
`/locations/tree` so every location resolves to the *root* location it
descends from -- XIQ's own site name -- which `resolve_site_name` uses
directly as the NetBox site name. Multiple XIQ locations (buildings/floors)
under the same site therefore consolidate into one NetBox site automatically.
"""

from __future__ import annotations

# XiqDeviceFunction enum values (xcloudiq-openapi.yaml) -> NetBox device role slug.
ROLE_BY_DEVICE_FUNCTION = {
    "AP": "wireless-ap",
    "SWITCH": "Switch",
    "SWITCH_HAC": "Switch",
    "SWITCH_DELL": "Switch",
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
    """Flatten a `/locations/tree` response into {location_id: {name, root_name}}."""
    index: dict[int, dict] = {}

    def walk(node: dict, root_name: str) -> None:
        loc_id = node.get("id")
        index[loc_id] = {"name": node.get("name", ""), "root_name": root_name}
        for child in node.get("children") or []:
            walk(child, root_name)

    for root in tree or []:
        walk(root, root.get("name", ""))
    return index


def resolve_site_name(
    location_id: int | None,
    location_index: dict[int, dict],
    default_site: str,
) -> tuple[str, str | None]:
    """Resolve a device's XIQ location_id to a NetBox site name.

    XIQ's own location hierarchy is Site -> Building -> Floor, so the root
    location a device's location_id descends from *is* its XIQ site name --
    used directly as the NetBox site name, no separate mapping table needed.

    Returns (site_name, xiq_root_location_name). The root location name is
    None when the location is unknown (missing/stale location_id), in which
    case default_site is used and no XIQ-location attribution is recorded.
    """
    entry = location_index.get(location_id) if location_id is not None else None
    if entry is None:
        return default_site, None
    root_name = entry["root_name"]
    return root_name, root_name
