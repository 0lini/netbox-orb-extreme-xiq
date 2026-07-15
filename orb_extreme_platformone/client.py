"""Thin Extreme Platform ONE client (bearer-token auth), on plain `requests`.

Covers the two Platform ONE API families this worker consumes, both served
from the same host and taking the same API token:

  - Assets API   (POST /assets/v1/devices) -- device inventory. Contract per
    the "Extreme Platform ONE REST API - Asset Management" OpenAPI spec
    (v25.11.0): `page`/`limit` query params (limit max 500), a
    `ListDevicesRequestFilter` JSON body (e.g. {"classification": "SWITCH"}),
    and a `PagedDevice` response with top-level `data` + `total_pages`.
  - ConfigState API (POST /configstate/v1/retrieve-*) -- per-device switch
    configuration/state tables. Contract per the "Config State Service."
    OpenAPI spec (v1.0.0): `page_number`/`page_size` query params, a
    per-table GetRequest body whose filter fields are all *lists* (so one
    call can cover many devices), and a response keyed by the table's
    schema name (e.g. "AssetPortState") plus a `Pagination` object.
"""

from __future__ import annotations

from collections.abc import Iterator

import requests

DEFAULT_BASE_URL = "https://cloudapi.extremecloudiq.com"
ASSETS_PAGE_LIMIT = 500  # documented max for the Assets `limit` query param
CONFIGSTATE_PAGE_SIZE = 500


class PlatformOneApiError(RuntimeError):
    """Raised on a non-2xx response from a Platform ONE API."""


def configstate_response_key(table: str) -> str:
    """Derive a ConfigState response key from its table name.

    Every retrieve-<table> endpoint wraps its records under the table's
    PascalCase schema name: retrieve-asset-port-state -> "AssetPortState",
    retrieve-asset-l2-vsn-suni-config -> "AssetL2VsnSuniConfig". Verified
    against every path/response pair in the ConfigState OpenAPI spec.
    """
    return "".join(part.capitalize() for part in table.split("-"))


class PlatformOneClient:
    """Minimal client for the Platform ONE endpoints this worker consumes."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_token: str | None = None,
        timeout: float = 60,
    ) -> None:
        if not api_token:
            raise ValueError("PlatformOneClient requires api_token")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }

    def _post(self, path: str, params: dict, body: dict) -> dict:
        url = f"{self._base_url}{path}"
        resp = self._session.post(url, headers=self._headers, params=params, json=body, timeout=self._timeout)
        if resp.status_code >= 400:
            raise PlatformOneApiError(f"Platform ONE API error {resp.status_code} for {path}: {resp.text}")
        return resp.json()

    def get_devices(
        self, *, classification: str = "SWITCH", limit: int = ASSETS_PAGE_LIMIT
    ) -> Iterator[dict]:
        """Yield every Assets-API device of `classification`, across all pages.

        `classification` is the documented ListDevicesRequestFilter enum
        (SWITCH, WIRELESS, ROUTER, ... or ALL); it is passed through verbatim
        so new upstream values need no client change.
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
        list, so batching many devices into one call is
        `retrieve("asset-port-state", {"asset_device_id": [id1, id2, ...]})`.
        An empty/None filter body returns the whole tenant-visible table.
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
