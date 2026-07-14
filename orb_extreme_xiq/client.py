"""Thin ExtremeCloud IQ client (token or user/pass auth), on plain `requests`.

Covers the read paths this worker needs:
  - GET /devices
  - GET /locations/tree
  - GET /devices/radio-information
  - GET /xiq/v0/monitor/device/wired/portlist (per-switch wired port telemetry,
    an older, undocumented endpoint that only exists on a different host
    (LEGACY_BASE_URL) -- confirmed by probing: newer API versions (v2+) 404
    there, and v1 exists as a namespace but returns a bare-backend 404 for
    this specific path, so v0 is not a deprecated predecessor of a current
    version, just the only version that exists. It takes the same bearer
    token as everything else.)
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import requests

DEFAULT_BASE_URL = "https://api.extremecloudiq.com"
LEGACY_BASE_URL = "https://cloudapi.extremecloudiq.com"
PAGE_LIMIT = 100  # XIQ's documented max for the `limit` query param on /devices
RADIO_PAGE_LIMIT = 50  # XIQ's documented max for /devices/radio-information (lower than /devices)


class XiqApiError(RuntimeError):
    """Raised on a non-2xx response from the XIQ API."""


class XiqClient:
    """Minimal client for the XIQ endpoints this worker consumes."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30,
    ) -> None:
        if not api_token and not (username and password):
            raise ValueError("XiqClient requires api_token or username/password")
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._session = requests.Session()
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_token:
            self._headers["Authorization"] = f"Bearer {api_token}"

        # None = static API token (never expires from our side) until a login
        # response sets it; set once we've logged in with username/password.
        self._token_expiry: float | None = None if api_token else 0.0

    def _ensure_token(self) -> None:
        if self._token_expiry is None:
            return
        if time.time() < self._token_expiry:
            return
        if self._username and self._password:
            self._login()
        elif "Authorization" not in self._headers:
            raise XiqApiError("No credentials available to authenticate with XIQ")

    def _login(self) -> None:
        url = f"{self._base_url}/login"
        payload = {"username": self._username, "password": self._password}
        resp = self._session.post(url, headers=self._headers, json=payload, timeout=self._timeout)
        if resp.status_code != 200:
            raise XiqApiError(f"XIQ login failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        if "access_token" not in data:
            raise XiqApiError("XIQ login response did not contain an access_token")
        self._headers["Authorization"] = f"Bearer {data['access_token']}"
        # Refresh a minute early so a request never races token expiry.
        self._token_expiry = time.time() + data.get("expires_in", 86400) - 60

    def _get(self, base_url: str, path: str, params: dict) -> dict:
        """GET `path` off `base_url`, re-logging in once on a 401 before giving up."""
        self._ensure_token()
        url = f"{base_url}{path}"
        resp = self._session.get(url, headers=self._headers, params=params, timeout=self._timeout)
        if resp.status_code == 401 and self._username and self._password:
            self._token_expiry = 0.0
            self._login()
            resp = self._session.get(url, headers=self._headers, params=params, timeout=self._timeout)
        if resp.status_code >= 400:
            raise XiqApiError(f"XIQ API error {resp.status_code}: {resp.text}")
        return resp.json()

    def _paginate(self, path: str, params: dict) -> Iterator[dict]:
        """Yield every item across all pages of a paginated list endpoint.
        `params` must already include the first "page" and "limit"."""
        page = params["page"]
        while True:
            payload = self._get(self._base_url, path, params)
            yield from payload.get("data", [])
            total_pages = payload.get("total_pages", page)
            if page >= total_pages:
                break
            page += 1
            params = {**params, "page": page}

    def get_devices(
        self, *, location_ids: list[int] | None = None, limit: int = PAGE_LIMIT
    ) -> Iterator[dict]:
        """Yield every device visible to this account/org, across all pages.

        Requests the FULL view so location_id, network_policy_name and the
        other fields the mapper needs are present (the default BASIC view
        omits them).
        """
        params: dict = {"page": 1, "limit": limit, "views": ["FULL"]}
        if location_ids:
            params["locationIds"] = location_ids
        yield from self._paginate("/devices", params)

    def get_location_tree(self, *, parent_id: int | None = None) -> list[dict]:
        """Return the XIQ location hierarchy as nested {id, name, children} dicts.

        `expandChildren` is left unset -- True (nested children) is XIQ's own
        server-side default, which is what this worker needs anyway.
        """
        params: dict = {}
        if parent_id is not None:
            params["parentId"] = parent_id
        return self._get(self._base_url, "/locations/tree", params)

    def get_radio_information(
        self, *, device_ids: list[int], limit: int = RADIO_PAGE_LIMIT
    ) -> Iterator[dict]:
        """Yield one {device_id, radios: [...]} record per AP in device_ids, across all pages.

        `includeDisabledRadio` is left unset -- False (enabled radios only) is
        XIQ's own server-side default, which is what this worker wants anyway.
        """
        params: dict = {"page": 1, "limit": limit, "deviceIds": device_ids}
        yield from self._paginate("/devices/radio-information", params)

    def get_wired_portlist(self, device_id: int) -> list[dict]:
        """Return the wired port list for one switch device (see module docstring)."""
        payload = self._get(LEGACY_BASE_URL, "/xiq/v0/monitor/device/wired/portlist", {"deviceId": device_id})
        return payload.get("data", {}).get("portList", [])
