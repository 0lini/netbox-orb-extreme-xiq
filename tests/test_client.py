"""Tests for the XIQ client: pagination, token auth, login/refresh, 401 re-auth.

get_devices/get_location_tree go through the official `extremecloudiq-api` SDK
(OpenAPI Generator's "oapg" style), which talks HTTP via urllib3 (not
`requests`) and normally deserializes responses into schema-validated Schema
objects. Our client calls it with `skip_deserialization=True` everywhere and
parses `result.response.data` as plain JSON itself (see client.py's module
docstring), so what we mock here is exactly that boundary: each Api method
returns an object with a `.response` attribute shaped like a urllib3
HTTPResponse (`.data` raw bytes, `.status`), and raises the SDK's own
ApiException for non-2xx -- both are the SDK's real behavior with
skip_deserialization=True, not something the mocks are inventing. Wire
format/serialization on the way *out* (query param encoding, headers) is the
SDK's own responsibility, not this repo's.

get_wired_portlist still uses plain `requests` (it's not in the SDK at all),
so that one is still mocked with `responses`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
import responses
from extremecloudiq.apis.tags.authentication_api import AuthenticationApi
from extremecloudiq.apis.tags.device_api import DeviceApi
from extremecloudiq.apis.tags.location_api import LocationApi
from extremecloudiq.exceptions import ApiException

from orb_extreme_xiq.client import LEGACY_BASE_URL, XiqApiError, XiqClient


@dataclass
class FakeHttpResponse:
    data: bytes
    status: int = 200


@dataclass
class FakeApiResponse:
    response: FakeHttpResponse


def json_response(payload: dict, status: int = 200) -> FakeApiResponse:
    return FakeApiResponse(response=FakeHttpResponse(data=json.dumps(payload).encode(), status=status))


def test_requires_some_credentials():
    with pytest.raises(ValueError):
        XiqClient()


def test_get_devices_paginates_until_last_page(monkeypatch):
    pages = [
        json_response({"data": [{"id": 1}, {"id": 2}], "total_pages": 2}),
        json_response({"data": [{"id": 3}], "total_pages": 2}),
    ]
    monkeypatch.setattr(DeviceApi, "list_devices", lambda self, **kw: pages.pop(0))

    client = XiqClient(api_token="tok123")
    devices = list(client.get_devices())

    assert [d["id"] for d in devices] == [1, 2, 3]


def test_get_location_tree_returns_nested_payload(monkeypatch):
    monkeypatch.setattr(
        LocationApi,
        "get_location_tree",
        lambda self, **kw: json_response([{"id": 1, "name": "HQ", "children": []}]),
    )

    client = XiqClient(api_token="tok123")
    assert client.get_location_tree() == [{"id": 1, "name": "HQ", "children": []}]


def test_username_password_login_is_used_as_bearer_token(monkeypatch):
    monkeypatch.setattr(
        AuthenticationApi, "login", lambda self, **kw: json_response({"access_token": "jwt-abc"})
    )
    seen_tokens = []

    def fake_list_devices(self, **kw):
        seen_tokens.append(self.api_client.configuration.access_token)
        return json_response({"data": [], "total_pages": 1})

    monkeypatch.setattr(DeviceApi, "list_devices", fake_list_devices)

    client = XiqClient(username="u@example.com", password="pw")
    list(client.get_devices())

    assert seen_tokens == ["jwt-abc"]


def test_401_triggers_relogin_for_username_password_client(monkeypatch):
    tokens = iter(["jwt-1", "jwt-2"])
    monkeypatch.setattr(
        AuthenticationApi, "login", lambda self, **kw: json_response({"access_token": next(tokens)})
    )

    calls = []

    def fake_list_devices(self, **kw):
        token = self.api_client.configuration.access_token
        calls.append(token)
        if token == "jwt-1":
            raise ApiException(status=401, reason="Unauthorized")
        return json_response({"data": [], "total_pages": 1})

    monkeypatch.setattr(DeviceApi, "list_devices", fake_list_devices)

    client = XiqClient(username="u@example.com", password="pw")
    list(client.get_devices())

    assert calls == ["jwt-1", "jwt-2"]


def test_401_persisting_after_relogin_raises_xiq_api_error_not_raw_sdk_exception(monkeypatch):
    monkeypatch.setattr(
        AuthenticationApi, "login", lambda self, **kw: json_response({"access_token": "jwt-new"})
    )

    def always_401(self, **kw):
        raise ApiException(status=401, reason="Unauthorized")

    monkeypatch.setattr(DeviceApi, "list_devices", always_401)

    client = XiqClient(username="u@example.com", password="pw")
    with pytest.raises(XiqApiError):
        list(client.get_devices())


@responses.activate
def test_get_wired_portlist_hits_legacy_host_with_device_id():
    responses.add(
        responses.GET,
        f"{LEGACY_BASE_URL}/xiq/v0/monitor/device/wired/portlist",
        json={"data": {"portList": [{"id": 1, "ifName": "1/1", "status": "UP"}]}},
        status=200,
    )

    client = XiqClient(api_token="tok123")
    ports = client.get_wired_portlist(999)

    assert ports == [{"id": 1, "ifName": "1/1", "status": "UP"}]
    assert responses.calls[0].request.params["deviceId"] == "999"
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok123"


@responses.activate
def test_get_wired_portlist_401_triggers_relogin_for_username_password_client(monkeypatch):
    # Two logins happen (initial ensure_token + relogin-on-401); each must
    # yield a different token so we can tell which one won.
    tokens = iter(["jwt-1", "jwt-2"])
    monkeypatch.setattr(
        AuthenticationApi, "login", lambda self, **kw: json_response({"access_token": next(tokens)})
    )

    responses.add(responses.GET, f"{LEGACY_BASE_URL}/xiq/v0/monitor/device/wired/portlist", status=401)
    responses.add(
        responses.GET,
        f"{LEGACY_BASE_URL}/xiq/v0/monitor/device/wired/portlist",
        json={"data": {"portList": []}},
        status=200,
    )

    client = XiqClient(username="u@example.com", password="pw")
    client.get_wired_portlist(999)

    assert responses.calls[-1].request.headers["Authorization"] == "Bearer jwt-2"
