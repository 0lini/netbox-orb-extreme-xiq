"""Tests for the XIQ client: pagination, token auth, login/refresh, 401 re-auth.

All XIQ HTTP calls go through plain `requests` now, so every endpoint --
including login -- is mocked with `responses`.
"""

from __future__ import annotations

import pytest
import responses

from orb_extreme_xiq.client import DEFAULT_BASE_URL, LEGACY_BASE_URL, XiqApiError, XiqClient


def test_requires_some_credentials():
    with pytest.raises(ValueError):
        XiqClient()


@responses.activate
def test_get_devices_paginates_until_last_page():
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/devices",
        json={"data": [{"id": 1}, {"id": 2}], "total_pages": 2},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/devices",
        json={"data": [{"id": 3}], "total_pages": 2},
        status=200,
    )

    client = XiqClient(api_token="tok123")
    devices = list(client.get_devices())

    assert [d["id"] for d in devices] == [1, 2, 3]
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok123"


@responses.activate
def test_get_radio_information_paginates_and_sends_device_ids():
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/devices/radio-information",
        json={"data": [{"device_id": 1, "radios": []}], "total_pages": 2},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/devices/radio-information",
        json={"data": [{"device_id": 2, "radios": []}], "total_pages": 2},
        status=200,
    )

    client = XiqClient(api_token="tok123")
    radio_infos = list(client.get_radio_information(device_ids=[1, 2]))

    assert [r["device_id"] for r in radio_infos] == [1, 2]
    assert all(c.request.params["deviceIds"] == ["1", "2"] for c in responses.calls)
    assert [c.request.params["page"] for c in responses.calls] == ["1", "2"]


@responses.activate
def test_get_location_tree_returns_nested_payload():
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/locations/tree",
        json=[{"id": 1, "name": "HQ", "children": []}],
        status=200,
    )

    client = XiqClient(api_token="tok123")
    assert client.get_location_tree() == [{"id": 1, "name": "HQ", "children": []}]


@responses.activate
def test_username_password_login_is_used_as_bearer_token():
    responses.add(
        responses.POST, f"{DEFAULT_BASE_URL}/login", json={"access_token": "jwt-abc"}, status=200
    )
    responses.add(
        responses.GET, f"{DEFAULT_BASE_URL}/devices", json={"data": [], "total_pages": 1}, status=200
    )

    client = XiqClient(username="u@example.com", password="pw")
    list(client.get_devices())

    assert responses.calls[1].request.headers["Authorization"] == "Bearer jwt-abc"


@responses.activate
def test_401_triggers_relogin_for_username_password_client():
    responses.add(
        responses.POST, f"{DEFAULT_BASE_URL}/login", json={"access_token": "jwt-1"}, status=200
    )
    responses.add(responses.GET, f"{DEFAULT_BASE_URL}/devices", status=401)
    responses.add(
        responses.POST, f"{DEFAULT_BASE_URL}/login", json={"access_token": "jwt-2"}, status=200
    )
    responses.add(
        responses.GET, f"{DEFAULT_BASE_URL}/devices", json={"data": [], "total_pages": 1}, status=200
    )

    client = XiqClient(username="u@example.com", password="pw")
    list(client.get_devices())

    device_calls = [c for c in responses.calls if c.request.url.split("?")[0].endswith("/devices")]
    assert [c.request.headers["Authorization"] for c in device_calls] == ["Bearer jwt-1", "Bearer jwt-2"]


@responses.activate
def test_401_persisting_after_relogin_raises_xiq_api_error():
    responses.add(
        responses.POST, f"{DEFAULT_BASE_URL}/login", json={"access_token": "jwt-new"}, status=200
    )
    responses.add(responses.GET, f"{DEFAULT_BASE_URL}/devices", status=401)
    responses.add(responses.GET, f"{DEFAULT_BASE_URL}/devices", status=401)

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
def test_get_wired_portlist_401_triggers_relogin_for_username_password_client():
    responses.add(
        responses.POST, f"{DEFAULT_BASE_URL}/login", json={"access_token": "jwt-1"}, status=200
    )
    responses.add(responses.GET, f"{LEGACY_BASE_URL}/xiq/v0/monitor/device/wired/portlist", status=401)
    responses.add(
        responses.POST, f"{DEFAULT_BASE_URL}/login", json={"access_token": "jwt-2"}, status=200
    )
    responses.add(
        responses.GET,
        f"{LEGACY_BASE_URL}/xiq/v0/monitor/device/wired/portlist",
        json={"data": {"portList": []}},
        status=200,
    )

    client = XiqClient(username="u@example.com", password="pw")
    client.get_wired_portlist(999)

    assert responses.calls[-1].request.headers["Authorization"] == "Bearer jwt-2"
