"""XIQ -> Diode entities, with field-authority enforcement.

Field authority controls which Device attributes this worker asserts on
every run. Fields XIQ owns (the default set) are reasserted every sync, so
if a human edits them in NetBox they'll be flagged as drift once Assurance
is enabled. Fields dropped from authority are simply omitted from the
Device entity, handing ownership to NetBox/humans with zero re-drift.

`custom_fields` and `tags` are always emitted regardless of authority --
they're provenance metadata (extreme/xiq/discovered tags, xiq_network_policy),
not fields a human would meaningfully contest.
"""

from __future__ import annotations

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

# Vendor/product/lifecycle tags, mirroring the flat-tag pattern NetBox Labs'
# own Cisco Meraki integration uses (e.g. "cisco", "meraki", "discovered")
# rather than one namespaced "source:xiq" tag.
PROVENANCE_TAGS = ["extreme-networks", "xiq", "discovered"]

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


def _device_custom_fields(device: dict) -> dict:
    custom_fields = {}
    network_policy = device.get("network_policy_name")
    if network_policy:
        custom_fields["xiq_network_policy"] = _cf_text(network_policy)
    return custom_fields


def _device_kwargs(device: dict, *, site_name: str | None, authority: frozenset, name_source: str) -> dict:
    kwargs: dict = {
        "name": device_name(device, name_source),
        "serial": device.get("serial_number") or device.get("service_tag") or None,
        "custom_fields": _device_custom_fields(device),
        "tags": PROVENANCE_TAGS,
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
    default_site: str,
    authority: frozenset = DEFAULT_AUTHORITY,
    name_source: str = "hostname",
    site_scope: set[str] | None = None,
) -> list:
    """Map XIQ devices to Diode entities: one Site per XIQ root location
    plus one Device per device.
    """
    entities = []
    site_names: set[str] = set()
    resolved: list[tuple[dict, str | None]] = []

    for device in devices:
        site_name, root_name = resolve_site_name(device.get("location_id"), location_index, default_site)
        if site_scope and site_name not in site_scope:
            continue
        resolved.append((device, site_name))
        if "site" in authority and root_name:
            site_names.add(site_name)

    if "site" in authority:
        for site_name in site_names:
            entities.append(Entity(site=Site(name=site_name)))

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
    I-SID membership through any documented API endpoint to assert instead --
    confirmed this fleet does use Fabric-Attach on at least some ports, so
    trusting `portMode` (trunk/access) directly would risk asserting wrong
    VLAN-mode data. VLAN data (`taggedVlans`) is not currently mapped either.

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
