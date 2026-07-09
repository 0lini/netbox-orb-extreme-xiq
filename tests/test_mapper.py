"""Mapping logic tests against a stubbed Diode SDK (no protobuf/network needed)."""

from __future__ import annotations

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

LOCATION_SITE_MAPPING = {"HQ": "Corporate-HQ"}


def _map(**overrides):
    kwargs = {
        "location_index": mapper.build_location_index(LOC_TREE),
        "location_site_mapping": LOCATION_SITE_MAPPING,
        "default_site": "XIQ-Unmapped",
    }
    kwargs.update(overrides)
    return mapper.devices_to_entities(DEVICES, **kwargs)


def _locations(entities):
    return [e._kw["location"] for e in entities if "location" in e._kw]


def _devices(entities):
    return [e._kw["device"] for e in entities if "device" in e._kw]


def test_location_tree_is_nested_under_the_resolved_site(stub_sdk):
    by_name = {loc._kw["name"]: loc for loc in _locations(_map())}
    assert set(by_name) == {"HQ", "Floor 1", "Floor 2"}

    hq = by_name["HQ"]
    assert hq._kw["site"]._kw["name"] == "Corporate-HQ"
    assert "parent" not in hq._kw  # root location has no parent Location

    floor1 = by_name["Floor 1"]
    assert floor1._kw["site"]._kw["name"] == "Corporate-HQ"
    assert floor1._kw["parent"] == "HQ"


def test_location_carries_stable_id_custom_field(stub_sdk):
    by_name = {loc._kw["name"]: loc for loc in _locations(_map())}

    assert cf(by_name["HQ"]._kw["custom_fields"]["xiq_location_id"]._kw) == "1"
    assert cf(by_name["Floor 1"]._kw["custom_fields"]["xiq_location_id"]._kw) == "2"


def test_device_references_both_site_and_location(stub_sdk):
    device = _devices(_map())[0]

    assert device._kw["site"]._kw["name"] == "Corporate-HQ"
    assert device._kw["location"]._kw["name"] == "Floor 1"


def test_device_carries_identity_custom_fields_and_tags(stub_sdk):
    device = _devices(_map())[0]

    assert cf(device._kw["custom_fields"]["xiq_device_id"]._kw) == "111"
    assert cf(device._kw["custom_fields"]["xiq_network_policy"]._kw) == "Corp-WiFi"
    assert "source:xiq" in device._kw["tags"]
    assert "xiq-org:org-9" in device._kw["tags"]
    assert device._kw["role"] == "wireless-ap"
    assert device._kw["status"] == "active"


def test_switch_with_no_policy_drops_empty_custom_field_and_is_offline(stub_sdk):
    switch = _devices(_map())[1]

    assert "xiq_network_policy" not in switch._kw["custom_fields"]
    assert switch._kw["status"] == "offline"


def test_dropping_site_from_authority_omits_site_and_location_with_no_redrift(stub_sdk):
    authority = set(mapper.DEFAULT_AUTHORITY) - {"site"}
    entities = _map(authority=authority)

    device = _devices(entities)[0]
    assert "site" not in device._kw
    assert "location" not in device._kw
    assert _locations(entities) == []


def test_site_scope_filters_devices_and_their_locations_outside_scope(stub_sdk):
    in_scope = _map(site_scope={"Corporate-HQ"})
    assert len(_devices(in_scope)) == 2
    assert len(_locations(in_scope)) == 3  # HQ, Floor 1, Floor 2

    out_of_scope = _map(site_scope={"Some-Other-Site"})
    assert _devices(out_of_scope) == []
    assert _locations(out_of_scope) == []


def test_distinct_roots_consolidated_into_one_site_do_not_collide(stub_sdk):
    """Two same-named child locations under different XIQ roots, both
    consolidated into the same NetBox site, must stay distinct NetBox
    Locations -- disambiguated by their different root-location parent.
    This is the scenario the flat `xiq_locations` JSON field used to lose.
    """
    tree = [
        {"id": 10, "name": "HQ", "children": [{"id": 11, "name": "Floor 1", "children": []}]},
        {"id": 20, "name": "Branch A", "children": [{"id": 21, "name": "Floor 1", "children": []}]},
    ]
    devices = [
        {"id": 1, "hostname": "sw-hq", "location_id": 11, "device_function": "SWITCH", "connected": True},
        {"id": 2, "hostname": "sw-branch", "location_id": 21, "device_function": "SWITCH", "connected": True},
    ]
    mapping = {"HQ": "Corporate-HQ", "Branch A": "Corporate-HQ"}  # both roots -> one site

    entities = mapper.devices_to_entities(
        devices,
        location_index=mapper.build_location_index(tree),
        location_site_mapping=mapping,
        default_site="XIQ-Unmapped",
    )

    floor_ones = [loc for loc in _locations(entities) if loc._kw["name"] == "Floor 1"]
    assert len(floor_ones) == 2
    assert {loc._kw["parent"] for loc in floor_ones} == {"HQ", "Branch A"}

    dev_by_name = {d._kw["name"]: d for d in _devices(entities)}
    assert dev_by_name["sw-hq"]._kw["location"]._kw["name"] == "Floor 1"
    assert dev_by_name["sw-branch"]._kw["location"]._kw["name"] == "Floor 1"
