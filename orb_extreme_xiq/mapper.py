"""XIQ -> Diode entities: basic device/site inventory + interfaces.

This intentionally asserts a small, fixed set of fields (name, serial,
status, site) rather than a configurable one -- ownership of anything not
listed here (rack, description, platform, ...) stays with NetBox/humans.

`custom_fields` and `tags` are provenance/identity metadata (xiq_device_id,
source:xiq), always emitted alongside the fixed field set.
"""

from __future__ import annotations

import re

from netboxlabs.diode.sdk.ingester import CustomFieldValue, Device, Entity, Interface, Site

from .identity import build_location_index, device_name, resolve_site_name

__all__ = [
    "build_location_index",
    "devices_to_entities",
    "ports_to_entities",
]


def _status_for(device: dict) -> str:
    return "active" if device.get("connected") else "offline"


def _cf_text(value: str) -> CustomFieldValue:
    return CustomFieldValue(text=value)


def _device_custom_fields(device: dict) -> dict:
    return {"xiq_device_id": _cf_text(str(device["id"]))}


def _device_kwargs(device: dict, *, site_name: str, name_source: str) -> dict:
    return {
        "name": device_name(device, name_source),
        "serial": device.get("serial_number") or device.get("service_tag") or None,
        "status": _status_for(device),
        "site": Site(name=site_name),
        "custom_fields": _device_custom_fields(device),
        "tags": ["source:xiq"],
    }


def devices_to_entities(
    devices: list[dict],
    *,
    location_index: dict,
    default_site: str,
    name_source: str = "hostname",
    site_scope: set[str] | None = None,
) -> list:
    """Map XIQ devices to Diode entities: one Site per XIQ location (1:1)
    plus one Device per device.
    """
    entities = []
    resolved: list[tuple[dict, str]] = []
    site_names: set[str] = set()

    for device in devices:
        site_name = resolve_site_name(device.get("location_id"), location_index, default_site)
        if site_scope and site_name not in site_scope:
            continue
        resolved.append((device, site_name))
        site_names.add(site_name)

    for site_name in sorted(site_names):
        entities.append(Entity(site=Site(name=site_name)))

    for device, site_name in resolved:
        kwargs = _device_kwargs(device, site_name=site_name, name_source=name_source)
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
    return {"xiq_port_id": _cf_text(str(port["id"]))}


def ports_to_entities(ports: list[dict], *, device: str) -> list:
    """Map one switch's wired portlist (client.get_wired_portlist) to Interface entities.

    `mode` and `type` are deliberately not asserted: on FLEX-UNI/Fabric-Attach
    deployments a port is mapped straight into an I-SID rather than a VLAN, so
    portMode/taggedVlans don't describe real port configuration there, and
    XIQ doesn't expose I-SID membership through any documented API endpoint
    to assert instead.
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
