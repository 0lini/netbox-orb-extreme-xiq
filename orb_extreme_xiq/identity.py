"""Stable device naming + site/location resolution for XIQ devices.

XIQ's location hierarchy is Site -> Building -> Floor (and potentially
deeper): the root of each tree returned by `/locations/tree` is the site;
everything below it is a chain of nested NetBox Location entities.
`build_location_index` flattens the tree into a lookup from location_id to
(site_name, location_path) -- location_path is the ordered list of location
names from the top-level Location (e.g. the building) down to the specific
node a device is assigned to (e.g. the floor), empty if a device is assigned
directly to the site itself.
"""

from __future__ import annotations

# XiqDeviceFunction values (xcloudiq-openapi.yaml) that are switches -- used
# both to build ROLE_BY_DEVICE_FUNCTION below and by is_switch(), so a device
# is identified as a switch by its raw device_function, never by comparing
# against the display string role_for() happens to map it to (backend.py used
# to do exactly that indirect comparison to decide whether to sync wired
# ports; renaming the display string in one place without the other would
# have silently stopped wired-port-sync for every switch, with nothing to
# error).
SWITCH_DEVICE_FUNCTIONS = frozenset({"SWITCH", "SWITCH_HAC", "SWITCH_DELL"})

# Same reasoning as SWITCH_DEVICE_FUNCTIONS above, for wireless-radio sync.
AP_DEVICE_FUNCTIONS = frozenset({"AP"})

# XiqDeviceFunction enum values (xcloudiq-openapi.yaml) -> NetBox device role slug.
ROLE_BY_DEVICE_FUNCTION = {
    **dict.fromkeys(SWITCH_DEVICE_FUNCTIONS, "Switch"),
    **dict.fromkeys(AP_DEVICE_FUNCTIONS, "Wireless AP"),
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


def _device_function_in(device_function: str | None, functions: frozenset[str]) -> bool:
    return bool(device_function) and device_function.upper() in functions


def is_switch(device_function: str | None) -> bool:
    """Whether a XIQ device_function is a switch (see SWITCH_DEVICE_FUNCTIONS)."""
    return _device_function_in(device_function, SWITCH_DEVICE_FUNCTIONS)


def is_ap(device_function: str | None) -> bool:
    """Whether a XIQ device_function is an access point (see AP_DEVICE_FUNCTIONS)."""
    return _device_function_in(device_function, AP_DEVICE_FUNCTIONS)


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
    """Flatten a `/locations/tree` response into {location_id: {site_name, location_path}}.

    The root of each tree is the site; every descendant's location_path is
    the chain of names from the top-level child of the root down to itself
    (the root's own location_path is empty -- a device assigned directly to
    the site has no Location to nest it under).
    """
    index: dict[int, dict] = {}

    def walk(node: dict, site_name: str, location_path: list[str]) -> None:
        index[node.get("id")] = {"site_name": site_name, "location_path": location_path}
        for child in node.get("children") or []:
            walk(child, site_name, [*location_path, child.get("name", "")])

    for root in tree or []:
        walk(root, root.get("name", ""), [])
    return index


def resolve_location(
    location_id: int | None, location_index: dict[int, dict], default_site: str
) -> tuple[str, list[str]]:
    """Resolve a device's XIQ location_id to (site_name, location_path).

    Falls back to (default_site, []) when the location is unknown
    (missing/stale location_id) -- no Location chain is asserted for a
    device with no real XIQ location to attribute it to.
    """
    entry = location_index.get(location_id) if location_id is not None else None
    if entry is None:
        return default_site, []
    return entry["site_name"], entry["location_path"]


def expand_location_paths(
    paths: set[tuple[str, tuple[str, ...]]],
) -> list[tuple[str, tuple[str, ...]]]:
    """Expand a set of (site_name, location_path) tuples into every distinct
    (site_name, ancestor_path) pair needed to represent the full nested
    hierarchy -- one entry per Building/Floor/etc. level actually in use,
    deduped, with every path's ancestors ordered before itself so a caller
    can thread `parent` references through the result in a single pass.
    """
    seen: set[tuple[str, tuple[str, ...]]] = set()
    ancestors: list[tuple[str, tuple[str, ...]]] = []
    for site_name, path in sorted(paths):
        for depth in range(1, len(path) + 1):
            key = (site_name, path[:depth])
            if key not in seen:
                seen.add(key)
                ancestors.append(key)
    return ancestors
