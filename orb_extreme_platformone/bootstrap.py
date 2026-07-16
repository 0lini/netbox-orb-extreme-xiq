"""One-time idempotent NetBox schema setup: custom fields + provenance tags.

Uses the NetBox REST API directly (not Diode) because field definitions are
schema, not data. Skips gracefully if no NetBox credentials are configured.
"""

from __future__ import annotations

import requests

CUSTOM_FIELDS = [
    {
        "name": "platformone_device_id",
        "label": "Platform ONE Device ID",
        "type": "text",
        "object_types": ["dcim.device"],
        "description": (
            "Immutable Extreme Platform ONE device id (Assets API device_id); stable "
            "correlation key even if the device is renamed."
        ),
        "filter_logic": "exact",
    },
    {
        "name": "platformone_interface_id",
        "label": "Platform ONE Interface ID",
        "type": "text",
        "object_types": ["dcim.interface"],
        "description": (
            "Immutable Extreme Platform ONE interface UUID (ConfigState asset_interface_id); "
            "stable correlation key even if the port is renamed."
        ),
        "filter_logic": "exact",
    },
]

TAGS = [
    {
        "name": "extreme-networks",
        "slug": "extreme-networks",
        "color": "2196f3",
        "description": "Objects synced from Extreme Networks via netbox-orb-extreme-platformone.",
    },
    {
        "name": "platform-one",
        "slug": "platform-one",
        "color": "2196f3",
        "description": "Objects synced from Extreme Platform ONE via netbox-orb-extreme-platformone.",
    },
    {
        "name": "discovered",
        "slug": "discovered",
        "color": "9e9e9e",
        "description": "Objects created by automated discovery rather than manually.",
    },
]


def _headers(token: str) -> dict:
    return {"Authorization": f"Token {token}", "Content-Type": "application/json"}


def _exists(url: str, token: str, name: str) -> bool:
    resp = requests.get(url, headers=_headers(token), params={"name": name}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("count", 0) > 0


def _ensure_all(url: str, token: str, definitions: list[dict]) -> None:
    for definition in definitions:
        if not _exists(url, token, definition["name"]):
            resp = requests.post(url, headers=_headers(token), json=definition, timeout=30)
            resp.raise_for_status()


def ensure_schema(netbox_url: str | None, netbox_token: str | None) -> None:
    """Idempotently create the custom-field definitions and provenance tags."""
    if not netbox_url or not netbox_token:
        return
    base = netbox_url.rstrip("/")
    _ensure_all(f"{base}/api/extras/custom-fields/", netbox_token, CUSTOM_FIELDS)
    _ensure_all(f"{base}/api/extras/tags/", netbox_token, TAGS)
