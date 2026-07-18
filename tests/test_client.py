"""PlatformOneClient tests -- HTTP mocked with `responses`.

Response shapes mirror the two Platform ONE OpenAPI specs: `PagedDevice`
(Assets, top-level data/total_pages) and the ConfigState GetResponse
envelope (records under the table's PascalCase schema name + `Pagination`).
"""

from __future__ import annotations

import json

import pytest
import responses

from orb_extreme_platformone.client import (
    DEFAULT_BASE_URL,
    PlatformOneApiError,
    PlatformOneClient,
    configstate_response_key,
    truncate_error_body,
)

ASSETS_URL = f"{DEFAULT_BASE_URL}/assets/v1/devices"


def _client() -> PlatformOneClient:
    return PlatformOneClient(api_token="tok")


def test_client_requires_a_token():
    with pytest.raises(ValueError):
        PlatformOneClient(api_token=None)


def test_client_requires_https_base_url():
    with pytest.raises(ValueError, match="https://"):
        PlatformOneClient(base_url="http://cloudapi.extremecloudiq.com", api_token="tok")


def test_truncate_error_body_collapses_and_limits_length():
    assert truncate_error_body("  a \n b  ") == "a b"
    long = "x" * 500
    truncated = truncate_error_body(long, limit=20)
    assert truncated == ("x" * 17) + "..."
    assert len(truncated) == 20


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("asset-device", "AssetDevice"),
        ("asset-port-state", "AssetPortState"),
        ("asset-interface-vlan-properties", "AssetInterfaceVlanProperties"),
        ("asset-vlan-config", "AssetVlanConfig"),
        ("inferred-cluster", "InferredCluster"),
        ("inferred-device", "InferredDevice"),
        ("asset-lldp-neighbor-state", "AssetLldpNeighborState"),
        ("asset-l2-vsn-suni-config", "AssetL2VsnSuniConfig"),
    ],
)
def test_configstate_response_key_matches_the_spec_schema_names(table, key):
    assert configstate_response_key(table) == key


@responses.activate
def test_get_devices_paginates_and_sends_the_classification_filter():
    for page, data in [(1, [{"device_id": 1}]), (2, [{"device_id": 2}])]:
        responses.add(
            responses.POST,
            ASSETS_URL,
            match=[
                responses.matchers.query_param_matcher({"page": str(page), "limit": "500"}),
                responses.matchers.json_params_matcher({"classification": "ALL"}),
            ],
            json={"data": data, "page": page, "total_pages": 2, "total_count": 2},
            status=200,
        )

    devices = list(_client().get_devices())

    assert [d["device_id"] for d in devices] == [1, 2]


@responses.activate
def test_get_devices_passes_a_custom_classification_through_verbatim():
    responses.add(
        responses.POST,
        ASSETS_URL,
        match=[responses.matchers.json_params_matcher({"classification": "WIRELESS"})],
        json={"data": [], "page": 1, "total_pages": 1, "total_count": 0},
        status=200,
    )

    assert list(_client().get_devices(classification="WIRELESS")) == []


@responses.activate
def test_retrieve_paginates_and_unwraps_the_tables_response_key():
    url = f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-state"
    for page, records in [(1, [{"name": "1/1"}]), (2, [{"name": "1/2"}])]:
        responses.add(
            responses.POST,
            url,
            match=[responses.matchers.query_param_matcher({"page_number": str(page), "page_size": "500"})],
            json={
                "AssetPortState": records,
                "Pagination": {"page": page, "total_pages": 2, "count": 1, "total_count": 2},
            },
            status=200,
        )

    records = list(_client().retrieve("asset-port-state", {"asset_device_id": ["uuid-1"]}))

    assert [r["name"] for r in records] == ["1/1", "1/2"]
    assert json.loads(responses.calls[0].request.body) == {"asset_device_id": ["uuid-1"]}


@responses.activate
def test_retrieve_sends_an_empty_filter_body_by_default():
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-device",
        match=[responses.matchers.json_params_matcher({})],
        json={"AssetDevice": [], "Pagination": {"total_pages": 1}},
        status=200,
    )

    assert list(_client().retrieve("asset-device")) == []


@responses.activate
def test_retrieve_tolerates_a_null_records_key():
    """ConfigState marks the records array nullable in its spec -- an empty
    table comes back as null, not []."""
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-config",
        json={"AssetPortConfig": None, "Pagination": {"total_pages": 1}},
        status=200,
    )

    assert list(_client().retrieve("asset-port-config")) == []


@responses.activate
def test_non_2xx_raises_platform_one_api_error():
    responses.add(responses.POST, ASSETS_URL, json={"error": "nope"}, status=403)

    with pytest.raises(PlatformOneApiError, match="403") as excinfo:
        list(_client().get_devices())
    assert "nope" in str(excinfo.value)


@responses.activate
def test_non_2xx_truncates_long_error_bodies():
    responses.add(responses.POST, ASSETS_URL, body="e" * 1000, status=500)

    with pytest.raises(PlatformOneApiError) as excinfo:
        list(_client().get_devices())
    message = str(excinfo.value)
    assert "500" in message
    assert "e" * 1000 not in message
    assert message.endswith("...")
