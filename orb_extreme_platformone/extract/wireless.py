"""Batched ConfigState wireless-interface / SSID fetches for APs."""

from __future__ import annotations

from orb_extreme_platformone.client import PlatformOneClient

from .retrieve import retrieve_ok
from .tables import WIRELESS_TABLES


def extract_wireless_tables(
    client: PlatformOneClient,
    device_ids: list[str],
    policy_name: str,
) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    """Batched wireless + SSID retrieves for the given AssetDevice UUIDs.

    Returns ``(tables_by_device, failed_tables)``. Independent tables fetch
    concurrently. A failed table is recorded in ``failed_tables`` and omitted
    from the returned buckets.
    """
    failed_tables: list[str] = []
    tables_by_device: dict[str, dict[str, list[dict]]] = {
        device_id: {key: [] for key in WIRELESS_TABLES} for device_id in device_ids
    }
    jobs = [(table, {filter_field: device_ids}) for table, filter_field in WIRELESS_TABLES.values()]
    for key, rows in retrieve_ok(
        client,
        jobs,
        list(WIRELESS_TABLES),
        policy_name=policy_name,
        failed_tables=failed_tables,
        degradation="wireless sync without it",
    ):
        for row in rows:
            device_id = str(row.get("asset_device_id") or "")
            if device_id in tables_by_device:
                tables_by_device[device_id][key].append(row)
    return tables_by_device, failed_tables
