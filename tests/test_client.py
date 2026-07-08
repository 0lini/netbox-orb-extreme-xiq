"""Tests for the XIQ REST client: pagination, token auth, login/refresh, 401 re-auth."""

from __future__ import annotations

import pytest
import responses

from orb_extreme_xiq.client import XiqClient

BASE = "https://api.extremecloudiq.com"


def test_requires_some_credentials():
    with pytest.raises(ValueError):
        XiqClient()


@responses.activate
def test_get_devices_paginates_until_last_page():
    responses.add(
        responses.GET,
        f"{BASE}/devices",
        json={"page": 1, "count": 2, "total_pages": 2, "total_count": 3, "data": [{"id": 1}, {"id": 2}]},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/devices",
        json={"page": 2, "count": 1, "total_pages": 2, "total_count": 3, "data": [{"id": 3}]},
        status=200,
    )

    client = XiqClient(api_token="tok123")
    devices = list(client.get_devices())

    assert [d["id"] for d in devices] == [1, 2, 3]


@responses.activate
def test_get_location_tree_returns_nested_payload():
    responses.add(
        responses.GET,
        f"{BASE}/locations/tree",
        json=[{"id": 1, "name": "HQ", "children": []}],
        status=200,
    )

    client = XiqClient(api_token="tok123")
    assert client.get_location_tree() == [{"id": 1, "name": "HQ", "children": []}]


@responses.activate
def test_username_password_login_is_used_as_bearer_token():
    responses.add(
        responses.POST,
        f"{BASE}/login",
        json={"access_token": "jwt-abc", "token_type": "Bearer", "expires_in": 86400},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/devices",
        json={"page": 1, "count": 0, "total_pages": 1, "total_count": 0, "data": []},
        status=200,
    )

    client = XiqClient(username="u@example.com", password="pw")
    list(client.get_devices())

    auth_header = responses.calls[1].request.headers["Authorization"]
    assert auth_header == "Bearer jwt-abc"


@responses.activate
def test_401_triggers_relogin_for_username_password_client():
    responses.add(
        responses.POST,
        f"{BASE}/login",
        json={"access_token": "jwt-1", "token_type": "Bearer", "expires_in": 86400},
        status=200,
    )
    responses.add(responses.GET, f"{BASE}/devices", status=401)
    responses.add(
        responses.POST,
        f"{BASE}/login",
        json={"access_token": "jwt-2", "token_type": "Bearer", "expires_in": 86400},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/devices",
        json={"page": 1, "count": 0, "total_pages": 1, "total_count": 0, "data": []},
        status=200,
    )

    client = XiqClient(username="u@example.com", password="pw")
    list(client.get_devices())

    assert responses.calls[-1].request.headers["Authorization"] == "Bearer jwt-2"
