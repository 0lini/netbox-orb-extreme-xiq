"""Assets ↔ ConfigState device correlation and location attach."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from orb_extreme_platformone.client import PlatformOneApiError, PlatformOneClient

logger = logging.getLogger("orb_extreme_platformone.fetch")


def fetch_cs_devices(client: PlatformOneClient, assets: list[dict]) -> list[dict]:
    """Fetch the ConfigState AssetDevice records for the given Assets devices.

    ConfigState rejects an empty GetRequest body (code 1727: at least one
    filter attribute is required), so the listing is filtered by the Assets
    serial numbers — the shared primary key between the two APIs.
    """
    serials = sorted({str(a["serial_number"]) for a in assets if a.get("serial_number")})
    if not serials:
        return []
    return list(client.retrieve("asset-device", {"serial_number": serials}))


def index_unique(items: Iterable[dict], key_fn, *, label: str) -> dict:
    """Build {key: item}, keeping the first on collision and warning."""
    index: dict = {}
    for item in items:
        key = key_fn(item)
        if not key:
            continue
        if key in index:
            logger.warning(
                "Duplicate ConfigState %s %r; keeping the first match",
                label,
                key,
            )
            continue
        index[key] = item
    return index


def correlate(assets: list[dict], cs_devices: list[dict]) -> dict[int, dict]:
    """Match Assets devices to ConfigState AssetDevice records by serial number.

    Returns {Assets device_id: ConfigState device record}. Serial number is
    the shared primary key between the two APIs — every physical Extreme
    device carries one, so there is deliberately no MAC/IP fallback. Devices
    with no match have no ConfigState data yet and still sync as Devices,
    minus ports and building/floor detail.
    """
    by_serial = index_unique(
        cs_devices,
        lambda d: str(d["serial_number"]).casefold() if d.get("serial_number") else None,
        label="AssetDevice serial_number",
    )

    matched: dict[int, dict] = {}
    for asset in assets:
        serial = str(asset.get("serial_number") or "").casefold()
        cs = by_serial.get(serial) if serial else None
        if cs is not None and asset.get("device_id") is not None:
            matched[asset["device_id"]] = cs
    return matched


def correlated_records(client: PlatformOneClient, assets: list[dict], policy_name: str) -> list[dict]:
    """Join each Assets device with its ConfigState identity + location.

    A ConfigState outage degrades to Assets-only data (flat site, no
    ports) instead of failing the sync: Diode ingestion is upsert-style,
    so a tick without building/floor/port detail is harmless.
    """
    try:
        cs_devices = fetch_cs_devices(client, assets)
    except PlatformOneApiError as exc:
        logger.warning(
            "Policy %s: ConfigState device listing failed, syncing without location/port detail: %s",
            policy_name,
            exc,
        )
        cs_devices = []
    cs_by_asset_id = correlate(assets, cs_devices)

    locations: dict[str, dict] = {}
    cs_uuids = sorted({str(cs["id"]) for cs in cs_by_asset_id.values() if cs.get("id")})
    if cs_uuids:
        try:
            locations = index_unique(
                client.retrieve("asset-location", {"asset_device_id": cs_uuids}),
                lambda loc: str(loc["asset_device_id"]) if loc.get("asset_device_id") else None,
                label="asset-location asset_device_id",
            )
        except PlatformOneApiError as exc:
            logger.warning(
                "Policy %s: ConfigState location fetch failed, falling back to Assets site names: %s",
                policy_name,
                exc,
            )

    records = []
    for asset in assets:
        cs = cs_by_asset_id.get(asset.get("device_id"))
        cs_device_id = str(cs["id"]) if cs and cs.get("id") else None
        records.append(
            {
                "asset": asset,
                "cs_device_id": cs_device_id,
                "cs_device": cs,
                "location": locations.get(cs_device_id) if cs_device_id else None,
            }
        )
    return records
