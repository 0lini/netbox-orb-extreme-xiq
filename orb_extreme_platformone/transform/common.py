"""Shared transform constants and small helpers."""

from __future__ import annotations

import logging

from netboxlabs.diode.sdk.ingester import CustomFieldValue

from orb_extreme_platformone import bootstrap

logger = logging.getLogger("orb_extreme_platformone.transform")

MANUFACTURER = "Extreme Networks"

PROVENANCE_TAGS = [tag["name"] for tag in bootstrap.TAGS]


def _cf_text(value: str) -> CustomFieldValue:
    return CustomFieldValue(text=value)


def _interface_custom_fields(*, interface_id: str | None = None) -> dict:
    """Build interface custom fields (ConfigState asset_interface_id)."""
    if not interface_id:
        return {}
    return {"platformone_interface_id": _cf_text(str(interface_id))}
