"""Device, site, and location mapping."""

from __future__ import annotations

import ipaddress

from netboxlabs.diode.sdk.ingester import (
    Device,
    DeviceRole,
    DeviceType,
    Entity,
    Location,
    Platform,
    Site,
    VirtualChassis,
)

from orb_extreme_platformone.identity import (
    asset_label,
    device_name,
    device_type_model_for,
    expand_location_paths,
    platform_name,
    resolve_location,
    role_for,
)

from .common import (
    CF_CLUSTER_ID,
    CF_DEVICE_ID,
    MANUFACTURER,
    PROVENANCE_TAGS,
    _cf_text,
    _explicit_cidr,
    logger,
)


def _status_for(asset: dict) -> str:
    """Map Assets `is_connected` to Device status (Meraki-style defaults).

    ``true`` → ``active``, ``false`` → ``offline``. Missing/unknown defaults
    to ``active`` — same posture as Cisco Meraki (any other / no status →
    active) and open Orb device-discovery (always active).
    """
    connected = asset.get("is_connected")
    if connected is False:
        return "offline"
    return "active"


def _primary_ips_from_asset(asset: dict) -> dict[str, str]:
    """Split Assets `ip_address` into primary_ip4 vs primary_ip6.

    Assets reports a bare host with no mask. Inventing /32 or /128 would
    create misleading host prefixes in NetBox, so a bare address is skipped.
    Only values that already include a prefix length are asserted (fallback
    when ConfigState did not yield a primary). Invalid values assert nothing.
    """
    cidr = _explicit_cidr(asset.get("ip_address"))
    if not cidr:
        return {}
    try:
        iface = ipaddress.ip_interface(cidr)
    except ValueError:
        return {}
    key = "primary_ip4" if iface.version == 4 else "primary_ip6"
    return {key: str(iface)}


def _coord(value) -> float | None:
    """Return a finite float coordinate, or None when unset/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _site_kwargs(site_name: str, coords: tuple[float | None, float | None] | None) -> dict:
    kwargs: dict = {"name": site_name}
    if coords:
        lat, lon = coords
        if lat is not None:
            kwargs["latitude"] = lat
        if lon is not None:
            kwargs["longitude"] = lon
    return kwargs


def _device_kwargs(
    asset: dict,
    *,
    site_name: str,
    location: Location | None,
    cs_device: dict | None = None,
    vc_membership: dict | None = None,
    primary_ips: dict[str, str] | None = None,
) -> dict:
    custom_fields: dict = {}
    if asset.get("device_id") is not None:
        custom_fields[CF_DEVICE_ID] = _cf_text(str(asset["device_id"]))
    kwargs = {
        "serial": asset.get("serial_number") or None,
        "site": Site(name=site_name),
        "custom_fields": custom_fields,
        "tags": PROVENANCE_TAGS,
    }
    name = device_name(asset)
    if name is not None:
        kwargs["name"] = name
    kwargs["status"] = _status_for(asset)
    if location is not None:
        kwargs["location"] = location

    role = role_for(asset.get("function"))
    if role:
        role_name, role_slug = role
        kwargs["role"] = DeviceRole(name=role_name, slug=role_slug)

    # Assets product_type / os_version preferred; ConfigState model_name /
    # firmware_version fill gaps when Assets omitted them.
    cs = cs_device or {}
    product_type = asset.get("product_type") or cs.get("model_name")
    if product_type:
        kwargs["device_type"] = DeviceType(
            model=device_type_model_for(product_type), manufacturer=MANUFACTURER
        )
        kwargs["manufacturer"] = MANUFACTURER
    os_version = asset.get("os_version") or cs.get("firmware_version")
    platform = platform_name(asset.get("function"), os_version)
    if platform:
        kwargs["platform"] = Platform(name=platform, manufacturer=MANUFACTURER)
    # ConfigState interface IPs (with mask_length) win; Assets CIDR is fallback.
    kwargs.update(primary_ips or _primary_ips_from_asset(asset))
    if vc_membership:
        # Include platformone_cluster_id so Diode matches the same VC as the
        # top-level entity (NetBox VirtualChassis.name is not unique).
        vc_kwargs: dict = {"name": vc_membership["name"]}
        cluster_id = vc_membership.get("cluster_id")
        if cluster_id:
            vc_kwargs["custom_fields"] = {CF_CLUSTER_ID: _cf_text(str(cluster_id))}
        kwargs["virtual_chassis"] = VirtualChassis(**vc_kwargs)
        kwargs["vc_position"] = vc_membership["position"]
    return kwargs


def _iter_scoped_devices(records: list[dict], *, site_scope: set[str] | None):
    """Yield (record, site_name, location_path) for devices that pass scope.

    Single resolve_location pass used by both `scope_devices` and
    `devices_to_entities`. Platform ONE assigns every device a site itself, so
    a record without one is unexpected and skipped (with a warning).
    """
    for record in records:
        site_name, location_path = resolve_location(record.get("location"), record["asset"])
        if site_name is None:
            asset = record["asset"]
            logger.warning(
                "Skipping device %s: Platform ONE reports no site for it",
                asset_label(asset),
            )
            continue
        if site_scope and site_name not in site_scope:
            continue
        yield record, site_name, location_path


def scope_devices(records: list[dict], *, site_scope: set[str] | None) -> list[dict]:
    """Return the device records whose resolved site is in site_scope (all, if no scope).

    Ownership: the backend scopes once up front (port fan-out must match the
    device list). Pass the result to `devices_to_entities` with
    `site_scope=None` so mapping does not re-filter by site. Direct callers
    that have not scoped yet may pass `site_scope` into `devices_to_entities`
    instead.
    """
    return [record for record, _, _ in _iter_scoped_devices(records, site_scope=site_scope)]


def _merge_site_coords(
    site_coords: dict[str, tuple[float | None, float | None]],
    site_name: str,
    location: dict | None,
) -> None:
    """Keep the first non-null lat/lon seen per site name."""
    if not location:
        return
    lat = _coord(location.get("site_latitude"))
    lon = _coord(location.get("site_longitude"))
    if lat is None and lon is None:
        return
    existing = site_coords.get(site_name)
    if existing is None:
        site_coords[site_name] = (lat, lon)
        return
    prev_lat, prev_lon = existing
    site_coords[site_name] = (
        prev_lat if prev_lat is not None else lat,
        prev_lon if prev_lon is not None else lon,
    )


def devices_to_entities(
    records: list[dict],
    *,
    site_scope: set[str] | None = None,
    virtual_chassis_entities: list[Entity] | None = None,
    vc_memberships: dict[str, dict] | None = None,
    primary_ips_by_cs_id: dict[str, dict[str, str]] | None = None,
) -> list[Entity]:
    """Map device records to Diode entities: one Site per distinct site, one
    nested Location per Building/Floor level in use, Devices, then
    VirtualChassis (if any).

    When the caller has already run `scope_devices` (backend tick path), pass
    `site_scope=None` so this does not re-filter by site. When calling
    directly with an unscoped list, pass `site_scope` here instead.

    `vc_memberships` is keyed by ConfigState device UUID (`cs_device_id`).
    `primary_ips_by_cs_id` supplies Device primary_ip4/primary_ip6 from
    ConfigState interface IPs (see `primary_ips_from_tables`); when absent,
    Assets `ip_address` is used only if it already includes a prefix.

    Devices with ``virtual_chassis`` / ``vc_position`` are emitted before the
    first-class VirtualChassis entities that set ``master``. NetBox rejects
    assigning a master that is not yet a chassis member; on a fresh VC create
    Diode applies entities in iterable order within a batch, so membership
    must land before master.
    """
    entities: list[Entity] = []
    resolved: list[tuple[dict, str, list[str]]] = []
    site_names: set[str] = set()
    location_paths: set[tuple[str, tuple[str, ...]]] = set()
    site_coords: dict[str, tuple[float | None, float | None]] = {}

    # One pass: filter (if site_scope set) + resolve. Does not call
    # scope_devices separately (avoids a second filter pass inside transform).
    for record, site_name, location_path in _iter_scoped_devices(records, site_scope=site_scope):
        resolved.append((record, site_name, location_path))
        site_names.add(site_name)
        _merge_site_coords(site_coords, site_name, record.get("location"))
        if location_path:
            location_paths.add((site_name, tuple(location_path)))

    for site_name in sorted(site_names):
        entities.append(Entity(site=Site(**_site_kwargs(site_name, site_coords.get(site_name)))))

    # expand_location_paths orders ancestors before descendants, so one pass
    # can thread `parent` through the cache.
    location_cache: dict[tuple[str, tuple[str, ...]], Location] = {}
    for site_name, path in expand_location_paths(location_paths):
        parent = location_cache.get((site_name, path[:-1])) if len(path) > 1 else None
        location = Location(name=path[-1], site=site_name, parent=parent)
        location_cache[(site_name, path)] = location
        entities.append(Entity(location=location))

    for record, site_name, location_path in resolved:
        location = location_cache.get((site_name, tuple(location_path))) if location_path else None
        cs_device_id = record.get("cs_device_id")
        membership = (vc_memberships or {}).get(cs_device_id) if cs_device_id else None
        primary_ips = (primary_ips_by_cs_id or {}).get(cs_device_id) if cs_device_id else None
        kwargs = _device_kwargs(
            record["asset"],
            site_name=site_name,
            location=location,
            cs_device=record.get("cs_device"),
            vc_membership=membership,
            primary_ips=primary_ips,
        )
        entities.append(Entity(device=Device(**kwargs)))

    # After member Devices so NetBox accepts VirtualChassis.master on create.
    if virtual_chassis_entities:
        entities.extend(virtual_chassis_entities)

    return entities
