"""Batched ConfigState wireless-interface / SSID fetches for APs."""

from __future__ import annotations

from orb_extreme_platformone.client import PlatformOneClient

from .retrieve import extract_device_table_buckets
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
    return extract_device_table_buckets(
        client,
        device_ids,
        WIRELESS_TABLES,
        policy_name=policy_name,
        degradation="wireless sync without it",
    )
