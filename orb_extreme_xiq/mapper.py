"""XIQ -> Diode entities, with field-authority enforcement.

Field authority controls which Device attributes this worker asserts on
every run. Fields XIQ owns (the default set) are reasserted every sync, so
if a human edits them in NetBox they'll be flagged as drift once Assurance
is enabled. Fields dropped from authority are simply omitted from the
Device entity, handing ownership to NetBox/humans with zero re-drift.

`custom_fields` and `tags` are always emitted regardless of authority --
they're provenance/identity metadata (xiq_device_id, source:xiq), not
fields a human would meaningfully contest. The "site" authority key also
covers the Location tree and each Device's `location=`: dropping it hands
XIQ's *entire* physical-placement story (site + location) to humans.

A device's `primary_ip4` is never a bare address string: NetBox requires a
device's primary IP to be assigned to one of its own interfaces, so a
synthetic "mgmt0" Interface + an IPAddress assigned to it are asserted
alongside the device (matching how the official Mist integration backs
primary IPs with a real Interface/assigned_object, not a floating address).
"""

from __future__ import annotations

from netboxlabs.diode.sdk.ingester import (
    CustomFieldValue,
    Device,
    DeviceType,
    Entity,
    Interface,
    IPAddress,
    Location,
    Platform,
    Site,
)

from .identity import build_location_index, device_name, location_ancestor_chain, resolve_site_name, role_for

__all__ = [
    "DEFAULT_AUTHORITY",
    "MANUFACTURER",
    "build_location_index",
    "devices_to_entities",
]

MANUFACTURER = "Extreme Networks"
MGMT_INTERFACE_NAME = "mgmt0"

DEFAULT_AUTHORITY = frozenset(
    {
        "site",
        "role",
        "device_type",
        "platform",
        "status",
        "description",
        "primary_ip",
    }
)


def _status_for(device: dict) -> str:
    return "active" if device.get("connected") else "offline"


def _primary_ip(device: dict) -> str | None:
    ip = device.get("ip_address")
    if not ip:
        return None
    return ip if "/" in ip else f"{ip}/32"


def _cf_text(value: str) -> CustomFieldValue:
    return CustomFieldValue(text=value)


def _management_interface_entities(device: dict, *, name: str) -> list[Entity]:
    """A synthetic mgmt Interface + the IPAddress assigned to it, for `device`.

    XIQ gives us a device's IP but no real port/interface data. NetBox
    requires a device's primary IP to be assigned to one of its own
    interfaces, so a bare `primary_ip4="1.2.3.4"` string on Device is not
    enough -- without this, the address has no assigned_object and can't
    validly be a primary IP.
    """
    ip = _primary_ip(device)
    if not ip:
        return []
    interface = Interface(device=name, name=MGMT_INTERFACE_NAME, type="virtual", mgmt_only=True, enabled=True)
    ip_address = IPAddress(
        address=ip, assigned_object_interface=Interface(name=MGMT_INTERFACE_NAME), device=name
    )
    return [Entity(interface=interface), Entity(ip_address=ip_address)]


def _device_custom_fields(device: dict) -> dict:
    custom_fields = {"xiq_device_id": _cf_text(str(device["id"]))}
    network_policy = device.get("network_policy_name")
    if network_policy:
        custom_fields["xiq_network_policy"] = _cf_text(network_policy)
    return custom_fields


def _device_tags(device: dict) -> list[str]:
    tags = ["source:xiq"]
    org_id = device.get("org_id")
    if org_id is not None:
        tags.append(f"xiq-org:{org_id}")
    return tags


def _device_kwargs(
    device: dict,
    *,
    site_name: str | None,
    location_name: str | None,
    authority: frozenset,
    name_source: str,
) -> dict:
    kwargs: dict = {
        "name": device_name(device, name_source),
        "serial": device.get("serial_number") or device.get("service_tag") or None,
        "custom_fields": _device_custom_fields(device),
        "tags": _device_tags(device),
    }
    if "status" in authority:
        kwargs["status"] = _status_for(device)
    if "role" in authority:
        kwargs["role"] = role_for(device.get("device_function"))
    if "device_type" in authority and device.get("product_type"):
        kwargs["device_type"] = DeviceType(model=device["product_type"], manufacturer=MANUFACTURER)
        kwargs["manufacturer"] = MANUFACTURER
    if "platform" in authority and device.get("software_version"):
        kwargs["platform"] = Platform(name=device["software_version"], manufacturer=MANUFACTURER)
    if "description" in authority and device.get("description"):
        kwargs["description"] = device["description"]
    if "primary_ip" in authority and _primary_ip(device):
        kwargs["primary_ip4"] = _primary_ip(device)
    if "site" in authority and site_name:
        kwargs["site"] = Site(name=site_name)
        if location_name:
            kwargs["location"] = Location(name=location_name)
    return kwargs


def _location_entity(location_id: int, location_index: dict, site_name: str) -> Entity:
    entry = location_index[location_id]
    kwargs: dict = {
        "name": entry["name"],
        "site": Site(name=site_name),
        "custom_fields": {"xiq_location_id": _cf_text(str(location_id))},
    }
    parent_id = entry["parent_id"]
    if parent_id is not None:
        kwargs["parent"] = location_index[parent_id]["name"]
    return Entity(location=Location(**kwargs))


def devices_to_entities(
    devices: list[dict],
    *,
    location_index: dict,
    location_site_mapping: dict,
    default_site: str,
    authority: frozenset = DEFAULT_AUTHORITY,
    name_source: str = "hostname",
    site_scope: set[str] | None = None,
) -> list:
    """Map XIQ devices to Diode entities: the Location tree each device sits
    in (nested under its resolved Site, preserving XIQ's hierarchy), one
    Device per device, and -- when the device has an IP -- a backing mgmt
    Interface + IPAddress so `primary_ip4` is a validly assigned address.
    """
    entities = []
    resolved: list[tuple[dict, str | None, int | None]] = []
    used_location_ids: list[int] = []
    seen_location_ids: set[int] = set()

    for device in devices:
        location_id = device.get("location_id")
        site_name = resolve_site_name(location_id, location_index, location_site_mapping, default_site)
        if site_scope and site_name not in site_scope:
            continue
        resolved.append((device, site_name, location_id))
        if "site" in authority:
            for ancestor_id in location_ancestor_chain(location_id, location_index):
                if ancestor_id not in seen_location_ids:
                    seen_location_ids.add(ancestor_id)
                    used_location_ids.append(ancestor_id)

    if "site" in authority:
        for location_id in used_location_ids:
            # Every entry carries its own root_name, so this resolves the
            # same way regardless of location_id's depth in the tree.
            site_name = resolve_site_name(location_id, location_index, location_site_mapping, default_site)
            entities.append(_location_entity(location_id, location_index, site_name))

    for device, site_name, location_id in resolved:
        location_name = location_index.get(location_id, {}).get("name") if "site" in authority else None
        kwargs = _device_kwargs(
            device,
            site_name=site_name,
            location_name=location_name,
            authority=authority,
            name_source=name_source,
        )
        entities.append(Entity(device=Device(**kwargs)))
        if "primary_ip" in authority:
            entities.extend(_management_interface_entities(device, name=kwargs["name"]))

    return entities
