"""Concurrent ConfigState retrieve helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

from orb_extreme_platformone.client import PlatformOneApiError, PlatformOneClient

logger = logging.getLogger("orb_extreme_platformone.extract")

# Catalog: transform key -> (retrieve-* table, GetRequest filter field).
TableCatalog = dict[str, tuple[str, str]]
RowDeviceIdFn = Callable[[dict], str]


def _default_row_device_id(row: dict) -> str:
    """Prefer asset_device_id; fall back to device_id (vlan/PoE tables)."""
    return str(row.get("asset_device_id") or row.get("device_id") or "")


def retrieve_parallel(
    client: PlatformOneClient, jobs: list[tuple[str, dict]]
) -> list[tuple[str, list[dict] | None, PlatformOneApiError | None]]:
    """Run independent ConfigState retrieves concurrently.

    Returns one result per job in submission order (deterministic merge /
    failure lists). A failed job yields ``(table, None, exc)`` and does not
    abort siblings.
    """
    if not jobs:
        return []

    def _one(table: str, filters: dict) -> tuple[str, list[dict] | None, PlatformOneApiError | None]:
        try:
            return table, list(client.retrieve(table, filters)), None
        except PlatformOneApiError as exc:
            return table, None, exc

    workers = min(len(jobs), 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, table, filters) for table, filters in jobs]
        # result() in submit order: work still overlaps; merge stays deterministic.
        return [fut.result() for fut in futures]


def retrieve_ok(
    client: PlatformOneClient,
    jobs: list[tuple[str, dict]],
    contexts: list,
    *,
    policy_name: str,
    failed_tables: list[str],
    degradation: str,
) -> Iterator[tuple]:
    """Run jobs concurrently and yield ``(context, rows)`` for the successes.

    ``contexts`` pairs one caller-side value (a table key, per-job metadata,
    ...) with each job. A failed job is logged with ``degradation`` (what the
    tick loses), recorded in ``failed_tables``, and skipped, so callers only
    handle good rows.
    """
    for context, (table, rows, exc) in zip(contexts, retrieve_parallel(client, jobs), strict=True):
        if exc is not None:
            failed_tables.append(table)
            logger.warning(
                "Policy %s: ConfigState %s fetch failed, %s: %s",
                policy_name,
                table,
                degradation,
                exc,
            )
            continue
        assert rows is not None
        yield context, rows


def extract_device_table_buckets(
    client: PlatformOneClient,
    device_ids: list[str],
    catalog: TableCatalog,
    *,
    policy_name: str,
    degradation: str,
    row_device_id: RowDeviceIdFn = _default_row_device_id,
    failed_tables: list[str] | None = None,
) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    """Batched device-filtered retrieves, bucketed by device UUID.

    Returns ``(tables_by_device, failed_tables)``. Each device gets an empty
    list per catalog key; successful rows append into the matching bucket.
    """
    failures = failed_tables if failed_tables is not None else []
    tables_by_device: dict[str, dict[str, list[dict]]] = {
        device_id: {key: [] for key in catalog} for device_id in device_ids
    }
    if not device_ids or not catalog:
        return tables_by_device, failures

    jobs = [(table, {filter_field: device_ids}) for table, filter_field in catalog.values()]
    for key, rows in retrieve_ok(
        client,
        jobs,
        list(catalog),
        policy_name=policy_name,
        failed_tables=failures,
        degradation=degradation,
    ):
        for row in rows:
            device_id = row_device_id(row)
            if device_id in tables_by_device:
                tables_by_device[device_id][key].append(row)
    return tables_by_device, failures
