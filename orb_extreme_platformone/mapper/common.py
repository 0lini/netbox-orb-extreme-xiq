"""Shared mapper constants and small helpers."""

from __future__ import annotations

import logging

from netboxlabs.diode.sdk.ingester import CustomFieldValue

from orb_extreme_platformone import bootstrap

logger = logging.getLogger("orb_extreme_platformone.mapper")

MANUFACTURER = "Extreme Networks"

PROVENANCE_TAGS = [tag["name"] for tag in bootstrap.TAGS]


def _cf_text(value: str) -> CustomFieldValue:
    return CustomFieldValue(text=value)
