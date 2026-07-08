"""Thin ExtremeCloud IQ REST client (token or user/pass auth).

Covers the two read paths this worker needs against the XIQ OpenAPI spec
(https://github.com/extremenetworks/ExtremeCloudIQ-OpenAPI):
  - GET /devices        (paginated XiqPage envelope: page/total_pages)
  - GET /locations/tree (nested ClsLocation tree: id/name/children)
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import requests

DEFAULT_BASE_URL = "https://api.extremecloudiq.com"
PAGE_LIMIT = 100  # XIQ's documented max for the `limit` query param


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
        self._api_token = api_token
        self._username = username
        self._password = password
        self._timeout = timeout
        self._session = requests.Session()
        # None = static API token (never expires from our side) until a login
        # response sets it; set once we've logged in with username/password.
        self._token_expiry: float | None = None if api_token else 0.0

    def _ensure_token(self) -> None:
        if self._api_token is not None and self._token_expiry is None:
            return
        if self._token_expiry is not None and time.time() < self._token_expiry:
            return
        if self._username and self._password:
            self._login()
        elif self._api_token is None:
            raise XiqApiError("No credentials available to authenticate with XIQ")

    def _login(self) -> None:
        resp = self._session.post(
            f"{self._base_url}/login",
            json={"username": self._username, "password": self._password},
            timeout=self._timeout,
        )
        self._raise_for_status(resp)
        payload = resp.json()
        self._api_token = payload["access_token"]
        # Refresh a minute early so a request never races token expiry.
        self._token_expiry = time.time() + payload.get("expires_in", 86400) - 60

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code >= 400:
            raise XiqApiError(f"XIQ API error {resp.status_code}: {resp.text}")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_token}", "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        self._ensure_token()
        resp = self._session.get(
            f"{self._base_url}{path}", headers=self._headers(), params=params, timeout=self._timeout
        )
        if resp.status_code == 401 and self._username and self._password:
            # Token may have been revoked/expired out from under us; re-login once.
            self._token_expiry = 0.0
            self._login()
            resp = self._session.get(
                f"{self._base_url}{path}", headers=self._headers(), params=params, timeout=self._timeout
            )
        self._raise_for_status(resp)
        return resp.json()

    def get_devices(
        self, *, location_ids: list[int] | None = None, limit: int = PAGE_LIMIT
    ) -> Iterator[dict]:
        """Yield every device visible to this account/org, across all pages.

        Requests the FULL view so location_id, network_policy_name and the
        other fields the mapper needs are present (the default BASIC view
        omits them).
        """
        page = 1
        while True:
            params: dict = {"page": page, "limit": limit, "views": ["FULL"]}
            if location_ids:
                params["locationIds"] = location_ids
            payload = self._get("/devices", params=params)
            yield from payload.get("data", [])
            total_pages = payload.get("total_pages", page)
            if page >= total_pages:
                break
            page += 1

    def get_location_tree(self, *, parent_id: int | None = None, expand_children: bool = True) -> list[dict]:
        """Return the XIQ location hierarchy as nested {id, name, children} dicts."""
        params: dict = {"expandChildren": expand_children}
        if parent_id is not None:
            params["parentId"] = parent_id
        return self._get("/locations/tree", params=params)
