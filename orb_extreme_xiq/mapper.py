"""XIQ -> Diode entities, with field-authority enforcement.

Field authority controls which Device attributes this worker asserts on
every run. Fields XIQ owns (the default set) are reasserted every sync, so
if a human edits them in NetBox they'll be flagged as drift once Assurance
is enabled. Fields dropped from authority are simply omitted from the
Device entity, handing ownership to NetBox/humans with zero re-drift.

`custom_fields` and `tags` are always emitted regardless of authority --
they're provenance/identity metadata (xiq_device_id, source:xiq), not
fields a human would meaningfully contest.
"""

from __future__ import annotations

import json
import re

from netboxlabs.diode.sdk.ingester import (
    CustomFieldValue,
    Device,
    DeviceType,
    Entity,
    Interface,
    Platform,
    Site,
)

from .identity import build_location_index, device_name, resolve_site_name, role_for

__all__ = [
    "DEFAULT_AUTHORITY",
    "MANUFACTURER",
    "build_location_index",
    "devices_to_entities",
    "ports_to_entities",
]

MANUFACTURER = "Extreme Networks"

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


def _cf_json(value: str) -> CustomFieldValue:
    return CustomFieldValue(json=value)


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


def _device_kwargs(device: dict, *, site_name: str | None, authority: frozenset, name_source: str) -> dict:
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
    return kwargs


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
    """Map XIQ devices to Diode entities: one Site per consolidated XIQ root
    location (asserting `xiq_locations`) plus one Device per device.
    """
    entities = []
    site_locations: dict[str, set[str]] = {}
    resolved: list[tuple[dict, str | None]] = []

    for device in devices:
        site_name, root_name = resolve_site_name(
            device.get("location_id"), location_index, location_site_mapping, default_site
        )
        if site_scope and site_name not in site_scope:
            continue
        resolved.append((device, site_name))
        if "site" in authority and root_name:
            site_locations.setdefault(site_name, set()).add(root_name)

    if "site" in authority:
        for site_name, locations in site_locations.items():
            entities.append(
                Entity(
                    site=Site(
                        name=site_name,
                        custom_fields={"xiq_locations": _cf_json(json.dumps(sorted(locations)))},
                    )
                )
            )

    for device, site_name in resolved:
        kwargs = _device_kwargs(device, site_name=site_name, authority=authority, name_source=name_source)
        entities.append(Entity(device=Device(**kwargs)))

    return entities


_SPEED_RE = re.compile(r"^SPEED_(\d+)([MG])$")

_DUPLEX_BY_TRANSMISSION_MODE = {"Full-duplex": "full", "Half-duplex": "half"}


def _speed_kbps(port_speed: str | None) -> int | None:
    """Parse e.g. 'SPEED_1000M' -> 1_000_000 Kbps. 'SPEED_AUTO' and unknown values -> None."""
    match = _SPEED_RE.match(port_speed or "")
    if not match:
        return None
    value, unit = match.groups()
    return int(value) * (1_000_000 if unit == "G" else 1_000)


def _port_custom_fields(port: dict) -> dict:
    custom_fields = {"xiq_port_id": _cf_text(str(port["id"]))}
    tagged_vlans = port.get("taggedVlans")
    if tagged_vlans:
        custom_fields["xiq_tagged_vlans"] = _cf_text(tagged_vlans)
    lldp_system_name = port.get("lldpSystemName")
    if lldp_system_name:
        custom_fields["xiq_lldp_neighbor"] = _cf_text(lldp_system_name)
    return custom_fields


def ports_to_entities(ports: list[dict], *, device: str) -> list:
    """Map one switch's wired portlist (client.get_wired_portlist) to Interface entities.

    `mode` and `type` are deliberately not asserted: on FLEX-UNI/Fabric-Attach
    deployments a port is mapped straight into an I-SID rather than a VLAN, so
    portMode/taggedVlans don't describe real port configuration there, and
    XIQ doesn't expose I-SID membership through any documented API endpoint
    to assert instead. taggedVlans is preserved as a raw custom field so the
    data isn't lost, rather than wired up as real (and potentially wrong)
    VLAN links.
    """
    entities = []
    for port in ports:
        entities.append(
            Entity(
                interface=Interface(
                    device=device,
                    name=port["ifName"],
                    enabled=port.get("status") == "UP",
                    speed=_speed_kbps(port.get("portSpeed")),
                    duplex=_DUPLEX_BY_TRANSMISSION_MODE.get(port.get("transmissionMode")),
                    description=port.get("ifAlias") or None,
                    custom_fields=_port_custom_fields(port),
                    tags=["source:xiq"],
                )
            )
        )
    return entities
