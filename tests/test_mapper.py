"""mapper.py tests against the stubbed Diode SDK (see conftest.stub_sdk).

Fixture payloads are shaped exactly like the two Platform ONE OpenAPI
specs' schemas: Assets `Device`, ConfigState `AssetLocation`,
`AssetPortConfig`, `AssetPortState`, `AssetInterfaceVlanProperties` (with
nested `AssetInterfaceVlanMap` rows).
"""

from __future__ import annotations

from orb_extreme_platformone import mapper
from tests.conftest import cf

SWITCH_ASSET = {
    "device_id": 42,
    "host_name": "sw-idf1",
    "serial_number": "SN42",
    "mac_address": "aabbccddeeff",
    "product_type": "FabricEngine_5320_48P_8XE",
    "function": "Fabric Engine",
    "os_version": "9.2.1.0",
    "is_connected": True,
    "ip_address": "10.0.0.2",
    "site_name": "Assets-Site",
}


def _record(asset=SWITCH_ASSET, location=None, cs_device_id="cs-uuid-42"):
    return {"asset": asset, "cs_device_id": cs_device_id, "location": location}


def test_devices_to_entities_builds_site_location_chain_and_device(stub_sdk):
    location = {"site_name": "HQ", "building_name": "B1", "floor_name": "F2"}
    entities = mapper.devices_to_entities([_record(location=location)], default_site="Unmapped")

    site, building, floor, device = (e._kw for e in entities)
    assert site["site"]._kw == {"name": "HQ"}
    assert building["location"]._kw["name"] == "B1"
    assert building["location"]._kw["parent"] is None
    assert floor["location"]._kw["name"] == "F2"
    assert floor["location"]._kw["parent"] is building["location"]
    assert device["device"]._kw["location"] is floor["location"]


def test_devices_to_entities_maps_the_assets_fields(stub_sdk):
    entities = mapper.devices_to_entities([_record()], default_site="Unmapped")

    device = entities[-1]._kw["device"]._kw
    assert device["name"] == "sw-idf1"
    assert device["serial"] == "SN42"
    assert device["status"] == "active"
    assert device["site"]._kw == {"name": "Assets-Site"}
    assert device["device_type"]._kw["model"] == "5320-48P-8XE-FabricEngine"
    assert device["device_type"]._kw["manufacturer"] == "Extreme Networks"
    assert device["platform"]._kw["name"] == "9.2.1.0"
    assert device["primary_ip4"] == "10.0.0.2/32"
    assert cf(device["custom_fields"]["platformone_device_id"]._kw) == "42"
    assert device["tags"] == ["extreme-networks", "platform-one", "discovered"]


def test_devices_to_entities_disconnected_device_is_offline(stub_sdk):
    asset = {**SWITCH_ASSET, "is_connected": False}
    entities = mapper.devices_to_entities([_record(asset=asset)], default_site="Unmapped")

    assert entities[-1]._kw["device"]._kw["status"] == "offline"


def test_devices_to_entities_without_any_site_uses_the_default_site(stub_sdk):
    asset = {"device_id": 7, "host_name": "sw-lost", "is_connected": True}
    entities = mapper.devices_to_entities([_record(asset=asset)], default_site="Unmapped")

    assert entities[0]._kw["site"]._kw == {"name": "Unmapped"}
    assert entities[1]._kw["device"]._kw["site"]._kw == {"name": "Unmapped"}
    assert "location" not in entities[1]._kw["device"]._kw


def test_scope_devices_filters_on_the_resolved_site():
    in_scope = _record(location={"site_name": "HQ"})
    out_of_scope = _record(location={"site_name": "Branch"})

    scoped = mapper.scope_devices([in_scope, out_of_scope], default_site="Unmapped", site_scope={"HQ"})

    assert scoped == [in_scope]


def test_scope_devices_without_a_scope_returns_everything():
    records = [_record(), _record(location={"site_name": "HQ"})]
    assert mapper.scope_devices(records, default_site="Unmapped", site_scope=None) == records


PORT_CONFIG = {
    "asset_device_id": "cs-uuid-42",
    "asset_interface_id": "if-uuid-1",
    "name": "1/1",
    "enabled": True,
    "description": "uplink to core",
}

PORT_STATE = {
    "asset_device_id": "cs-uuid-42",
    "asset_interface_id": "if-uuid-1",
    "name": "1/1",
    "oper_state": 1,
    "oper_speed": 4,
    "oper_duplex": 2,
    "connector_type": 1,
    "mac_address": "aa:bb:cc:dd:ee:01",
    "if_index": 1,
}

VLAN_PROPERTIES = {
    "device_id": "cs-uuid-42",
    "asset_interface_id": "if-uuid-1",
    "interface_name": "1/1",
    "port_vlan": 10,
    "vlans": [{"vlan_number": 10}, {"vlan_number": 20}, {"vlan_number": 30}],
}


def _tables(**overrides):
    tables = {
        "port_configs": [PORT_CONFIG],
        "port_states": [PORT_STATE],
        "vlan_properties": [VLAN_PROPERTIES],
    }
    tables.update(overrides)
    return tables


def test_ports_to_entities_maps_config_state_and_vlans_onto_one_interface(stub_sdk):
    entities = mapper.ports_to_entities(_tables(), device="sw-idf1")

    assert len(entities) == 1
    port = entities[0]._kw["interface"]._kw
    assert port["device"] == "sw-idf1"
    assert port["name"] == "1/1"
    assert port["enabled"] is True
    assert port["mark_connected"] is True
    assert port["speed"] == 1_000_000
    assert port["duplex"] == "full"
    assert port["type"] == "1000base-t"
    assert port["description"] == "uplink to core"
    assert port["primary_mac_address"] == "aa:bb:cc:dd:ee:01"
    assert port["untagged_vlan"]._kw == {"vid": 10}
    assert [v._kw["vid"] for v in port["tagged_vlans"]] == [20, 30]
    assert port["mode"] == "tagged"
    assert cf(port["custom_fields"]["platformone_interface_id"]._kw) == "if-uuid-1"


def test_ports_to_entities_config_only_port_still_syncs_admin_state(stub_sdk):
    entities = mapper.ports_to_entities(_tables(port_states=[], vlan_properties=[]), device="sw-idf1")

    port = entities[0]._kw["interface"]._kw
    assert port["enabled"] is True
    assert "mark_connected" not in port
    assert "speed" not in port


def test_ports_to_entities_state_only_port_still_syncs_link_state(stub_sdk):
    down = {**PORT_STATE, "oper_state": 2}
    entities = mapper.ports_to_entities(
        _tables(port_configs=[], port_states=[down], vlan_properties=[]), device="sw-idf1"
    )

    port = entities[0]._kw["interface"]._kw
    assert port["mark_connected"] is False
    assert "enabled" not in port


def test_ports_to_entities_admin_down_and_link_down_are_independent(stub_sdk):
    config = {**PORT_CONFIG, "enabled": False}
    state = {**PORT_STATE, "oper_state": 2}
    entities = mapper.ports_to_entities(
        _tables(port_configs=[config], port_states=[state], vlan_properties=[]), device="sw-idf1"
    )

    port = entities[0]._kw["interface"]._kw
    assert port["enabled"] is False
    assert port["mark_connected"] is False


def test_ports_to_entities_unverified_enum_codes_assert_nothing(stub_sdk):
    """ConfigState's integer enums have no published value table; codes not
    verified against a real device must not map to speed/duplex/type."""
    state = {**PORT_STATE, "oper_speed": 7, "oper_duplex": 9, "connector_type": 3}
    entities = mapper.ports_to_entities(_tables(port_states=[state], vlan_properties=[]), device="sw-idf1")

    port = entities[0]._kw["interface"]._kw
    assert "speed" not in port
    assert "duplex" not in port
    assert "type" not in port


def test_ports_to_entities_fiber_gig_port_maps_to_sfp_type(stub_sdk):
    state = {**PORT_STATE, "connector_type": 2}
    entities = mapper.ports_to_entities(_tables(port_states=[state], vlan_properties=[]), device="sw-idf1")

    assert entities[0]._kw["interface"]._kw["type"] == "1000base-x-sfp"


def test_ports_to_entities_untagged_only_is_access_mode(stub_sdk):
    vlan = {**VLAN_PROPERTIES, "vlans": [{"vlan_number": 10}]}
    entities = mapper.ports_to_entities(_tables(vlan_properties=[vlan]), device="sw-idf1")

    port = entities[0]._kw["interface"]._kw
    assert port["mode"] == "access"
    assert port["untagged_vlan"]._kw == {"vid": 10}
    assert "tagged_vlans" not in port


def test_ports_to_entities_no_vlan_rows_asserts_no_mode():
    """FLEX-UNI/Fabric-Attach ports can be mapped to an I-SID instead of a
    VLAN -- inventing an access mode would misrepresent them."""
    fields = mapper._vlan_fields([])
    assert fields == {}


def test_ports_to_entities_ports_join_on_interface_id_not_row_order(stub_sdk):
    config2 = {**PORT_CONFIG, "asset_interface_id": "if-uuid-2", "name": "1/2", "enabled": False}
    state2 = {**PORT_STATE, "asset_interface_id": "if-uuid-2", "name": "1/2", "oper_state": 2}
    entities = mapper.ports_to_entities(
        _tables(
            port_configs=[PORT_CONFIG, config2],
            port_states=[state2, PORT_STATE],  # deliberately reversed order
            vlan_properties=[],
        ),
        device="sw-idf1",
    )

    ports = {e._kw["interface"]._kw["name"]: e._kw["interface"]._kw for e in entities}
    assert ports["1/1"]["enabled"] is True
    assert ports["1/1"]["mark_connected"] is True
    assert ports["1/2"]["enabled"] is False
    assert ports["1/2"]["mark_connected"] is False
