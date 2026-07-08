"""Mapping logic tests against a stubbed Diode SDK (no protobuf/network needed)."""

from __future__ import annotations

import json

from orb_extreme_xiq import mapper

from .conftest import cf

LOC_TREE = [
    {
        "id": 1,
        "name": "HQ",
        "children": [
            {"id": 2, "name": "Floor 1", "children": []},
            {"id": 3, "name": "Floor 2", "children": []},
        ],
    }
]

DEVICES = [
    {
        "id": 111,
        "hostname": "ap-lobby",
        "serial_number": "SN111",
        "product_type": "AP305C",
        "device_function": "AP",
        "ip_address": "10.0.0.5",
        "connected": True,
        "location_id": 2,
        "software_version": "10.6r3",
        "network_policy_name": "Corp-WiFi",
        "mac_address": "AA:BB:CC:00:00:11",
        "org_id": "org-9",
    },
    {
        "id": 222,
        "hostname": "sw-idf1",
        "serial_number": "SN222",
        "product_type": "5420F",
        "device_function": "SWITCH",
        "ip_address": "10.0.0.6",
        "connected": False,
        "location_id": 3,
        "org_id": "org-9",
    },
]

LOCATION_SITE_MAPPING = {"HQ": "Corporate-HQ"}  # both floors roll up to HQ -> one site


def _map(**overrides):
    kwargs = {
        "location_index": mapper.build_location_index(LOC_TREE),
        "location_site_mapping": LOCATION_SITE_MAPPING,
        "default_site": "XIQ-Unmapped",
    }
    kwargs.update(overrides)
    return mapper.devices_to_entities(DEVICES, **kwargs)


def _site_entities(entities):
    return [
        e for e in entities if "site" in e._kw and getattr(e._kw.get("site"), "_kw", {}).get("custom_fields")
    ]


def _devices(entities):
    return [e._kw["device"] for e in entities if "device" in e._kw]


def test_consolidates_multiple_locations_into_one_site(stub_sdk):
    entities = _map()
    site_entities = _site_entities(entities)
    assert len(site_entities) == 1

    xiq_locations_cf = site_entities[0]._kw["site"]._kw["custom_fields"]["xiq_locations"]._kw
    assert json.loads(cf(xiq_locations_cf)) == ["HQ"]


def test_device_carries_identity_custom_fields_tags_and_site(stub_sdk):
    device = _devices(_map())[0]

    assert cf(device._kw["custom_fields"]["xiq_device_id"]._kw) == "111"
    assert cf(device._kw["custom_fields"]["xiq_network_policy"]._kw) == "Corp-WiFi"
    assert device._kw["site"]._kw["name"] == "Corporate-HQ"
    assert "source:xiq" in device._kw["tags"]
    assert "xiq-org:org-9" in device._kw["tags"]
    assert device._kw["role"] == "wireless-ap"
    assert device._kw["status"] == "active"


def test_switch_with_no_policy_drops_empty_custom_field_and_is_offline(stub_sdk):
    switch = _devices(_map())[1]

    assert "xiq_network_policy" not in switch._kw["custom_fields"]
    assert switch._kw["status"] == "offline"


def test_dropping_site_from_authority_omits_it_with_no_redrift(stub_sdk):
    authority = set(mapper.DEFAULT_AUTHORITY) - {"site"}
    entities = _map(authority=authority)

    assert "site" not in _devices(entities)[0]._kw
    assert not _site_entities(entities)


def test_site_scope_filters_devices_and_sites_outside_scope(stub_sdk):
    in_scope = _map(site_scope={"Corporate-HQ"})
    assert len(_devices(in_scope)) == 2  # both devices resolve into Corporate-HQ

    out_of_scope = _map(site_scope={"Some-Other-Site"})
    assert _devices(out_of_scope) == []
    assert _site_entities(out_of_scope) == []
