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


def _map(**overrides):
    kwargs = {
        "location_index": mapper.build_location_index(LOC_TREE),
        "default_site": "XIQ-Unmapped",
    }
    kwargs.update(overrides)
    return mapper.devices_to_entities(DEVICES, **kwargs)


def _site_entities(entities):
    return [e._kw["site"] for e in entities if "site" in e._kw]


def _devices(entities):
    return [e._kw["device"] for e in entities if "device" in e._kw]


def test_devices_map_directly_to_their_own_xiq_location_as_site(stub_sdk):
    entities = _map()
    site_entities = _site_entities(entities)

    assert {s._kw["name"] for s in site_entities} == {"Floor 1", "Floor 2"}


def test_device_carries_identity_custom_fields_tags_site_and_status(stub_sdk):
    device = _devices(_map())[0]

    assert cf(device._kw["custom_fields"]["xiq_network_policy"]._kw) == "Corp-WiFi"
    assert device._kw["site"]._kw["name"] == "Floor 1"
    assert device._kw["tags"] == ["extreme-networks", "xiq", "discovered"]
    assert device._kw["status"] == "active"
    assert "role" not in device._kw
    assert "device_type" not in device._kw
    assert "platform" not in device._kw
    assert "primary_ip4" not in device._kw


def test_switch_with_no_policy_drops_empty_custom_field_and_is_offline(stub_sdk):
    switch = _devices(_map())[1]

    assert "xiq_network_policy" not in switch._kw["custom_fields"]
    assert switch._kw["status"] == "offline"


def test_site_scope_filters_devices_and_sites_outside_scope(stub_sdk):
    in_scope = _map(site_scope={"Floor 1", "Floor 2"})
    assert len(_devices(in_scope)) == 2

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


def test_ports_to_entities_maps_link_state_speed_duplex_and_type(stub_sdk):
    interfaces = _interfaces(mapper.ports_to_entities(PORTS, device="sw-idf1"))

    up_port = interfaces[0]
    assert up_port._kw["device"] == "sw-idf1"
    assert up_port._kw["name"] == "1/1"
    assert up_port._kw["mark_connected"] is True
    assert "enabled" not in up_port._kw  # XIQ has no admin-state signal; not asserted
    assert up_port._kw["speed"] == 1_000_000
    assert up_port._kw["duplex"] == "full"
    assert up_port._kw["type"] == "1000base-t"  # guessed from SPEED_1000M

    down_port = interfaces[1]
    assert down_port._kw["mark_connected"] is False
    assert down_port._kw["speed"] is None  # SPEED_AUTO isn't a real link speed
    assert down_port._kw["duplex"] is None  # "N/A" has no netbox equivalent
    assert down_port._kw["type"] is None  # SPEED_AUTO has no known type mapping


def test_ports_to_entities_carries_identity_custom_fields_and_tags(stub_sdk):
    interfaces = _interfaces(mapper.ports_to_entities(PORTS, device="sw-idf1"))

    up_port_cf = interfaces[0]._kw["custom_fields"]
    assert cf(up_port_cf["xiq_port_id"]._kw) == "2166175345772344"
    assert interfaces[0]._kw["tags"] == ["extreme-networks", "xiq", "discovered"]

    down_port_cf = interfaces[1]._kw["custom_fields"]
    assert cf(down_port_cf["xiq_port_id"]._kw) == "2166175345772421"
    assert interfaces[1]._kw["description"] == "uplink to core"


def test_ports_to_entities_does_not_assert_mode(stub_sdk):
    """mode is intentionally left unset -- see ports_to_entities docstring
    (FLEX-UNI/Fabric-Attach ports map into an I-SID, not a VLAN)."""
    interfaces = _interfaces(mapper.ports_to_entities(PORTS, device="sw-idf1"))

    assert "mode" not in interfaces[0]._kw
