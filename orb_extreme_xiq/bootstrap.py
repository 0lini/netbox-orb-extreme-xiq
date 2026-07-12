"""One-time idempotent NetBox schema setup: custom fields + provenance tags.

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
        "name": "xiq_network_policy",
        "label": "XIQ Network Policy",
        "type": "text",
        "object_types": ["dcim.device"],
        "description": "The ExtremeCloud IQ network policy assigned to this device.",
    },
    {
        "name": "xiq_port_id",
        "label": "XIQ Port ID",
        "type": "text",
        "object_types": ["dcim.interface"],
        "description": (
            "Immutable XIQ port ID (cloud-global, not per-device); stable correlation "
            "key even if the port is renamed."
        ),
        "filter_logic": "exact",
    },
]

# Vendor/product/lifecycle tags, matching the pattern NetBox Labs' own Cisco
# Meraki integration uses (separate flat tags -- e.g. "cisco", "meraki",
# "discovered" -- rather than one namespaced tag).
TAGS = [
    {
        "name": "extreme-networks",
        "slug": "extreme-networks",
        "color": "2196f3",
        "description": "Objects synced from Extreme Networks via orb-extreme-xiq.",
    },
    {
        "name": "xiq",
        "slug": "xiq",
        "color": "2196f3",
        "description": "Objects synced from ExtremeCloud IQ via orb-extreme-xiq.",
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


def ensure_schema(netbox_url: str | None, netbox_token: str | None) -> None:
    """Idempotently create the custom-field definitions and provenance tags."""
    if not netbox_url or not netbox_token:
        return
    base = netbox_url.rstrip("/")
    custom_fields_url = f"{base}/api/extras/custom-fields/"
    tags_url = f"{base}/api/extras/tags/"

    for custom_field in CUSTOM_FIELDS:
        if not _exists(custom_fields_url, netbox_token, custom_field["name"]):
            resp = requests.post(
                custom_fields_url, headers=_headers(netbox_token), json=custom_field, timeout=30
            )
            resp.raise_for_status()

    for tag in TAGS:
        if not _exists(tags_url, netbox_token, tag["name"]):
            resp = requests.post(tags_url, headers=_headers(netbox_token), json=tag, timeout=30)
            resp.raise_for_status()
