"""Shared transform constants and small helpers."""

from __future__ import annotations

import ipaddress
import logging

from netboxlabs.diode.sdk.ingester import CustomFieldValue

from orb_extreme_platformone import bootstrap

logger = logging.getLogger("orb_extreme_platformone.transform")

MANUFACTURER = "Extreme Networks"

PROVENANCE_TAGS = [tag["name"] for tag in bootstrap.TAGS]

CF_DEVICE_ID = bootstrap.CF_DEVICE_ID
CF_INTERFACE_ID = bootstrap.CF_INTERFACE_ID
CF_CLUSTER_ID = bootstrap.CF_CLUSTER_ID


def _cf_text(value: str) -> CustomFieldValue:
    return CustomFieldValue(text=value)


def _interface_custom_fields(*, interface_id: str | None = None) -> dict:
    """Build interface custom fields (ConfigState asset_interface_id)."""
    if not interface_id:
        return {}
    return {CF_INTERFACE_ID: _cf_text(str(interface_id))}


def _interface_identity_kwargs(
    *,
    device: str,
    name: str,
    interface_id: str | None = None,
    enabled=None,
) -> dict:
    """Shared device/name/tags/custom_fields/enabled base for Interface entities."""
    kwargs: dict = {
        "device": device,
        "name": name,
        "tags": PROVENANCE_TAGS,
    }
    custom_fields = _interface_custom_fields(interface_id=interface_id)
    if custom_fields:
        kwargs["custom_fields"] = custom_fields
    if isinstance(enabled, bool):
        kwargs["enabled"] = enabled
    return kwargs


def _normalized_mac(value) -> str | None:
    if not value:
        return None
    return str(value).upper()


def _compact_token(value: str, drop: str = " _-") -> str:
    """Casefold and strip separator characters for fuzzy token matching."""
    text = str(value).casefold()
    for ch in drop:
        text = text.replace(ch, "")
    return text


def _coerce_int(value) -> int | None:
    """Accept JSON ints or digit-only strings; reject floats/bools/garbage."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _explicit_cidr(raw, mask_length=None) -> str | None:
    """Parse only explicitly-prefixed addresses; never invent /32 or /128.

    Accepts an inline ``/n`` in ``raw``, or a bare host plus usable
    ``mask_length``. Invalid values return None.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if "/" not in text:
        mask = _coerce_int(mask_length)
        if mask is None or not 0 <= mask <= 128:
            return None
        text = f"{text}/{mask}"
    try:
        return str(ipaddress.ip_interface(text))
    except ValueError:
        return None
