"""Mapping logic tests against a stubbed Diode SDK (no protobuf/network needed)."""

from __future__ import annotations

from orb_extreme_xiq import mapper

from .conftest import cf

LOC_TREE = [
    {
        "id": 1,
        "name": "HQ",
        "children": [
            {
                "id": 2,
                "name": "Building A",
                "children": [
                    {"id": 3, "name": "Floor 1", "children": []},
                    {"id": 4, "name": "Floor 2", "children": []},
                ],
            }
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
        "location_id": 3,
        "software_version": "10.6r3",
        "network_policy_name": "Corp-WiFi",
        "mac_address": "AA:BB:CC:00:00:11",
        "org_id": "org-9",
        "description": "Lobby AP",
    },
    {
        "id": 222,
        "hostname": "sw-idf1",
        "serial_number": "SN222",
        "product_type": "5420F",
        "device_function": "SWITCH",
        "ip_address": "10.0.0.6",
        "connected": False,
        "location_id": 4,
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


def _location_entities(entities):
    return [e._kw["location"] for e in entities if "location" in e._kw]


def _devices(entities):
    return [e._kw["device"] for e in entities if "device" in e._kw]


def test_devices_map_to_the_root_xiq_location_as_site(stub_sdk):
    entities = _map()
    site_entities = _site_entities(entities)

    assert {s._kw["name"] for s in site_entities} == {"HQ"}


def test_nested_locations_are_deduped_and_carry_parent_and_site(stub_sdk):
    entities = _map()
    locations = {loc._kw["name"]: loc for loc in _location_entities(entities)}

    assert set(locations) == {"Building A", "Floor 1", "Floor 2"}
    assert locations["Building A"]._kw["site"] == "HQ"
    assert locations["Building A"]._kw["parent"] is None
    assert locations["Floor 1"]._kw["site"] == "HQ"
    assert locations["Floor 1"]._kw["parent"]._kw["name"] == "Building A"
    assert locations["Floor 2"]._kw["parent"]._kw["name"] == "Building A"


def test_device_carries_identity_custom_fields_tags_site_location_and_status(stub_sdk):
    device = _devices(_map())[0]

    assert cf(device._kw["custom_fields"]["xiq_network_policy"]._kw) == "Corp-WiFi"
    assert device._kw["site"]._kw["name"] == "HQ"
    assert device._kw["location"]._kw["name"] == "Floor 1"
    assert device._kw["location"]._kw["parent"]._kw["name"] == "Building A"
    assert device._kw["tags"] == ["extreme-networks", "xiq", "discovered"]
    assert device._kw["status"] == "active"
    assert "role" not in device._kw
    assert device._kw["device_type"]._kw["model"] == "AP305C"
    assert device._kw["device_type"]._kw["manufacturer"] == "Extreme Networks"
    assert device._kw["manufacturer"] == "Extreme Networks"
    assert device._kw["platform"]._kw["name"] == "10.6r3"
    assert device._kw["description"] == "Lobby AP"
    assert device._kw["primary_ip4"] == "10.0.0.5/32"


def test_device_without_software_version_or_description_omits_them(stub_sdk):
    switch = _devices(_map())[1]

    # 5420F product_type is present in the fixture, so device_type/manufacturer
    # are still asserted; only fields with no underlying XIQ data are omitted.
    assert switch._kw["device_type"]._kw["model"] == "5420F"
    assert "platform" not in switch._kw
    assert "description" not in switch._kw
    assert switch._kw["primary_ip4"] == "10.0.0.6/32"


def test_switch_with_no_policy_drops_empty_custom_field_and_is_offline(stub_sdk):
    switch = _devices(_map())[1]

    assert "xiq_network_policy" not in switch._kw["custom_fields"]
    assert switch._kw["status"] == "offline"
    assert switch._kw["location"]._kw["name"] == "Floor 2"


def test_device_with_unknown_location_gets_default_site_and_no_location(stub_sdk):
    device = mapper.devices_to_entities(
        [{**DEVICES[0], "location_id": 999}],
        location_index=mapper.build_location_index(LOC_TREE),
        default_site="XIQ-Unmapped",
    )
    device_entity = _devices(device)[0]

    assert device_entity._kw["site"]._kw["name"] == "XIQ-Unmapped"
    assert "location" not in device_entity._kw


def test_device_type_model_goes_through_the_fabricengine_suffix_mapping(stub_sdk):
    device = mapper.devices_to_entities(
        [{**DEVICES[0], "product_type": "FabricEngine_5320_48P_8XE"}],
        location_index=mapper.build_location_index(LOC_TREE),
        default_site="XIQ-Unmapped",
    )
    device_entity = _devices(device)[0]

    assert device_entity._kw["device_type"]._kw["model"] == "5320-48P-8XE-FabricEngine"


def test_site_scope_filters_devices_and_sites_outside_scope(stub_sdk):
    in_scope = _map(site_scope={"HQ"})
    assert len(_devices(in_scope)) == 2

    out_of_scope = _map(site_scope={"Some-Other-Site"})
    assert _devices(out_of_scope) == []
    assert _site_entities(out_of_scope) == []
    assert _location_entities(out_of_scope) == []


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


RADIO_INFOS = [
    {
        "device_id": 111,
        "radios": [
            {
                "name": "Radio1",
                "mac_address": "001122334455",
                "mode": "_11ax_2g",
                "frequency": "2.4GHz",
                "channel_number": 6,
                "channel_width": "MHZ_20",
                "power": 17,
                "wlans": [
                    {
                        "ssid": "Corp-WiFi",
                        "network_policy_name": "Corp-Policy",
                        "ssid_security_type": "TYPE_802DOT1X",
                        "bssid": "001122334455",
                    }
                ],
            },
            {
                "name": "Radio2",
                "mac_address": "001122334456",
                "mode": "_11ax_5g",
                "frequency": "5GHz",
                "channel_number": 36,
                "channel_width": "MHZ_80",
                "power": 20,
                "wlans": [
                    {
                        "ssid": "Corp-WiFi",
                        "network_policy_name": "Corp-Policy",
                        "ssid_security_type": "TYPE_802DOT1X",
                        "bssid": "001122334457",
                    },
                    {
                        "ssid": "Guest-WiFi",
                        "network_policy_name": "Guest-Policy",
                        "ssid_security_type": "OPEN",
                        "bssid": "001122334458",
                    },
                ],
            },
        ],
    },
    {
        "device_id": 999,  # filtered out by site_scope upstream -- not in device_names
        "radios": [{"name": "Radio1", "mode": "_11ac", "channel_width": "MHZ_20", "wlans": []}],
    },
]


def _radio_map():
    return mapper.radios_to_entities(RADIO_INFOS, device_names={111: "ap-lobby"})


def _wlans(entities):
    return [e._kw["wireless_lan"] for e in entities if "wireless_lan" in e._kw]


def _radio_interfaces(entities):
    return [e._kw["interface"] for e in entities if "interface" in e._kw]


def test_radios_to_entities_skips_devices_missing_from_device_names(stub_sdk):
    interfaces = _radio_interfaces(_radio_map())

    assert {i._kw["device"] for i in interfaces} == {"ap-lobby"}
    assert len(interfaces) == 2  # only the two radios for device_id 111


def test_radios_to_entities_maps_radio_hardware_fields(stub_sdk):
    interfaces = {i._kw["name"]: i for i in _radio_interfaces(_radio_map())}

    radio1 = interfaces["Radio1"]
    assert radio1._kw["device"] == "ap-lobby"
    assert radio1._kw["type"] == "ieee802.11ax"
    assert radio1._kw["rf_role"] == "ap"
    assert radio1._kw["tx_power"] == 17
    assert radio1._kw["primary_mac_address"] == "001122334455"
    assert radio1._kw["rf_channel_frequency"] == 2437.0  # 2407 + 5*6
    assert radio1._kw["rf_channel_width"] == 20.0
    assert radio1._kw["wireless_lans"] == ["Corp-WiFi"]
    assert radio1._kw["tags"] == ["extreme-networks", "xiq", "discovered"]

    radio2 = interfaces["Radio2"]
    assert radio2._kw["rf_channel_frequency"] == 5180.0  # 5000 + 5*36
    assert radio2._kw["rf_channel_width"] == 80.0
    assert radio2._kw["wireless_lans"] == ["Corp-WiFi", "Guest-WiFi"]


def test_radios_to_entities_dedupes_wlans_by_ssid_across_radios(stub_sdk):
    wlans = {w._kw["ssid"]: w for w in _wlans(_radio_map())}

    assert set(wlans) == {"Corp-WiFi", "Guest-WiFi"}


def test_radios_to_entities_maps_auth_type_and_status(stub_sdk):
    wlans = {w._kw["ssid"]: w for w in _wlans(_radio_map())}

    assert wlans["Corp-WiFi"]._kw["auth_type"] == "wpa-enterprise"  # TYPE_802DOT1X
    assert wlans["Guest-WiFi"]._kw["auth_type"] == "open"  # OPEN
    assert wlans["Corp-WiFi"]._kw["status"] == "active"
    assert "auth_cipher" not in wlans["Corp-WiFi"]._kw  # not fetched (see mapper docstring)


def test_radios_to_entities_carries_network_policy_custom_field_and_tags(stub_sdk):
    wlans = {w._kw["ssid"]: w for w in _wlans(_radio_map())}

    assert cf(wlans["Corp-WiFi"]._kw["custom_fields"]["xiq_network_policy"]._kw) == "Corp-Policy"
    assert wlans["Corp-WiFi"]._kw["tags"] == ["extreme-networks", "xiq", "discovered"]
