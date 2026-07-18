"""Thin Extreme Platform ONE client (bearer-token auth) on plain `requests`.

Covers the two API families this worker consumes, both served from the same
host with the same token:

  - Assets API (POST /assets/v1/devices): `page`/`limit` query params, a
    filter JSON body, and a response with top-level `data` + `total_pages`.
  - ConfigState API (POST /configstate/v1/retrieve-*): `page_number`/
    `page_size` query params, a per-table GetRequest body whose filter
    fields all take lists, and a response keyed by the table's schema name
    plus a `Pagination` object.

Contracts verified against the Platform ONE OpenAPI specs; see
tests/test_openapi_contract.py.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import requests

from .urls import require_https_url

DEFAULT_BASE_URL = "https://cloudapi.extremecloudiq.com"
ASSETS_PAGE_LIMIT = 500  # documented max for the Assets `limit` query param
CONFIGSTATE_PAGE_SIZE = 500
# Keep API error text short so logs/exceptions do not retain full upstream
# bodies (which can include sensitive diagnostics).
_ERROR_BODY_LIMIT = 200


class PlatformOneApiError(RuntimeError):
    """Raised on a non-2xx response from a Platform ONE API."""


def truncate_error_body(text: str, *, limit: int = _ERROR_BODY_LIMIT) -> str:
    """Collapse whitespace and truncate an HTTP error body for safe logging."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 3:
        return cleaned[:limit]
    return cleaned[: limit - 3] + "..."


def configstate_response_key(table: str) -> str:
    """Derive a ConfigState response key from its table name.

    Every retrieve-<table> endpoint wraps its records under the table's
    PascalCase schema name: retrieve-asset-port-state -> "AssetPortState".
    """
    return "".join(part.capitalize() for part in table.split("-"))


class PlatformOneClient:
    """Minimal client for the Platform ONE endpoints this worker consumes.

    HTTP sessions are thread-local so independent ConfigState retrieves can
    run concurrently (see backend parallel table fetches) without sharing a
    `requests.Session` across threads.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_token: str | None = None,
        timeout: float = 60,
    ) -> None:
        if not api_token:
            raise ValueError("PlatformOneClient requires api_token")
        self._base_url = require_https_url(base_url, what="PLATFORMONE_API_URL")
        self._timeout = timeout
        self._local = threading.local()
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    def _post(self, path: str, params: dict, body: dict) -> dict:
        url = f"{self._base_url}{path}"
        resp = self._session().post(
            url, headers=self._headers, params=params, json=body, timeout=self._timeout
        )
        if resp.status_code >= 400:
            detail = truncate_error_body(resp.text)
            raise PlatformOneApiError(f"Platform ONE API error {resp.status_code} for {path}: {detail}")
        return resp.json()

    def get_devices(self, *, classification: str = "ALL", limit: int = ASSETS_PAGE_LIMIT) -> Iterator[dict]:
        """Yield every Assets-API device of `classification`, across all pages.

        `classification` (ALL, SWITCH, WIRELESS, ROUTER, ...) is passed
        through verbatim so new upstream values need no client change.
        """
        page = 1
        while True:
            payload = self._post(
                "/assets/v1/devices",
                {"page": page, "limit": limit},
                {"classification": classification},
            )
            yield from payload.get("data") or []
            total_pages = payload.get("total_pages") or page
            if page >= total_pages:
                break
            page += 1

    def retrieve(
        self, table: str, filters: dict | None = None, *, page_size: int = CONFIGSTATE_PAGE_SIZE
    ) -> Iterator[dict]:
        """Yield every ConfigState record of retrieve-`table`, across all pages.

        `filters` is the table's GetRequest body; every filter field takes a
        list, e.g. retrieve("asset-port-state", {"asset_device_id": [a, b]}).
        The API rejects an empty filter body (code 1727) -- always pass at
        least one filter attribute with a non-empty list.
        """
        response_key = configstate_response_key(table)
        page = 1
        while True:
            payload = self._post(
                f"/configstate/v1/retrieve-{table}",
                {"page_number": page, "page_size": page_size},
                filters or {},
            )
            yield from payload.get(response_key) or []
            pagination = payload.get("Pagination") or {}
            total_pages = pagination.get("total_pages") or page
            if page >= total_pages:
                break
            page += 1
