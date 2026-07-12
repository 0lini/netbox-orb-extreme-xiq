"""XIQ -> Diode entities: basic device/site inventory + interfaces.

This intentionally asserts a small, fixed set of fields (name, serial,
status, site) rather than a configurable one -- ownership of anything not
listed here (rack, description, platform, ...) stays with NetBox/humans.

`custom_fields` and `tags` are always emitted alongside the fixed field set
-- they're provenance metadata (extreme/xiq/discovered tags,
xiq_network_policy), not fields a human would meaningfully contest. Identity
relies on the native `serial` field (see `_device_kwargs`) rather than a
separate immutable ID custom field -- neither the real Cisco Meraki
integration nor NetBox Labs' generic discovery backends carry one; they rely
on native `serial` the same way.
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

# Vendor/product/lifecycle tags, mirroring the flat-tag pattern NetBox Labs'
# own Cisco Meraki integration uses (e.g. "cisco", "meraki", "discovered")
# rather than one namespaced "source:xiq" tag.
PROVENANCE_TAGS = ["extreme-networks", "xiq", "discovered"]


def _status_for(device: dict) -> str:
    return "active" if device.get("connected") else "offline"


def _cf_text(value: str) -> CustomFieldValue:
    return CustomFieldValue(text=value)


def _device_custom_fields(device: dict) -> dict:
    custom_fields = {}
    network_policy = device.get("network_policy_name")
    if network_policy:
        custom_fields["xiq_network_policy"] = _cf_text(network_policy)
    return custom_fields


def _device_kwargs(device: dict, *, site_name: str, name_source: str) -> dict:
    return {
        "name": device_name(device, name_source),
        "serial": device.get("serial_number") or device.get("service_tag") or None,
        "status": _status_for(device),
        "site": Site(name=site_name),
        "custom_fields": _device_custom_fields(device),
        "tags": PROVENANCE_TAGS,
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


# Best-effort NetBox interface type from XIQ's *actual negotiated* speed --
# not a real media/capability signal (XIQ doesn't expose SFP-vs-copper or a
# capability list the way some other platforms do), just the same kind of
# speed-based guess used elsewhere. >=10G is assumed SFP+ since copper 10G is
# rare on switch uplinks; everything else is assumed copper (RJ45).
_TYPE_BY_SPEED = {
    ("100", "M"): "100base-tx",
    ("1000", "M"): "1000base-t",
    ("2500", "M"): "2.5gbase-t",
    ("5000", "M"): "5gbase-t",
    ("10", "G"): "10gbase-x-sfpp",
    ("25", "G"): "25gbase-x-sfp28",
    ("40", "G"): "40gbase-x-qsfpp",
    ("100", "G"): "100gbase-x-qsfp28",
}


def _type_for_speed(port_speed: str | None) -> str | None:
    match = _SPEED_RE.match(port_speed or "")
    if not match:
        return None
    return _TYPE_BY_SPEED.get(match.groups())


def _port_custom_fields(port: dict) -> dict:
    return {"xiq_port_id": _cf_text(str(port["id"]))}


def ports_to_entities(ports: list[dict], *, device: str) -> list:
    """Map one switch's wired portlist (client.get_wired_portlist) to Interface entities.

    XIQ's port `status` is link/operational state (is there an active physical
    link), not administrative shut/no-shut state -- this endpoint doesn't expose
    admin state at all. It's therefore asserted as `mark_connected`, NetBox's
    field for "this interface is physically connected to something" (used for
    the cabling/topology view without a full Cable object), not as `enabled`,
    which conventionally means administrative state and would misrepresent a
    link-down port as "shut down by an operator" when XIQ can't actually tell
    us that. `enabled` is left unset rather than asserting a fake default.

    `mode` is deliberately not asserted: on FLEX-UNI/Fabric-Attach deployments
    a port is mapped straight into an I-SID rather than a VLAN, so `portMode`
    doesn't describe real port configuration there, and XIQ doesn't expose
    I-SID membership through any documented API endpoint to assert instead.
    VLAN data (`taggedVlans`) is not currently mapped either.

    `type` is a best-effort guess from `portSpeed` alone (see `_type_for_speed`
    -- XIQ doesn't expose a capability list or SFP-vs-copper signal), left
    unset when the speed is unrecognized (e.g. `SPEED_AUTO`) rather than
    guessing further.
    """
    entities = []
    for port in ports:
        entities.append(
            Entity(
                interface=Interface(
                    device=device,
                    name=port["ifName"],
                    type=_type_for_speed(port.get("portSpeed")),
                    mark_connected=port.get("status") == "UP",
                    speed=_speed_kbps(port.get("portSpeed")),
                    duplex=_DUPLEX_BY_TRANSMISSION_MODE.get(port.get("transmissionMode")),
                    description=port.get("ifAlias") or None,
                    custom_fields=_port_custom_fields(port),
                    tags=PROVENANCE_TAGS,
                )
            )
        )
    return entities
