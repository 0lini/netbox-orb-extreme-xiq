"""One-time idempotent NetBox schema setup: custom fields + provenance tags.

Uses the NetBox REST API directly (not Diode) because field definitions are
schema, not data. Skips gracefully if no NetBox credentials are configured.
"""

from __future__ import annotations

import requests

# One shared one-to-one Platform ONE correlation key with `unique` enforced
# (NetBox >= 3.7): two NetBox objects claiming the same Platform ONE id is
# always a sync defect worth failing loudly on. The value spaces are disjoint
# (Assets device ids are numeric, interface/cluster ids are UUIDs), so shared
# uniqueness cannot cross-collide. The ConfigState AssetDevice UUID is
# deliberately NOT stored in NetBox: the worker re-correlates by serial every
# tick, so it stays an internal join key.
CUSTOM_FIELDS = [
    {
        "name": "platformone_id",
        "label": "Platform ONE ID",
        "type": "text",
        "object_types": ["dcim.device", "dcim.interface", "dcim.virtualchassis"],
        "description": (
            "Immutable Extreme Platform ONE id: Assets device_id on devices, "
            "ConfigState asset_interface_id on interfaces, InferredCluster UUID "
            "on virtual chassis. Stable correlation key across renames."
        ),
        "filter_logic": "exact",
        "unique": True,
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


def _lookup(url: str, token: str, name: str) -> dict | None:
    resp = requests.get(url, headers=_headers(token), params={"name": name}, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    return results[0] if results else None


def _ensure_all(url: str, token: str, definitions: list[dict]) -> None:
    """Create missing definitions; align `unique` on existing ones.

    Only `unique` is reconciled on existing records: it is the one flag with
    enforcement semantics, and pre-uniqueness bootstraps must pick it up.
    Everything else (label, description, ...) is left to manual edits.
    """
    for definition in definitions:
        existing = _lookup(url, token, definition["name"])
        if existing is None:
            resp = requests.post(url, headers=_headers(token), json=definition, timeout=30)
            resp.raise_for_status()
            continue
        desired_unique = definition.get("unique")
        if desired_unique is not None and existing.get("unique") != desired_unique:
            resp = requests.patch(
                f"{url}{existing['id']}/",
                headers=_headers(token),
                json={"unique": desired_unique},
                timeout=30,
            )
            resp.raise_for_status()


def ensure_schema(netbox_url: str | None, netbox_token: str | None) -> None:
    """Idempotently create the custom-field definitions and provenance tags."""
    if not netbox_url or not netbox_token:
        return
    base = netbox_url.rstrip("/")
    _ensure_all(f"{base}/api/extras/custom-fields/", netbox_token, CUSTOM_FIELDS)
    _ensure_all(f"{base}/api/extras/tags/", netbox_token, TAGS)
