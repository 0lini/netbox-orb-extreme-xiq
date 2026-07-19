"""AP radio and WirelessLAN mapping."""

from __future__ import annotations

from collections import defaultdict

from netboxlabs.diode.sdk.ingester import Entity, Interface, WirelessLAN

from .common import PROVENANCE_TAGS, _cf_text

# Keys `radios_to_entities` reads from each device's wireless tables dict.
WIRELESS_ENTITY_TABLE_KEYS = frozenset(
    {
        "wireless_interfaces",
        "wireless_states",
        "ssid_configs",
        "ssid_states",
    }
)
_RADIO_TYPE_BY_MODE = {
    "_11a": "ieee802.11a",
    "_11bg": "ieee802.11g",
    "_11an": "ieee802.11n",
    "_11ng": "ieee802.11n",
    "_11ac": "ieee802.11ac",
    "_11ax_2g": "ieee802.11ax",
    "_11ax_5g": "ieee802.11ax",
    "_11ax_6g": "ieee802.11ax",
    "11a": "ieee802.11a",
    "11bg": "ieee802.11g",
    "11an": "ieee802.11n",
    "11ng": "ieee802.11n",
    "11ac": "ieee802.11ac",
    "11ax": "ieee802.11ax",
    "11ax_2g": "ieee802.11ax",
    "11ax_5g": "ieee802.11ax",
    "11ax_6g": "ieee802.11ax",
    "ieee802.11a": "ieee802.11a",
    "ieee802.11b": "ieee802.11b",
    "ieee802.11g": "ieee802.11g",
    "ieee802.11n": "ieee802.11n",
    "ieee802.11ac": "ieee802.11ac",
    "ieee802.11ax": "ieee802.11ax",
}

# channel_width is an integer in ConfigState; only values that are already
# standard IEEE channel widths in MHz are asserted.
_VERIFIED_CHANNEL_WIDTH_MHZ = frozenset({20, 40, 80, 160, 320})


def _channel_frequency_mhz(band: str | None, channel: int | None) -> float | None:
    """Channel-center frequency in MHz from band label + channel number.

    Uses standard IEEE 802.11 channel-numbering formulas (not Extreme-specific):
    2.4 GHz = 2407 + 5*channel; 5 GHz = 5000 + 5*channel; 6 GHz = 5950 + 5*channel.
    """
    if channel is None:
        return None
    try:
        channel_number = int(channel)
    except (TypeError, ValueError):
        return None
    if not band:
        return None
    # Collapse separators so BAND_5_GHZ / "5 GHz" / "5g" all normalize alike.
    normalized = str(band).casefold().replace(" ", "").replace("_", "").replace("-", "")
    if "6g" in normalized or normalized in {"6", "band6"}:
        offset = 5950.0
    elif "2.4" in normalized or "2,4" in normalized or normalized in {"24g", "2g", "band24", "band2.4"}:
        offset = 2407.0
    elif "5g" in normalized or normalized in {"5", "band5"}:
        offset = 5000.0
    else:
        return None
    return offset + 5.0 * channel_number


def _radio_type(radio_mode: str | None) -> str | None:
    if not radio_mode:
        return None
    key = str(radio_mode).strip()
    mapped = _RADIO_TYPE_BY_MODE.get(key) or _RADIO_TYPE_BY_MODE.get(key.casefold())
    if mapped:
        return mapped
    compact = key.casefold().replace(" ", "").replace("-", "").replace(".", "")
    # Wi-Fi 7 / 11be has no confirmed NetBox Interface type here — leave unset.
    if "11be" in compact:
        return None
    for needle, iface_type in (
        ("11ax", "ieee802.11ax"),
        ("11ac", "ieee802.11ac"),
        ("11n", "ieee802.11n"),
        ("11g", "ieee802.11g"),
        ("11b", "ieee802.11b"),
        ("11a", "ieee802.11a"),
    ):
        if needle in compact:
            return iface_type
    return None


def _channel_width_mhz(value) -> float | None:
    try:
        width = int(value)
    except (TypeError, ValueError):
        return None
    if width in _VERIFIED_CHANNEL_WIDTH_MHZ:
        return float(width)
    return None


def _tx_power(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _auth_type_from_encryption(encryption: str | None) -> str | None:
    """Map AssetSsidState.encryption to NetBox WirelessLAN auth_type.

    Unknown / empty values leave auth_type unset (no invented "open").
    """
    if not encryption or not str(encryption).strip():
        return None
    compact = str(encryption).casefold().replace(" ", "").replace("-", "").replace("_", "")
    if compact in {"open", "enhancedopen", "none", "owe"} or compact.startswith("open"):
        return "open"
    if "wep" in compact:
        return "wep"
    if any(token in compact for token in ("8021x", "enterprise", "radius", "eap", "dot1x")):
        return "wpa-enterprise"
    if any(token in compact for token in ("psk", "ppsk", "sae", "personal", "wpa2", "wpa3")):
        return "wpa-personal"
    return None


def _split_if_names(value) -> list[str]:
    """Normalize AssetSsid*.if_names into a list of interface name strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        # JSON-ish list serialized as a string.
        inner = text[1:-1].strip()
        if not inner:
            return []
        parts = [part.strip().strip("'\"") for part in inner.split(",")]
        return [part for part in parts if part]
    for sep in (",", ";", "|"):
        if sep in text:
            return [part.strip() for part in text.split(sep) if part.strip()]
    return [text]


def _wireless_radio_key(row: dict) -> str | None:
    interface_id = str(row.get("asset_interface_id") or "").strip()
    if interface_id:
        return f"id:{interface_id}"
    device_id = str(row.get("asset_device_id") or "").strip()
    name = str(row.get("name") or "").strip()
    if device_id and name:
        return f"name:{device_id}:{name}"
    return None


def _wlan_status(enabled) -> str | None:
    if enabled is True:
        return "active"
    if enabled is False:
        return "disabled"
    return None


def _wlan_kwargs(ssid: str, *, enabled, encryption: str | None) -> dict:
    kwargs: dict = {
        "ssid": ssid,
        "tags": PROVENANCE_TAGS,
    }
    status = _wlan_status(enabled)
    if status is not None:
        kwargs["status"] = status
    # When enabled is unknown, leave status unset rather than inventing "active".
    auth_type = _auth_type_from_encryption(encryption)
    if auth_type is not None:
        kwargs["auth_type"] = auth_type
    return kwargs


def _radio_interface_kwargs(
    *,
    device: str,
    name: str,
    config: dict,
    state: dict,
    ssids: list[str],
) -> dict:
    interface_id = str(config.get("asset_interface_id") or state.get("asset_interface_id") or "")
    kwargs: dict = {
        "device": device,
        "name": name,
        "rf_role": "ap",
        "tags": PROVENANCE_TAGS,
        "custom_fields": {"platformone_interface_id": _cf_text(interface_id)} if interface_id else {},
    }
    if "enabled" in config and isinstance(config.get("enabled"), bool):
        kwargs["enabled"] = config["enabled"]
    radio_type = _radio_type(state.get("radio_mode") or config.get("radio_mode"))
    if radio_type is not None:
        kwargs["type"] = radio_type
    tx_power = _tx_power(state.get("power"))
    if tx_power is not None:
        kwargs["tx_power"] = tx_power
    bssid = state.get("bssid")
    if bssid:
        kwargs["primary_mac_address"] = str(bssid)
    frequency = _channel_frequency_mhz(state.get("band"), state.get("channel"))
    if frequency is not None:
        kwargs["rf_channel_frequency"] = frequency
    width = _channel_width_mhz(state.get("channel_width"))
    if width is not None:
        kwargs["rf_channel_width"] = width
    if ssids:
        kwargs["wireless_lans"] = ssids
    return kwargs


def radios_to_entities(
    tables_by_device: dict[str, dict[str, list[dict]]],
    *,
    device_names: dict[str, str],
) -> list[Entity]:
    """Map ConfigState wireless + SSID tables to Interface and WirelessLAN entities.

    `tables_by_device` maps ConfigState AssetDevice UUID -> wireless table
    buckets (`wireless_interfaces`, `wireless_states`, `ssid_configs`,
    `ssid_states`). `device_names` maps the same UUID to the NetBox device
    name already used for Device entities.

    Each radio becomes an Interface with native RF fields (`rf_role`,
    `tx_power`, `rf_channel_frequency`, `rf_channel_width`, `type`,
    `primary_mac_address`, `wireless_lans`). Each distinct SSID becomes a
    WirelessLAN (`ssid`, `status`, `auth_type` when encryption maps cleanly).
    WLANs are not site-scoped: the same SSID can broadcast from APs in many
    sites. SSIDs link to radios via `AssetSsid*.if_names` and any
    `ssid_name` on wireless interface state rows.
    """
    wlans: dict[str, dict] = {}
    ssids_by_radio: dict[tuple[str, str], list[str]] = defaultdict(list)
    radio_rows: dict[tuple[str, str], dict] = {}

    for device_id, tables in tables_by_device.items():
        if device_id not in device_names:
            continue
        configs = tables.get("wireless_interfaces") or []
        states = tables.get("wireless_states") or []
        ssid_configs = tables.get("ssid_configs") or []
        ssid_states = tables.get("ssid_states") or []

        radios: dict[str, dict] = {}
        for row in configs:
            key = _wireless_radio_key(row)
            if not key:
                continue
            radios.setdefault(key, {"config": {}, "states": []})["config"] = row
        for row in states:
            key = _wireless_radio_key(row)
            if not key:
                continue
            radios.setdefault(key, {"config": {}, "states": []})["states"].append(row)

        name_to_key: dict[str, str] = {}
        for key, radio in radios.items():
            config = radio["config"]
            state = (radio["states"] or [{}])[0]
            name = str(config.get("name") or state.get("name") or "").strip()
            if not name:
                continue
            name_to_key[name] = key
            radio_rows[(device_id, key)] = {
                "device": device_names[device_id],
                "name": name,
                "config": config,
                "states": radio["states"],
            }
            for state_row in radio["states"]:
                ssid = str(state_row.get("ssid_name") or "").strip()
                if ssid and ssid not in ssids_by_radio[(device_id, key)]:
                    ssids_by_radio[(device_id, key)].append(ssid)
                    wlans.setdefault(ssid, {"enabled": None, "encryption": None})

        encryption_by_ssid = {
            str(row.get("name") or "").strip(): row.get("encryption")
            for row in ssid_states
            if str(row.get("name") or "").strip()
        }
        for row in ssid_configs:
            ssid = str(row.get("name") or "").strip()
            if not ssid:
                continue
            entry = wlans.setdefault(ssid, {"enabled": None, "encryption": None})
            if isinstance(row.get("enabled"), bool):
                entry["enabled"] = row["enabled"]
            if entry.get("encryption") is None and encryption_by_ssid.get(ssid) is not None:
                entry["encryption"] = encryption_by_ssid[ssid]
            for if_name in _split_if_names(row.get("if_names")):
                radio_key = name_to_key.get(if_name)
                if radio_key and ssid not in ssids_by_radio[(device_id, radio_key)]:
                    ssids_by_radio[(device_id, radio_key)].append(ssid)
        for row in ssid_states:
            ssid = str(row.get("name") or "").strip()
            if not ssid:
                continue
            entry = wlans.setdefault(ssid, {"enabled": None, "encryption": None})
            if entry.get("encryption") is None and row.get("encryption") is not None:
                entry["encryption"] = row.get("encryption")
            for if_name in _split_if_names(row.get("if_names")):
                radio_key = name_to_key.get(if_name)
                if radio_key and ssid not in ssids_by_radio[(device_id, radio_key)]:
                    ssids_by_radio[(device_id, radio_key)].append(ssid)

    entities = [
        Entity(
            wireless_lan=WirelessLAN(
                **_wlan_kwargs(ssid, enabled=meta.get("enabled"), encryption=meta.get("encryption"))
            )
        )
        for ssid, meta in sorted(wlans.items())
    ]
    for (device_id, key), radio in sorted(
        radio_rows.items(), key=lambda item: (item[1]["device"], item[1]["name"])
    ):
        state = next((row for row in radio["states"] if row), {})
        entities.append(
            Entity(
                interface=Interface(
                    **_radio_interface_kwargs(
                        device=radio["device"],
                        name=radio["name"],
                        config=radio["config"],
                        state=state,
                        ssids=ssids_by_radio.get((device_id, key), []),
                    )
                )
            )
        )
    return entities
