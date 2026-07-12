"""XIQ -> Diode entities: basic device/site/location inventory + interfaces.

Asserts name, serial, status, site, location, device_type/manufacturer,
platform, description, and primary_ip4 whenever XIQ reports the underlying
field -- all unconditionally (no configurable field-authority system, no
opt-in flags), matching the rest of this worker's "just always sync what's
available" convention.

`custom_fields` and `tags` are always emitted alongside -- they're
provenance metadata (extreme/xiq/discovered tags, xiq_network_policy), not
fields a human would meaningfully contest. Identity relies on the native
`serial` field (see `_device_kwargs`) rather than a separate immutable ID
custom field -- neither the real Cisco Meraki integration nor NetBox Labs'
generic discovery backends carry one; they rely on native `serial` the
same way.
"""

from __future__ import annotations

import re

from netboxlabs.diode.sdk.ingester import (
    CustomFieldValue,
    Device,
    DeviceType,
    Entity,
    Interface,
    Location,
    Platform,
    Site,
    WirelessLAN,
)

from . import bootstrap
from .identity import build_location_index, device_name, expand_location_paths, resolve_location

__all__ = [
    "build_location_index",
    "devices_to_entities",
    "ports_to_entities",
    "radios_to_entities",
]

MANUFACTURER = "Extreme Networks"

# Vendor/product/lifecycle tags, mirroring the flat-tag pattern NetBox Labs'
# own Cisco Meraki integration uses (e.g. "cisco", "meraki", "discovered")
# rather than one namespaced "source:xiq" tag. Derived from bootstrap.TAGS so
# the two can't drift apart.
PROVENANCE_TAGS = [tag["name"] for tag in bootstrap.TAGS]


def _status_for(device: dict) -> str:
    return "active" if device.get("connected") else "offline"


def _primary_ip4(device: dict) -> str | None:
    ip = device.get("ip_address")
    if not ip:
        return None
    return ip if "/" in ip else f"{ip}/32"


def _cf_text(value: str) -> CustomFieldValue:
    return CustomFieldValue(text=value)


def _network_policy_custom_fields(obj: dict) -> dict:
    custom_fields = {}
    network_policy = obj.get("network_policy_name")
    if network_policy:
        custom_fields["xiq_network_policy"] = _cf_text(network_policy)
    return custom_fields


def _device_kwargs(device: dict, *, site_name: str, location: Location | None, name_source: str) -> dict:
    kwargs = {
        "name": device_name(device, name_source),
        "serial": device.get("serial_number") or device.get("service_tag") or None,
        "status": _status_for(device),
        "site": Site(name=site_name),
        "custom_fields": _network_policy_custom_fields(device),
        "tags": PROVENANCE_TAGS,
    }
    if location is not None:
        kwargs["location"] = location
    if device.get("product_type"):
        kwargs["device_type"] = DeviceType(model=device["product_type"], manufacturer=MANUFACTURER)
        kwargs["manufacturer"] = MANUFACTURER
    if device.get("software_version"):
        kwargs["platform"] = Platform(name=device["software_version"], manufacturer=MANUFACTURER)
    if device.get("description"):
        kwargs["description"] = device["description"]
    primary_ip4 = _primary_ip4(device)
    if primary_ip4:
        kwargs["primary_ip4"] = primary_ip4
    return kwargs


def devices_to_entities(
    devices: list[dict],
    *,
    location_index: dict,
    default_site: str,
    name_source: str = "hostname",
    site_scope: set[str] | None = None,
) -> list:
    """Map XIQ devices to Diode entities: one Site per XIQ site, one nested
    Location per Building/Floor (etc.) level actually in use, plus one
    Device per device.
    """
    entities = []
    resolved: list[tuple[dict, str, list[str]]] = []
    site_names: set[str] = set()
    location_paths: set[tuple[str, tuple[str, ...]]] = set()

    for device in devices:
        site_name, location_path = resolve_location(device.get("location_id"), location_index, default_site)
        if site_scope and site_name not in site_scope:
            continue
        resolved.append((device, site_name, location_path))
        site_names.add(site_name)
        if location_path:
            location_paths.add((site_name, tuple(location_path)))

    for site_name in sorted(site_names):
        entities.append(Entity(site=Site(name=site_name)))

    # expand_location_paths orders every path's ancestors before itself, so a
    # single pass can thread `parent` through a cache instead of rebuilding
    # each prefix's chain from scratch (O(total locations), not O(depth^2)).
    location_cache: dict[tuple[str, tuple[str, ...]], Location] = {}
    for site_name, path in expand_location_paths(location_paths):
        parent = location_cache.get((site_name, path[:-1])) if len(path) > 1 else None
        location = Location(name=path[-1], site=site_name, parent=parent)
        location_cache[(site_name, path)] = location
        entities.append(Entity(location=location))

    for device, site_name, location_path in resolved:
        location = location_cache.get((site_name, tuple(location_path))) if location_path else None
        kwargs = _device_kwargs(device, site_name=site_name, location=location, name_source=name_source)
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


# XiqRadio.mode (xcloudiq-openapi.yaml) -> NetBox Interface type. _11be_* (WiFi
# 7) has no confirmed NetBox InterfaceTypeChoices entry as of this writing --
# left unmapped (type unset) rather than guessing at a value NetBox might reject.
_RADIO_TYPE_BY_MODE = {
    "_11a": "ieee802.11a",
    "_11bg": "ieee802.11g",
    "_11an": "ieee802.11n",
    "_11ng": "ieee802.11n",
    "_11ac": "ieee802.11ac",
    "_11ax_2g": "ieee802.11ax",
    "_11ax_5g": "ieee802.11ax",
    "_11ax_6g": "ieee802.11ax",
}

_CHANNEL_WIDTH_MHZ = {
    "MHZ_20": 20.0,
    "MHZ_40": 40.0,
    "MHZ_80": 80.0,
    "MHZ_160": 160.0,
    "MHZ_320": 320.0,
}


def _channel_frequency_mhz(frequency_band: str | None, channel_number: int | None) -> float | None:
    """Channel-center-frequency in MHz from XIQ's band label + channel number,
    via the standard IEEE 802.11 channel-numbering formulas (not XIQ-specific,
    so this doesn't need live-API verification the way field *names* do):
    2.4GHz = 2407 + 5*channel (channel 14, Japan-only 802.11b, is the sole
    exception at 2484 MHz and isn't special-cased here); 5GHz = 5000 + 5*channel;
    6GHz = 5950 + 5*channel.
    """
    if channel_number is None:
        return None
    offset = {"2.4GHz": 2407.0, "5GHz": 5000.0, "6GHz": 5950.0}.get(frequency_band)
    if offset is None:
        return None
    return offset + 5.0 * channel_number


def _radio_kwargs(radio: dict, *, device: str, ssids: list[str]) -> dict:
    return {
        "device": device,
        "name": radio["name"],
        "type": _RADIO_TYPE_BY_MODE.get(radio.get("mode")),
        "rf_role": "ap",
        "tx_power": radio.get("power"),
        "primary_mac_address": radio.get("mac_address") or None,
        "rf_channel_frequency": _channel_frequency_mhz(radio.get("frequency"), radio.get("channel_number")),
        "rf_channel_width": _CHANNEL_WIDTH_MHZ.get(radio.get("channel_width")),
        "wireless_lans": ssids,
        "tags": PROVENANCE_TAGS,
    }


# XiqWirelessWlan.ssid_security_type (xcloudiq-openapi.yaml) -> NetBox
# WirelessLAN auth_type. Any unrecognized/missing value falls back to "open",
# mirroring the real Cisco Meraki integration's own fallback convention.
_AUTH_TYPE_BY_SECURITY_TYPE = {
    "OPEN": "open",
    "ENHANCED_OPEN": "open",
    "PPSK": "wpa-personal",
    "PSK": "wpa-personal",
    "WEP": "wep",
    "TYPE_802DOT1X": "wpa-enterprise",
}


def _wlan_kwargs(ssid: str, wlan: dict) -> dict:
    return {
        "ssid": ssid,
        # All WLANs seen here are currently broadcasting on a live radio, so
        # "active" is a safe default -- unlike Meraki/Mist, this data comes
        # from XiqWirelessWlan (nested per-radio broadcast info), which
        # doesn't carry the SSID's own configured enabled/disabled state
        # (`ssid_status` here is OPEN/CLOSED, which reads as broadcast
        # visibility, not enabled/disabled, so it isn't used for this).
        "status": "active",
        "auth_type": _AUTH_TYPE_BY_SECURITY_TYPE.get(wlan.get("ssid_security_type"), "open"),
        # auth_cipher isn't set: XiqWirelessWlan doesn't carry cipher detail
        # (CCMP/TKIP/...) -- only the fuller /ssids endpoint's
        # access_security.encryption_method would, and this worker doesn't
        # call it (see README).
        "custom_fields": _network_policy_custom_fields(wlan),
        "tags": PROVENANCE_TAGS,
    }


def _record_wlan(wlans: dict[str, dict], wlan: dict) -> str | None:
    """Record `wlan` in the cross-AP dedup dict `wlans` (first-seen wins,
    see radios_to_entities) and return its ssid, or None if it has none.
    """
    ssid = wlan.get("ssid")
    if not ssid:
        return None
    wlans.setdefault(ssid, wlan)
    return ssid


def radios_to_entities(radio_infos: list[dict], *, device_names: dict[int, str]) -> list:
    """Map /devices/radio-information records to Interface (one per radio)
    and WirelessLAN (one per unique SSID, deduped across every AP) entities.

    device_names maps a XIQ device id to the NetBox device name it was
    already mapped to (see identity.device_name) -- radio_infos entries for
    any id not present here (e.g. filtered out by site_scope) are skipped.

    WLANs aren't site-scoped: XIQ network policies (unlike Meraki networks)
    aren't inherently 1:1 with a site, and a single SSID can broadcast from
    APs across many sites, so there's no single correct scope_site to assert.
    If the same SSID name is seen with a different network_policy_name on
    different radios, the first one encountered wins (documented behavior,
    not expected to matter in practice -- SSID names are broadcast-visible
    and organizations avoid reusing one across unrelated policies).
    """
    wlans: dict[str, dict] = {}
    radio_kwargs_list = []

    for radio_info in radio_infos:
        device = device_names.get(radio_info.get("device_id"))
        if device is None:
            continue
        for radio in radio_info.get("radios") or []:
            ssids = [ssid for wlan in radio.get("wlans") or [] if (ssid := _record_wlan(wlans, wlan))]
            radio_kwargs_list.append(_radio_kwargs(radio, device=device, ssids=ssids))

    entities = [Entity(wireless_lan=WirelessLAN(**_wlan_kwargs(ssid, wlan))) for ssid, wlan in wlans.items()]
    entities.extend(Entity(interface=Interface(**kwargs)) for kwargs in radio_kwargs_list)
    return entities
