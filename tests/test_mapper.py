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


PORTS = [
    {
        "id": 2166175345772344,
        "ifName": "1/1",
        "ifAlias": "",
        "status": "UP",
        "portSpeed": "SPEED_1000M",
        "transmissionMode": "Full-duplex",
        "portMode": "Trunk",
        "taggedVlans": "500,867",
        "lldpSystemName": "",
    },
    {
        "id": 2166175345772421,
        "ifName": "1/10",
        "ifAlias": "uplink to core",
        "status": "DOWN",
        "portSpeed": "SPEED_AUTO",
        "transmissionMode": "N/A",
        "portMode": "Trunk",
        "taggedVlans": "",
        "lldpSystemName": "core-sw-01",
    },
]


def _interfaces(entities):
    return [e._kw["interface"] for e in entities]


def test_ports_to_entities_maps_link_state_speed_and_duplex(stub_sdk):
    interfaces = _interfaces(mapper.ports_to_entities(PORTS, device="sw-idf1"))

    up_port = interfaces[0]
    assert up_port._kw["device"] == "sw-idf1"
    assert up_port._kw["name"] == "1/1"
    assert up_port._kw["enabled"] is True
    assert up_port._kw["speed"] == 1_000_000
    assert up_port._kw["duplex"] == "full"

    down_port = interfaces[1]
    assert down_port._kw["enabled"] is False
    assert down_port._kw["speed"] is None  # SPEED_AUTO isn't a real link speed
    assert down_port._kw["duplex"] is None  # "N/A" has no netbox equivalent


def test_ports_to_entities_carries_identity_custom_fields_and_tags(stub_sdk):
    interfaces = _interfaces(mapper.ports_to_entities(PORTS, device="sw-idf1"))

    up_port_cf = interfaces[0]._kw["custom_fields"]
    assert cf(up_port_cf["xiq_port_id"]._kw) == "2166175345772344"
    assert cf(up_port_cf["xiq_tagged_vlans"]._kw) == "500,867"
    assert "xiq_lldp_neighbor" not in up_port_cf  # blank lldpSystemName -> omitted
    assert interfaces[0]._kw["tags"] == ["source:xiq"]

    down_port_cf = interfaces[1]._kw["custom_fields"]
    assert cf(down_port_cf["xiq_lldp_neighbor"]._kw) == "core-sw-01"
    assert "xiq_tagged_vlans" not in down_port_cf  # blank taggedVlans -> omitted
    assert interfaces[1]._kw["description"] == "uplink to core"


def test_ports_to_entities_does_not_assert_mode_or_type(stub_sdk):
    """mode/type are intentionally left unset -- see ports_to_entities docstring
    (FLEX-UNI/Fabric-Attach ports map into an I-SID, not a VLAN)."""
    interfaces = _interfaces(mapper.ports_to_entities(PORTS, device="sw-idf1"))

    assert "mode" not in interfaces[0]._kw
    assert "type" not in interfaces[0]._kw
