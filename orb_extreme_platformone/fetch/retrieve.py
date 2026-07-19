"""Concurrent ConfigState retrieve helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

from orb_extreme_platformone.client import PlatformOneApiError, PlatformOneClient

logger = logging.getLogger("orb_extreme_platformone.fetch")


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
