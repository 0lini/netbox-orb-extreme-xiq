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


def _interface_custom_fields(*, interface_id: str | None = None, serial: str | None = None) -> dict:
    """Build Meraki-style interface CFs: product id + device serial."""
    custom_fields: dict = {}
    if interface_id:
        custom_fields["platformone_interface_id"] = _cf_text(str(interface_id))
    if serial:
        custom_fields["platformone_serial"] = _cf_text(str(serial))
    return custom_fields
