"""Stable device naming + site/location resolution for Platform ONE devices.

Site resolution prefers the ConfigState AssetLocation record (site +
building/floor chain), then falls back to the Assets record's flat
`site_name`. There is no worker-side default site: callers skip devices
that resolve to neither (Platform ONE assigns every device a site).
"""

from __future__ import annotations

import re

# Assets API `Device.function` values that are switch OSes, mapped to the
# canonical OS-family name used in the NetBox Platform.
PLATFORM_BY_FUNCTION = {
    "SWITCH ENGINE": "Switch Engine",
    "FABRIC ENGINE": "Fabric Engine",
    "EXOS": "EXOS",
    "VOSS": "VOSS",
}

# Gates the per-switch ConfigState port sync in backend.py.
SWITCH_DEVICE_FUNCTIONS = frozenset(PLATFORM_BY_FUNCTION)


def is_switch(function: str | None) -> bool:
    """Whether an Assets `function` value is a switch OS (see SWITCH_DEVICE_FUNCTIONS)."""
    return bool(function) and function.upper() in SWITCH_DEVICE_FUNCTIONS


def platform_for(function: str | None) -> str | None:
    """Canonical OS-family name for an Assets `function` value.

    Returns None for non-OS functions (AP, Router, Appliance, Unknown, ...)
    rather than inventing an OS family for them.
    """
    if not function:
        return None
    return PLATFORM_BY_FUNCTION.get(function.upper())


def platform_name(function: str | None, os_version: str | None) -> str | None:
    """NetBox Platform name: OS family and version in one flat value.

    NetBox platforms cannot nest, so family and version combine into a
    single name (e.g. "Fabric Engine 9.2.1.0"). Either part may be absent;
    returns None when neither is known.
    """
    parts = [part for part in (platform_for(function), os_version) if part]
    return " ".join(parts) or None


def slugify(value: str) -> str:
    """NetBox-style slug: lowercase, non-alnum runs collapsed to hyphens.

    Returns an empty string when the value has no alphanumeric characters —
    callers must not invent a fallback slug.
    """
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def role_for(function: str | None) -> tuple[str, str] | None:
    """Map Assets `function` to a NetBox DeviceRole (name, slug).

    Convention: keep the Platform ONE function string as the role `name`
    (e.g. "Fabric Engine", "AP") and derive `slug` via `slugify` (e.g.
    `fabric-engine`, `ap`). Returns None when function is empty, the Assets
    sentinel ``Unknown``, or when no valid slug can be derived — never
    invents a static default role (``switch``, ``network``, ``unknown``, …).
    """
    if not function or not str(function).strip():
        return None
    name = str(function).strip()
    if name.casefold() == "unknown":
        return None
    slug = slugify(name)
    if not slug:
        return None
    return name, slug


_FABRIC_ENGINE_PREFIX = "FabricEngine_"


def device_type_model_for(product_type: str | None) -> str | None:
    """Map an Assets product_type to its NetBox Device Type Library model name.

    Assets prefixes product_type with "FabricEngine_" for Fabric Engine
    switches (e.g. "FabricEngine_5320_48P_8XE"); the Device Type Library
    puts the marker at the end with hyphens ("5320-48P-8XE-FabricEngine").
    Values without the prefix are passed through unchanged rather than
    guessed at.
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


def resolve_location(asset_location: dict | None, assets_device: dict) -> tuple[str | None, list[str]]:
    """Resolve a device to (site_name, location_path).

    location_path is the ordered Building -> Floor chain from the ConfigState
    AssetLocation record; either level may be absent. Without a ConfigState
    location, the Assets record's flat `site_name` is used with no Location
    chain. Platform ONE assigns every device a site itself ("Default Site"),
    so `None` -- neither source naming one -- is unexpected; callers skip
    such devices rather than inventing a site.
    """
    if asset_location:
        site = asset_location.get("site_name") or assets_device.get("site_name") or None
        path = [
            name for name in (asset_location.get("building_name"), asset_location.get("floor_name")) if name
        ]
        return site, path
    return assets_device.get("site_name") or None, []


def expand_location_paths(
    paths: set[tuple[str, tuple[str, ...]]],
) -> list[tuple[str, tuple[str, ...]]]:
    """Expand (site_name, location_path) tuples into every distinct
    (site_name, ancestor_path) pair in the hierarchy, deduped, with each
    path's ancestors ordered before itself so a caller can thread `parent`
    references through the result in a single pass.
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
