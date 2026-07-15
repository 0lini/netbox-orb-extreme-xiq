"""Stable device naming + site/location resolution for Platform ONE devices.

Two sources feed identity here:

  - The Assets API device record (`Device` schema, Asset Management spec):
    `host_name`, `serial_number`, `mac_address`, `device_id`, `function`,
    `site_name` -- always present for every onboarded device.
  - The ConfigState `AssetLocation` record: `site_name`, `building_name`,
    `floor_name` per device -- only present once ConfigState has collected
    the device, but the only source with the building/floor detail.

Site resolution prefers ConfigState's location (site + building/floor
chain), falls back to the Assets record's flat `site_name`, then to the
configured default site. There is no location *tree* API to index the way
XIQ's /locations/tree was: both sources carry the resolved names per
device, so location handling is per-device record lookup instead.
"""

from __future__ import annotations

# Assets API `Device.function` enum values that are switches -- gates the
# per-switch ConfigState port sync in backend.py. The Assets spec's own
# values (25.11.0): AP, Switch Engine, Fabric Engine, EXOS, VOSS, XIQ SE,
# Appliance, Tunnel Concentrator, Router, Unknown.
SWITCH_DEVICE_FUNCTIONS = frozenset({"SWITCH ENGINE", "FABRIC ENGINE", "EXOS", "VOSS"})


def is_switch(function: str | None) -> bool:
    """Whether an Assets `function` value is a switch OS (see SWITCH_DEVICE_FUNCTIONS)."""
    return bool(function) and function.upper() in SWITCH_DEVICE_FUNCTIONS


# Assets prefixes product_type with "FabricEngine_" for switches running
# Fabric Engine OS (e.g. "FabricEngine_5320_48P_8XE"), the same convention
# ExtremeCloud IQ used. The NetBox Device Type Library's
# (netbox-community/devicetype-library, device-types/Extreme Networks/)
# convention for these is "<model>-FabricEngine" (e.g. 5320-48P-8XE-FabricEngine),
# so the prefix moves to a suffix and underscores become hyphens.
_FABRIC_ENGINE_PREFIX = "FabricEngine_"


def device_type_model_for(product_type: str | None) -> str | None:
    """Map an Assets product_type to its NetBox Device Type Library model name.

    product_type values without the FabricEngine_ prefix (e.g. "VSP_SWITCH",
    a generic code that doesn't identify a specific physical model) are
    passed through unchanged -- guessing a suffix would misrepresent real
    hardware.
    """
    if not product_type:
        return None
    if product_type.startswith(_FABRIC_ENGINE_PREFIX):
        model = product_type[len(_FABRIC_ENGINE_PREFIX) :].replace("_", "-")
        return f"{model}-FabricEngine"
    return product_type


def device_name(device: dict, name_source: str = "hostname") -> str:
    """Deterministic device name; falls back to serial/MAC/Assets device_id."""
    serial = device.get("serial_number")
    if name_source == "serial" and serial:
        return serial
    hostname = device.get("host_name")
    if hostname:
        return hostname
    if serial:
        return serial
    mac = device.get("mac_address")
    if mac:
        return mac
    return f"platformone-{device.get('device_id')}"


def resolve_location(
    asset_location: dict | None, assets_device: dict, default_site: str
) -> tuple[str, list[str]]:
    """Resolve a device to (site_name, location_path).

    location_path is the ordered Building -> Floor chain from the
    ConfigState AssetLocation record (either level may be absent -- a device
    can sit directly in a site or a building). When ConfigState has no
    location for the device, the Assets record's flat `site_name` is used
    with no Location chain; when neither source names a site, the configured
    default site is used, and no Location chain is asserted for it.
    """
    if asset_location:
        site = asset_location.get("site_name") or assets_device.get("site_name") or default_site
        path = [
            name for name in (asset_location.get("building_name"), asset_location.get("floor_name")) if name
        ]
        return site, path
    return assets_device.get("site_name") or default_site, []


def expand_location_paths(
    paths: set[tuple[str, tuple[str, ...]]],
) -> list[tuple[str, tuple[str, ...]]]:
    """Expand a set of (site_name, location_path) tuples into every distinct
    (site_name, ancestor_path) pair needed to represent the full nested
    hierarchy -- one entry per Building/Floor level actually in use, deduped,
    with every path's ancestors ordered before itself so a caller can thread
    `parent` references through the result in a single pass.
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
