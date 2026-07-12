"""Stable device naming + direct site resolution for XIQ devices.

XIQ's location tree and NetBox's site structure are treated as 1:1: each XIQ
location a device belongs to becomes a NetBox site of the same name.
`build_location_index` just flattens `/locations/tree` into a flat
{location_id: name} lookup.
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


def build_location_index(tree: list[dict]) -> dict[int, str]:
    """Flatten a `/locations/tree` response into {location_id: name}."""
    index: dict[int, str] = {}

    def walk(node: dict) -> None:
        index[node.get("id")] = node.get("name", "")
        for child in node.get("children") or []:
            walk(child)

    for root in tree or []:
        walk(root)
    return index


def resolve_site_name(location_id: int | None, location_index: dict[int, str], default_site: str) -> str:
    """Resolve a device's XIQ location_id directly to a NetBox site name (1:1).

    Falls back to default_site when the location is unknown (missing/stale
    location_id).
    """
    if location_id is None:
        return default_site
    return location_index.get(location_id, default_site)
