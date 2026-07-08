"""One-time idempotent NetBox schema setup: custom fields + the source tag.

Uses the NetBox REST API directly (not Diode) because field *definitions*
are schema, not data, and this path works regardless of Diode SDK version.
Skips gracefully if no NetBox credentials are configured -- BOOTSTRAP is
meant to run once with NETBOX_API_URL/NETBOX_API_TOKEN set, then be turned
off for scheduled runs.
"""

from __future__ import annotations

import requests

CUSTOM_FIELDS = [
    {
        "name": "xiq_device_id",
        "label": "XIQ Device ID",
        "type": "text",
        "object_types": ["dcim.device"],
        "description": "Immutable XIQ device ID; stable correlation key even after a rename.",
        "filter_logic": "exact",
    },
    {
        "name": "xiq_network_policy",
        "label": "XIQ Network Policy",
        "type": "text",
        "object_types": ["dcim.device"],
        "description": "The ExtremeCloud IQ network policy assigned to this device.",
    },
    {
        "name": "xiq_locations",
        "label": "XIQ Locations",
        "type": "json",
        "object_types": ["dcim.site"],
        "description": "The XIQ root locations consolidated into this NetBox site.",
    },
]

SOURCE_TAG = {
    "name": "source:xiq",
    "slug": "source-xiq",
    "color": "2196f3",
    "description": "Objects synced from ExtremeCloud IQ via orb-extreme-xiq.",
}


def _headers(token: str) -> dict:
    return {"Authorization": f"Token {token}", "Content-Type": "application/json"}


def _exists(base_url: str, token: str, path: str, name: str) -> bool:
    resp = requests.get(f"{base_url}{path}", headers=_headers(token), params={"name": name}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("count", 0) > 0


def ensure_schema(netbox_url: str | None, netbox_token: str | None) -> None:
    """Idempotently create the custom-field definitions and source:xiq tag."""
    if not netbox_url or not netbox_token:
        return
    base = netbox_url.rstrip("/")
    custom_fields_url = f"{base}/api/extras/custom-fields/"
    tags_url = f"{base}/api/extras/tags/"
    for custom_field in CUSTOM_FIELDS:
        if not _exists(base, netbox_token, "/api/extras/custom-fields/", custom_field["name"]):
            resp = requests.post(
                custom_fields_url, headers=_headers(netbox_token), json=custom_field, timeout=30
            )
            resp.raise_for_status()
    if not _exists(base, netbox_token, "/api/extras/tags/", SOURCE_TAG["name"]):
        resp = requests.post(tags_url, headers=_headers(netbox_token), json=SOURCE_TAG, timeout=30)
        resp.raise_for_status()
