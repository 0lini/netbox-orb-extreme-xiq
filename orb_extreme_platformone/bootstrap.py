"""One-time idempotent NetBox schema setup: custom fields + provenance tags.

Uses the NetBox REST API directly (not Diode) because field definitions are
schema, not data. Skips gracefully if no NetBox credentials are configured.
"""

from __future__ import annotations

import requests

from .urls import require_https_url

# Per-object-type Platform ONE correlation keys with `unique` enforced
# (NetBox >= 3.7): two NetBox objects of the same type claiming the same
# Platform ONE id is always a sync defect worth failing loudly on. The
# ConfigState AssetDevice UUID stays an internal join key (re-correlated by
# serial every tick) and is not stored on Device.
CF_DEVICE_ID = "platformone_device_id"
CF_INTERFACE_ID = "platformone_interface_id"
CF_CLUSTER_ID = "platformone_cluster_id"

CUSTOM_FIELDS = [
    {
        "name": CF_DEVICE_ID,
        "label": "Platform ONE Device ID",
        "type": "text",
        "object_types": ["dcim.device"],
        "description": (
            "Immutable Extreme Platform ONE device id (Assets API device_id); "
            "stable correlation key even if the device is renamed."
        ),
        "filter_logic": "exact",
        "unique": True,
    },
    {
        "name": CF_INTERFACE_ID,
        "label": "Platform ONE Interface ID",
        "type": "text",
        "object_types": ["dcim.interface"],
        "description": (
            "Immutable Extreme Platform ONE interface UUID "
            "(ConfigState asset_interface_id); stable correlation key even if "
            "the port is renamed."
        ),
        "filter_logic": "exact",
        "unique": True,
    },
    {
        "name": CF_CLUSTER_ID,
        "label": "Platform ONE Cluster ID",
        "type": "text",
        "object_types": ["dcim.virtualchassis"],
        "description": (
            "Immutable Extreme Platform ONE InferredCluster UUID "
            "(ConfigState retrieve-inferred-cluster id); stable correlation "
            "key even if peer names change."
        ),
        "filter_logic": "exact",
        "unique": True,
    },
]

TAGS = [
    {
        "name": "extreme-networks",
        "slug": "extreme-networks",
        # Extreme Networks brand primary purple (#440099).
        "color": "440099",
        "description": "Objects synced from Extreme Networks via netbox-orb-extreme-platformone.",
    },
    {
        "name": "platform-one",
        "slug": "platform-one",
        # Same Extreme brand purple as extreme-networks (#440099).
        "color": "440099",
        "description": "Objects synced from Extreme Platform ONE via netbox-orb-extreme-platformone.",
    },
    {
        "name": "discovered",
        "slug": "discovered",
        # Neutral gray — provenance marker, not brand-colored.
        "color": "9e9e9e",
        "description": "Objects created by automated discovery rather than manually.",
    },
]


def _headers(token: str) -> dict:
    return {"Authorization": f"Token {token}", "Content-Type": "application/json"}


def _request(method: str, url: str, token: str, **kwargs):
    """NetBox REST call that never follows redirects (token must not leave origin)."""
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("allow_redirects", False)
    resp = requests.request(method, url, headers=_headers(token), **kwargs)
    if 300 <= resp.status_code < 400:
        raise requests.HTTPError(
            f"NetBox unexpected redirect {resp.status_code} for {url}",
            response=resp,
        )
    resp.raise_for_status()
    return resp


def _lookup(url: str, token: str, name: str) -> dict | None:
    resp = _request("GET", url, token, params={"name": name})
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
            _request("POST", url, token, json=definition)
            continue
        desired_unique = definition.get("unique")
        if desired_unique is not None and existing.get("unique") != desired_unique:
            _request(
                "PATCH",
                f"{url}{existing['id']}/",
                token,
                json={"unique": desired_unique},
            )


def ensure_schema(netbox_url: str | None, netbox_token: str | None) -> None:
    """Idempotently create the custom-field definitions and provenance tags.

    When either URL or token is missing the call is a no-op so scheduled
    runs without bootstrap credentials stay quiet. Callers that set
    ``BOOTSTRAP: true`` should fail closed before invoking this (see
    ``backend.Backend.run``).
    """
    if not netbox_url or not netbox_token:
        return
    base = require_https_url(netbox_url, what="NETBOX_API_URL")
    _ensure_all(f"{base}/api/extras/custom-fields/", netbox_token, CUSTOM_FIELDS)
    _ensure_all(f"{base}/api/extras/tags/", netbox_token, TAGS)
