"""Thin ExtremeCloud IQ client (token or user/pass auth).

Covers the read paths this worker needs:
  - GET /devices                              (via the official `extremecloudiq-api` SDK)
  - GET /locations/tree                       (via the official `extremecloudiq-api` SDK)
  - GET /devices/radio-information             (via the official `extremecloudiq-api` SDK)
  - GET /xiq/v0/monitor/device/wired/portlist (per-switch wired port telemetry)

The first three are documented in the current XIQ OpenAPI spec and covered by
the official generated SDK (https://github.com/extremenetworks/ExtremeCloudIQ-SDK-Python,
PyPI: extremecloudiq-api). That SDK is generated in OpenAPI Generator's
"oapg" style: request/response bodies are schema-validated Schema objects
(query params as `frozendict`, response bodies as dict-like Schema
instances), which is a lot of ceremony for what we need. We call every
endpoint with `skip_deserialization=True` and parse `result.response.data`
as plain JSON ourselves instead -- the SDK still owns URL building, query
param serialization, the Bearer auth header and status-code-based
ApiException raising, we just skip its schema deserialization layer.

The port-list call is an older, undocumented endpoint that only exists on a
different host (LEGACY_BASE_URL) and isn't in the SDK at all -- confirmed by
probing: newer API versions (v2+) 404 there, and v1 exists as a namespace
but returns a bare-backend 404 for this specific path, so v0 is not a
deprecated predecessor of a current version, just the only version that
exists. It takes the same bearer token as the SDK calls, so we pull the
token out of the SDK's Configuration and send it with a plain `requests` call.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import frozendict
import requests
from extremecloudiq.api_client import ApiClient
from extremecloudiq.apis.tags.authentication_api import AuthenticationApi
from extremecloudiq.apis.tags.device_api import DeviceApi
from extremecloudiq.apis.tags.location_api import LocationApi
from extremecloudiq.configuration import Configuration
from extremecloudiq.exceptions import ApiException

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
        self._username = username
        self._password = password
        self._timeout = timeout

        self._configuration = Configuration(host=base_url.rstrip("/"))
        # Configuration.__init__'s access_token param is a no-op in this SDK
        # version (it unconditionally sets self.access_token = None) --
        # setting the attribute directly afterward is the only way that
        # actually sticks.
        self._configuration.access_token = api_token
        api_client = ApiClient(self._configuration)
        self._auth_api = AuthenticationApi(api_client)
        self._device_api = DeviceApi(api_client)
        self._location_api = LocationApi(api_client)
        self._legacy_session = requests.Session()  # for get_wired_portlist only

        # None = static API token (never expires from our side) until a login
        # response sets it; set once we've logged in with username/password.
        self._token_expiry: float | None = None if api_token else 0.0

    def _ensure_token(self) -> None:
        if self._configuration.access_token is not None and self._token_expiry is None:
            return
        if self._token_expiry is not None and time.time() < self._token_expiry:
            return
        if self._username and self._password:
            self._login()
        elif self._configuration.access_token is None:
            raise XiqApiError("No credentials available to authenticate with XIQ")

    def _login(self) -> None:
        try:
            result = self._auth_api.login(
                body={"username": self._username, "password": self._password},
                skip_deserialization=True,
            )
        except ApiException as exc:
            raise XiqApiError(f"XIQ login failed ({exc.status}): {exc.body}") from exc
        payload = json.loads(result.response.data)
        self._configuration.access_token = payload["access_token"]
        # Refresh a minute early so a request never races token expiry.
        self._token_expiry = time.time() + payload.get("expires_in", 86400) - 60

    def _call(self, fn, **kwargs) -> dict:
        """Call an SDK Api method with skip_deserialization=True (see module
        docstring) and return the parsed JSON body, re-logging in once on a
        401 (mirrors the legacy-endpoint retry below) before giving up.
        """
        self._ensure_token()
        try:
            result = fn(skip_deserialization=True, **kwargs)
        except ApiException as exc:
            if exc.status == 401 and self._username and self._password:
                self._token_expiry = 0.0
                self._login()
                try:
                    result = fn(skip_deserialization=True, **kwargs)
                except ApiException as retry_exc:
                    raise XiqApiError(f"XIQ API error {retry_exc.status}: {retry_exc.body}") from retry_exc
            else:
                raise XiqApiError(f"XIQ API error {exc.status}: {exc.body}") from exc
        return json.loads(result.response.data)

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
            payload = self._call(self._device_api.list_devices, query_params=frozendict.frozendict(params))
            yield from payload.get("data", [])
            total_pages = payload.get("total_pages", page)
            if page >= total_pages:
                break
            page += 1

    def get_location_tree(self, *, parent_id: int | None = None, expand_children: bool = True) -> list[dict]:
        """Return the XIQ location hierarchy as nested {id, name, children} dicts.

        expand_children=False can't actually be sent: this SDK version's query
        param serializer round-trips any bool through Python's native `bool`
        before its URI-template expansion step, which only handles str/float/int
        -- confirmed this isn't fixable by wrapping the value in the SDK's own
        BoolSchema first, it still ends up native `bool` by the time it matters.
        True is XIQ's own server-side default when the param is omitted, so we
        just omit it rather than route around a bug for a value we don't need.
        """
        if not expand_children:
            raise NotImplementedError(
                "expand_children=False can't be sent -- this SDK version can't serialize "
                "boolean query params at all (see docstring)"
            )
        params: dict = {}
        if parent_id is not None:
            params["parentId"] = parent_id
        return self._call(self._location_api.get_location_tree, query_params=frozendict.frozendict(params))

    def get_radio_information(
        self, *, device_ids: list[int], limit: int = RADIO_PAGE_LIMIT
    ) -> Iterator[dict]:
        """Yield one {device_id, radios: [...]} record per AP in device_ids, across all pages.

        includeDisabledRadio can't be set to True: same boolean-query-param
        serialization bug as get_location_tree's expand_children (see its
        docstring). False (enabled radios only, XIQ's own server-side
        default) is what this worker wants anyway, so it's simply omitted
        rather than routed around.
        """
        page = 1
        while True:
            params: dict = {"page": page, "limit": limit, "deviceIds": device_ids}
            payload = self._call(
                self._device_api.list_devices_radio_information, query_params=frozendict.frozendict(params)
            )
            yield from payload.get("data", [])
            total_pages = payload.get("total_pages", page)
            if page >= total_pages:
                break
            page += 1

    def get_wired_portlist(self, device_id: int) -> list[dict]:
        """Return the wired port list for one switch device (see module docstring)."""
        self._ensure_token()
        url = f"{LEGACY_BASE_URL}/xiq/v0/monitor/device/wired/portlist"
        headers = {"Authorization": f"Bearer {self._configuration.access_token}"}
        resp = self._legacy_session.get(
            url, headers=headers, params={"deviceId": device_id}, timeout=self._timeout
        )
        if resp.status_code == 401 and self._username and self._password:
            self._token_expiry = 0.0
            self._login()
            headers = {"Authorization": f"Bearer {self._configuration.access_token}"}
            resp = self._legacy_session.get(
                url, headers=headers, params={"deviceId": device_id}, timeout=self._timeout
            )
        if resp.status_code >= 400:
            raise XiqApiError(f"XIQ API error {resp.status_code}: {resp.text}")
        return resp.json().get("data", {}).get("portList", [])
