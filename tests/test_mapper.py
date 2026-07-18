"""mapper.py tests against the stubbed Diode SDK (see conftest.stub_sdk).

Fixture payloads are shaped exactly like the two Platform ONE OpenAPI
specs' schemas: Assets `Device`, ConfigState `AssetLocation`,
`AssetPortConfig`, `AssetPortState`, `AssetInterfaceVlanProperties` (with
nested `AssetInterfaceVlanMap` rows), `AssetLagConfig` /
`AssetLagState` (with nested member ports), and `InferredCluster`.
"""

from __future__ import annotations

from orb_extreme_platformone import mapper
from orb_extreme_platformone.backend import INTERFACE_ID_TABLES, PORT_TABLES
from tests.conftest import PORT_CONFIG, PORT_STATE, SWITCH_ASSET, VLAN_PROPERTIES, cf


def _record(asset=SWITCH_ASSET, location=None, cs_device_id="cs-uuid-42", cs_device=None):
    return {
        "asset": asset,
        "cs_device_id": cs_device_id,
        "cs_device": cs_device,
        "location": location,
    }


def test_port_entity_table_keys_match_backend_fetches():
    """Mapper port keys must stay aligned with backend PORT_TABLES + INTERFACE_ID_TABLES."""
    assert frozenset(PORT_TABLES) | frozenset(INTERFACE_ID_TABLES) == mapper.PORT_ENTITY_TABLE_KEYS


def test_devices_to_entities_builds_site_location_chain_and_device(stub_sdk):
    location = {
        "site_name": "HQ",
        "building_name": "B1",
        "floor_name": "F2",
        "site_latitude": 48.137,
        "site_longitude": 11.575,
    }
    entities = mapper.devices_to_entities([_record(location=location)])

    site, building, floor, device = (e._kw for e in entities)
    assert site["site"]._kw == {"name": "HQ", "latitude": 48.137, "longitude": 11.575}
    assert building["location"]._kw["name"] == "B1"
    assert building["location"]._kw["parent"] is None
    assert floor["location"]._kw["name"] == "F2"
    assert floor["location"]._kw["parent"] is building["location"]
    assert device["device"]._kw["location"] is floor["location"]


def test_devices_to_entities_maps_the_assets_fields(stub_sdk):
    entities = mapper.devices_to_entities([_record()])

    device = entities[-1]._kw["device"]._kw
    assert device["name"] == "sw-idf1"
    assert device["serial"] == "SN42"
    assert device["status"] == "active"
    assert device["site"]._kw == {"name": "Assets-Site"}
    assert device["device_type"]._kw["model"] == "5320-48P-8XE-FabricEngine"
    assert device["device_type"]._kw["manufacturer"] == "Extreme Networks"
    assert device["platform"]._kw["name"] == "Fabric Engine 9.2.1.0"
    assert device["platform"]._kw["manufacturer"] == "Extreme Networks"
    # Assets reports a bare host address; do not invent /32.
    assert "primary_ip4" not in device
    assert "primary_ip6" not in device
    assert device["role"]._kw == {"name": "Switch", "slug": "switch"}
    assert cf(device["custom_fields"]["platformone_id"]._kw) == "42"
    # The ConfigState UUID stays an internal join key; it is not synced.
    assert "platformone_configstate_device_id" not in device["custom_fields"]
    assert device["tags"] == ["extreme-networks", "platform-one", "discovered"]


def test_devices_to_entities_skips_bare_primary_ip6(stub_sdk):
    asset = {**SWITCH_ASSET, "ip_address": "2001:db8::1"}
    entities = mapper.devices_to_entities([_record(asset=asset)])

    device = entities[-1]._kw["device"]._kw
    assert "primary_ip6" not in device
    assert "primary_ip4" not in device


def test_devices_to_entities_keeps_primary_ip_when_prefix_is_present(stub_sdk):
    asset = {**SWITCH_ASSET, "ip_address": "10.0.0.2/24"}
    entities = mapper.devices_to_entities([_record(asset=asset)])

    device = entities[-1]._kw["device"]._kw
    assert device["primary_ip4"] == "10.0.0.2/24"
    assert "primary_ip6" not in device


def test_devices_to_entities_keeps_primary_ip6_when_prefix_is_present(stub_sdk):
    asset = {**SWITCH_ASSET, "ip_address": "2001:db8::1/64"}
    entities = mapper.devices_to_entities([_record(asset=asset)])

    device = entities[-1]._kw["device"]._kw
    assert device["primary_ip6"] == "2001:db8::1/64"
    assert "primary_ip4" not in device


def test_devices_to_entities_uses_configstate_primary_ips_by_cs_id(stub_sdk):
    entities = mapper.devices_to_entities(
        [_record()],
        primary_ips_by_cs_id={"cs-uuid-42": {"primary_ip4": "10.0.0.2/24"}},
    )

    device = entities[-1]._kw["device"]._kw
    assert device["primary_ip4"] == "10.0.0.2/24"


def test_primary_ips_from_tables_prefers_is_primary():
    tables = _tables(
        interface_ips=[
            {
                "asset_interface_id": "if-uuid-1",
                "address": "10.0.0.2",
                "mask_length": 24,
                "is_primary": True,
            },
            {
                "asset_interface_id": "if-other",
                "address": "10.0.0.99",
                "mask_length": 24,
                "is_primary": False,
            },
        ]
    )
    assert mapper.primary_ips_from_tables(tables) == {"primary_ip4": "10.0.0.2/24"}


def test_primary_ips_from_tables_falls_back_to_management_port():
    tables = _tables(
        port_capabilities=[
            {"asset_device_id": "cs-uuid-42", "port_name": "1/1", "management_port": True},
        ],
        interface_ips=[
            {
                "asset_interface_id": "if-uuid-1",
                "address": "10.0.0.2",
                "mask_length": 24,
                "is_primary": False,
            },
            {
                "asset_interface_id": "if-other",
                "address": "10.0.0.99",
                "mask_length": 24,
            },
        ],
    )
    assert mapper.primary_ips_from_tables(tables) == {"primary_ip4": "10.0.0.2/24"}


def test_primary_ips_from_tables_matches_assets_host_when_needed():
    tables = _tables(
        port_capabilities=[],
        interface_ips=[
            {
                "asset_interface_id": "if-uuid-1",
                "address": "10.0.0.2",
                "mask_length": 24,
            },
        ],
    )
    assert mapper.primary_ips_from_tables(tables, asset_ip="10.0.0.2") == {"primary_ip4": "10.0.0.2/24"}


def test_primary_ips_from_tables_skips_bare_addresses_without_mask():
    tables = _tables(
        interface_ips=[
            {"asset_interface_id": "if-uuid-1", "address": "10.0.0.2", "is_primary": True},
        ]
    )
    assert mapper.primary_ips_from_tables(tables) == {}


def test_devices_to_entities_uses_configstate_model_and_firmware_fallbacks(stub_sdk):
    asset = {**SWITCH_ASSET, "product_type": None, "os_version": None}
    cs = {"id": "cs-uuid-42", "model_name": "FabricEngine_5520_24T", "firmware_version": "8.10.1.0"}
    entities = mapper.devices_to_entities([_record(asset=asset, cs_device=cs)])

    device = entities[-1]._kw["device"]._kw
    assert device["device_type"]._kw["model"] == "5520-24T-FabricEngine"
    assert device["platform"]._kw["name"] == "Fabric Engine 8.10.1.0"


def test_devices_to_entities_non_switch_function_platform_is_version_only(stub_sdk):
    asset = {**SWITCH_ASSET, "function": "AP"}
    entities = mapper.devices_to_entities([_record(asset=asset)])

    assert entities[-1]._kw["device"]._kw["platform"]._kw["name"] == "9.2.1.0"


def test_devices_to_entities_without_function_or_version_asserts_no_platform(stub_sdk):
    asset = {**SWITCH_ASSET, "function": None, "os_version": None}
    entities = mapper.devices_to_entities([_record(asset=asset)])

    assert "platform" not in entities[-1]._kw["device"]._kw


def test_devices_to_entities_without_function_asserts_no_role(stub_sdk):
    asset = {**SWITCH_ASSET, "function": None}
    entities = mapper.devices_to_entities([_record(asset=asset)])

    assert "role" not in entities[-1]._kw["device"]._kw


def test_devices_to_entities_unknown_function_asserts_no_role(stub_sdk):
    asset = {**SWITCH_ASSET, "function": "Unknown"}
    entities = mapper.devices_to_entities([_record(asset=asset)])

    assert "role" not in entities[-1]._kw["device"]._kw


def test_devices_to_entities_disconnected_device_is_offline(stub_sdk):
    asset = {**SWITCH_ASSET, "is_connected": False}
    entities = mapper.devices_to_entities([_record(asset=asset)])

    assert entities[-1]._kw["device"]._kw["status"] == "offline"


def test_devices_to_entities_omits_status_when_is_connected_unknown(stub_sdk):
    asset = {**SWITCH_ASSET}
    del asset["is_connected"]
    entities = mapper.devices_to_entities([_record(asset=asset)])

    assert "status" not in entities[-1]._kw["device"]._kw


def test_devices_to_entities_without_any_site_skips_the_device(stub_sdk, caplog):
    """Platform ONE assigns every device a site itself, so a device without
    one is unexpected: it is skipped instead of getting an invented site."""
    asset = {"device_id": 7, "host_name": "sw-lost", "is_connected": True}
    entities = mapper.devices_to_entities([_record(asset=asset)])

    assert entities == []
    assert "sw-lost" in caplog.text


def test_scope_devices_filters_on_the_resolved_site():
    in_scope = _record(location={"site_name": "HQ"})
    out_of_scope = _record(location={"site_name": "Branch"})

    scoped = mapper.scope_devices([in_scope, out_of_scope], site_scope={"HQ"})

    assert scoped == [in_scope]


def test_scope_devices_without_a_scope_returns_everything():
    records = [_record(), _record(location={"site_name": "HQ"})]
    assert mapper.scope_devices(records, site_scope=None) == records


def _tables(**overrides):
    tables = {
        "port_configs": [PORT_CONFIG],
        "port_states": [PORT_STATE],
        "vlan_properties": [VLAN_PROPERTIES],
    }
    tables.update(overrides)
    return tables


def test_ports_to_entities_warns_on_duplicate_first_row_join(stub_sdk, caplog):
    dup = {**PORT_CONFIG, "enabled": False}
    entities = mapper.ports_to_entities(
        _tables(port_configs=[PORT_CONFIG, dup], vlan_properties=[]),
        device="sw-idf1",
    )

    assert entities[0]._kw["interface"]._kw["enabled"] is True
    assert "Multiple port_configs rows share join key" in caplog.text


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
    assert cf(port["custom_fields"]["platformone_id"]._kw) == "if-uuid-1"


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


def test_ports_to_entities_maps_mgmt_only_from_capabilities(stub_sdk):
    caps = [
        {
            "asset_device_id": "cs-uuid-42",
            "port_name": "1/1",
            "management_port": True,
        }
    ]
    entities = mapper.ports_to_entities(_tables(vlan_properties=[], port_capabilities=caps), device="sw-idf1")

    assert entities[0]._kw["interface"]._kw["mgmt_only"] is True


def test_ports_to_entities_capabilities_scoped_per_device(stub_sdk, caplog):
    """Same port_name on two devices must not share a capability row.

    A mixed capabilities list (as if backend bucketing were skipped) still
    joins each port to its own asset_device_id; the other device's
    management_port must not leak across.
    """
    caps = [
        {"asset_device_id": "cs-uuid-OTHER", "port_name": "1/1", "management_port": True},
        {"asset_device_id": "cs-uuid-42", "port_name": "1/1", "management_port": False},
    ]
    entities = mapper.ports_to_entities(_tables(vlan_properties=[], port_capabilities=caps), device="sw-idf1")

    assert entities[0]._kw["interface"]._kw["mgmt_only"] is False
    assert "Multiple port_capabilities rows share port_name" not in caplog.text


def test_ports_to_entities_warns_on_per_device_capability_duplicates(stub_sdk, caplog):
    caps = [
        {"asset_device_id": "cs-uuid-42", "port_name": "1/1", "management_port": True},
        {"asset_device_id": "cs-uuid-42", "port_name": "1/1", "management_port": False},
    ]
    entities = mapper.ports_to_entities(_tables(vlan_properties=[], port_capabilities=caps), device="sw-idf1")

    assert entities[0]._kw["interface"]._kw["mgmt_only"] is True
    assert "Multiple port_capabilities rows share port_name '1/1' on device 'cs-uuid-42'" in caplog.text


def test_ports_to_entities_maps_poe_mode_pse_when_supported(stub_sdk):
    poe_state = {
        "device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-1",
        "interface_name": "1/1",
        "supported": True,
    }
    poe_config = {
        "asset_interface_id": "if-uuid-1",
        "interface_name": "1/1",
        "enable": False,
        "classification": 1,
    }
    entities = mapper.ports_to_entities(
        _tables(vlan_properties=[], poe_states=[poe_state], poe_configs=[poe_config]),
        device="sw-idf1",
    )

    port = entities[0]._kw["interface"]._kw
    assert port["poe_mode"] == "pse"
    assert "poe_type" not in port


def test_ports_to_entities_omits_poe_when_not_supported_or_enabled(stub_sdk):
    poe_state = {
        "device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-1",
        "supported": False,
    }
    poe_config = {"asset_interface_id": "if-uuid-1", "enable": False}
    entities = mapper.ports_to_entities(
        _tables(vlan_properties=[], poe_states=[poe_state], poe_configs=[poe_config]),
        device="sw-idf1",
    )

    assert "poe_mode" not in entities[0]._kw["interface"]._kw


def test_ports_to_entities_falls_back_to_native_vlan_when_no_vlan_properties(stub_sdk):
    config = {**PORT_CONFIG, "native_vlan": 99, "port_mode": True}
    entities = mapper.ports_to_entities(_tables(port_configs=[config], vlan_properties=[]), device="sw-idf1")

    port = entities[0]._kw["interface"]._kw
    assert port["untagged_vlan"]._kw == {"vid": 99}
    assert port["mode"] == "tagged"


def test_ports_to_entities_vlan_properties_win_over_native_vlan_fallback(stub_sdk):
    config = {**PORT_CONFIG, "native_vlan": 99, "port_mode": True}
    entities = mapper.ports_to_entities(_tables(port_configs=[config]), device="sw-idf1")

    port = entities[0]._kw["interface"]._kw
    assert port["untagged_vlan"]._kw == {"vid": 10}
    assert port["mode"] == "tagged"


def test_ports_to_entities_rewrites_colon_ports_to_native_notation(stub_sdk):
    """ConfigState reports slot:port for every OS; on Fabric Engine the ports
    must come out slash-native with capability and LAG-member joins intact."""
    config = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-52",
        "name": "1:52",
        "enabled": True,
    }
    caps = [{"asset_device_id": "cs-uuid-42", "port_name": "1:52", "management_port": True}]
    lag_config = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-lag",
        "name": "lag 1",
        "member_ports": [{"interface_name": "1:52"}],
    }
    entities = mapper.ports_to_entities(
        _tables(
            port_configs=[config],
            port_states=[],
            vlan_properties=[],
            port_capabilities=caps,
            lag_configs=[lag_config],
        ),
        device="sw-idf1",
        function="Fabric Engine",
    )

    lag = entities[0]._kw["interface"]._kw
    port = entities[1]._kw["interface"]._kw
    assert lag["name"] == "lag 1"
    assert port["name"] == "1/52"
    assert port["mgmt_only"] is True
    assert port["lag"]._kw["name"] == "lag 1"
    # Caller rows stay untouched (tables are copied, not mutated).
    assert config["name"] == "1:52"


def test_ports_to_entities_keeps_colon_ports_for_switch_engine(stub_sdk):
    config = {"asset_device_id": "cs-uuid-42", "asset_interface_id": "if-uuid-52", "name": "1:52"}
    entities = mapper.ports_to_entities(
        _tables(port_configs=[config], port_states=[], vlan_properties=[]),
        device="sw-idf1",
        function="Switch Engine",
    )

    assert entities[0]._kw["interface"]._kw["name"] == "1:52"


def test_ports_to_entities_emits_interface_ip_addresses(stub_sdk):
    ips = [
        {
            "asset_interface_id": "if-uuid-1",
            "address": "10.0.0.2",
            "mask_length": 24,
            "ip_version": 4,
            "is_primary": True,
        },
        {
            "asset_interface_id": "if-uuid-1",
            "address": "2001:db8::2",
            "mask_length": 64,
            "ip_version": 6,
            "is_primary": False,
        },
    ]
    entities = mapper.ports_to_entities(_tables(vlan_properties=[], interface_ips=ips), device="sw-idf1")

    assert entities[0]._kw["interface"]._kw["name"] == "1/1"
    ip_entities = [e._kw["ip_address"]._kw for e in entities if "ip_address" in e._kw]
    addresses = {ip["address"] for ip in ip_entities}
    assert addresses == {"10.0.0.2/24", "2001:db8::2/64"}
    assert all(ip["assigned_object_interface"]._kw["name"] == "1/1" for ip in ip_entities)
    assert all(ip["assigned_object_interface"]._kw["device"] == "sw-idf1" for ip in ip_entities)


def test_ports_to_entities_emits_svi_ips_via_interface_name(stub_sdk):
    """An IP on an interface with no port/LAG row (e.g. a VLAN/SVI interface)
    emits a minimal Interface, then the IPAddress assigned to it."""
    ips = [
        {
            "asset_interface_id": "if-svi",
            "interface_name": "vlan10",
            "address": "10.0.10.1",
            "mask_length": 24,
        }
    ]
    entities = mapper.ports_to_entities(_tables(vlan_properties=[], interface_ips=ips), device="sw-idf1")

    # Physical port 1/1 from default fixtures, then the SVI interface + its IP.
    iface_entities = [e._kw["interface"]._kw for e in entities if "interface" in e._kw]
    svi = next(i for i in iface_entities if i["name"] == "vlan10")
    assert svi["device"] == "sw-idf1"
    assert "type" not in svi
    assert cf(svi["custom_fields"]["platformone_id"]._kw) == "if-svi"

    ip_entities = [e._kw["ip_address"]._kw for e in entities if "ip_address" in e._kw]
    assert [ip["address"] for ip in ip_entities] == ["10.0.10.1/24"]
    assert ip_entities[0]["assigned_object_interface"]._kw["name"] == "vlan10"


def test_ports_to_entities_skips_interface_ips_without_mask_length(stub_sdk):
    ips = [
        {
            "asset_interface_id": "if-uuid-1",
            "address": "10.0.0.2",
            "is_primary": True,
        }
    ]
    entities = mapper.ports_to_entities(_tables(vlan_properties=[], interface_ips=ips), device="sw-idf1")

    assert not [e for e in entities if "ip_address" in e._kw]


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


def test_ports_to_entities_omits_reserved_untagged_vlan(stub_sdk):
    """Untagged VID 4094 (Extreme reserved) is omitted from the interface."""
    vlan = {
        **VLAN_PROPERTIES,
        "port_vlan": 4094,
        "vlans": [{"vlan_number": 10}, {"vlan_number": 4094}],
    }
    entities = mapper.ports_to_entities(_tables(vlan_properties=[vlan]), device="sw-idf1")
    port = entities[0]._kw["interface"]._kw
    assert "untagged_vlan" not in port
    assert [v._kw["vid"] for v in port["tagged_vlans"]] == [10]
    assert port["mode"] == "tagged"


def test_ports_to_entities_strips_reserved_tagged_vids(stub_sdk):
    """Tagged list drops Extreme reserved VIDs; user VIDs and mode remain."""
    vlan = {
        **VLAN_PROPERTIES,
        "port_vlan": 10,
        "vlans": [
            {"vlan_number": 10},
            {"vlan_number": 20},
            {"vlan_number": 4060},
            {"vlan_number": 4094},
        ],
    }
    entities = mapper.ports_to_entities(_tables(vlan_properties=[vlan]), device="sw-idf1")
    port = entities[0]._kw["interface"]._kw
    assert port["untagged_vlan"]._kw == {"vid": 10}
    assert [v._kw["vid"] for v in port["tagged_vlans"]] == [20]
    assert port["mode"] == "tagged"


def test_ports_to_entities_only_reserved_vlan_asserts_no_vlan_or_mode():
    """A port whose only membership is reserved VID 4094 gets no VLAN/mode."""
    vlan = {
        **VLAN_PROPERTIES,
        "port_vlan": 4094,
        "vlans": [{"vlan_number": 4094}],
    }
    assert mapper._vlan_fields([vlan]) == {}


def test_ports_to_entities_omits_reserved_native_vlan_fallback(stub_sdk):
    """native_vlan fallback also strips Extreme reserved VIDs."""
    config = {**PORT_CONFIG, "native_vlan": 4094, "port_mode": True}
    entities = mapper.ports_to_entities(
        _tables(port_configs=[config], vlan_properties=[]),
        device="sw-idf1",
    )
    port = entities[0]._kw["interface"]._kw
    assert "untagged_vlan" not in port
    assert "mode" not in port


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


LAG_CONFIG = {
    "id": "lag-cfg-1",
    "asset_device_id": "cs-uuid-42",
    "asset_interface_id": "lag-if-1",
    "lag_number": "1",
    "name": "lag1",
    "enabled": True,
    "member_ports": [
        {"asset_lag_config_id": "lag-cfg-1", "interface_name": "1/1"},
        {"asset_lag_config_id": "lag-cfg-1", "interface_name": "1/2"},
    ],
}

LAG_STATE = {
    "id": "lag-st-1",
    "asset_device_id": "cs-uuid-42",
    "asset_interface_id": "lag-if-1",
    "lag_number": "1",
    "name": "lag1",
}


def test_ports_to_entities_maps_lag_parent_and_member_refs(stub_sdk):
    port2 = {**PORT_CONFIG, "asset_interface_id": "if-uuid-2", "name": "1/2"}
    entities = mapper.ports_to_entities(
        _tables(
            port_configs=[PORT_CONFIG, port2],
            port_states=[],
            vlan_properties=[],
            lag_configs=[LAG_CONFIG],
            lag_states=[LAG_STATE],
        ),
        device="sw-idf1",
    )

    ports = {e._kw["interface"]._kw["name"]: e._kw["interface"]._kw for e in entities}
    assert [e._kw["interface"]._kw["name"] for e in entities][0] == "lag1"
    assert ports["lag1"]["type"] == "lag"
    assert ports["lag1"]["enabled"] is True
    assert cf(ports["lag1"]["custom_fields"]["platformone_id"]._kw) == "lag-if-1"
    assert ports["1/1"]["lag"]._kw == {"device": "sw-idf1", "name": "lag1"}
    assert ports["1/2"]["lag"]._kw == {"device": "sw-idf1", "name": "lag1"}


def test_ports_to_entities_lag_without_name_uses_lag_number(stub_sdk):
    lag = {**LAG_CONFIG, "name": None, "member_ports": []}
    entities = mapper.ports_to_entities(
        _tables(port_configs=[], port_states=[], vlan_properties=[], lag_configs=[lag], lag_states=[]),
        device="sw-idf1",
    )

    assert entities[0]._kw["interface"]._kw["name"] == "lag-1"
    assert entities[0]._kw["interface"]._kw["type"] == "lag"


def test_ports_to_entities_member_only_from_lag_still_emits_interface(stub_sdk):
    """A member named only on the LAG still becomes an Interface with lag set."""
    entities = mapper.ports_to_entities(
        _tables(
            port_configs=[],
            port_states=[],
            vlan_properties=[],
            lag_configs=[LAG_CONFIG],
            lag_states=[],
        ),
        device="sw-idf1",
    )

    ports = {e._kw["interface"]._kw["name"]: e._kw["interface"]._kw for e in entities}
    assert set(ports) == {"lag1", "1/1", "1/2"}
    assert ports["1/1"]["lag"]._kw["name"] == "lag1"
    assert ports["1/1"].get("type") is None


def test_ports_to_entities_skips_lag_row_duplicated_in_port_tables(stub_sdk):
    """If AssetPortConfig also returns the LAG's asset_interface_id, emit type=lag once."""
    lag_as_port = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "lag-if-1",
        "name": "lag1",
        "enabled": True,
        "description": "core lag",
        "native_vlan": 99,
        "port_mode": True,
    }
    lag_as_state = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "lag-if-1",
        "name": "lag1",
        "oper_state": 1,
        "mac_address": "aa:bb:cc:dd:ee:99",
        "oper_speed": 4,
        "oper_duplex": 1,
        "connector_type": 1,
    }
    entities = mapper.ports_to_entities(
        _tables(
            port_configs=[lag_as_port],
            port_states=[lag_as_state],
            vlan_properties=[],
            lag_configs=[{**LAG_CONFIG, "member_ports": []}],
            lag_states=[],
        ),
        device="sw-idf1",
    )

    ports = [e._kw["interface"]._kw for e in entities]
    assert len(ports) == 1
    assert ports[0]["name"] == "lag1"
    assert ports[0]["type"] == "lag"
    assert ports[0]["description"] == "core lag"
    assert ports[0]["mark_connected"] is True
    assert ports[0]["primary_mac_address"] == "aa:bb:cc:dd:ee:99"
    assert ports[0]["mode"] == "tagged"
    assert ports[0]["untagged_vlan"]._kw["vid"] == 99
    assert "speed" not in ports[0]
    assert "duplex" not in ports[0]


def test_ports_to_entities_lag_applies_vlan_trunk_from_vlan_properties(stub_sdk):
    """Trunk VLANs on the LAG parent come from vlan-properties on its interface id."""
    vlan_on_lag = {
        "asset_interface_id": "lag-if-1",
        "port_vlan": 10,
        "vlans": [{"vlan_number": 10}, {"vlan_number": 20}, {"vlan_number": 30}],
    }
    entities = mapper.ports_to_entities(
        _tables(
            port_configs=[],
            port_states=[],
            vlan_properties=[vlan_on_lag],
            lag_configs=[{**LAG_CONFIG, "member_ports": []}],
            lag_states=[],
        ),
        device="sw-idf1",
    )

    lag = entities[0]._kw["interface"]._kw
    assert lag["type"] == "lag"
    assert lag["mode"] == "tagged"
    assert lag["untagged_vlan"]._kw["vid"] == 10
    assert [v._kw["vid"] for v in lag["tagged_vlans"]] == [20, 30]


def test_ports_to_entities_lag_joins_poe_and_ip_like_physical_ports(stub_sdk):
    """PoE + IP joins use the LAG's asset_interface_id the same way as ports."""
    poe_state = {"asset_interface_id": "lag-if-1", "supported": True}
    poe_config = {"asset_interface_id": "lag-if-1", "enable": True}
    ips = [
        {
            "asset_interface_id": "lag-if-1",
            "interface_name": "lag1",
            "address": "10.0.0.1",
            "mask_length": 24,
        }
    ]
    entities = mapper.ports_to_entities(
        _tables(
            port_configs=[],
            port_states=[],
            vlan_properties=[],
            lag_configs=[{**LAG_CONFIG, "member_ports": []}],
            lag_states=[],
            poe_states=[poe_state],
            poe_configs=[poe_config],
            interface_ips=ips,
        ),
        device="sw-idf1",
    )

    interfaces = [e._kw["interface"]._kw for e in entities if "interface" in e._kw]
    ips_out = [e._kw["ip_address"]._kw for e in entities if "ip_address" in e._kw]
    assert interfaces[0]["type"] == "lag"
    assert interfaces[0]["poe_mode"] == "pse"
    assert cf(interfaces[0]["custom_fields"]["platformone_id"]._kw) == "lag-if-1"
    assert "lag_number" not in interfaces[0].get("custom_fields", {})
    assert ips_out[0]["address"] == "10.0.0.1/24"
    assert ips_out[0]["assigned_object_interface"]._kw["name"] == "lag1"


def test_ports_to_entities_ignores_unmapped_lacp_fields_on_lag(stub_sdk):
    """LACP mode/key/algo have no Diode target; do not invent description or mode."""
    lag = {
        **LAG_CONFIG,
        "member_ports": [],
        "mode": 2,
        "lacp_key": "100",
        "load_balance_algo": 1,
        "dynamic": True,
    }
    entities = mapper.ports_to_entities(
        _tables(port_configs=[], port_states=[], vlan_properties=[], lag_configs=[lag], lag_states=[]),
        device="sw-idf1",
    )

    kwargs = entities[0]._kw["interface"]._kw
    assert kwargs["type"] == "lag"
    assert "mode" not in kwargs  # 802.1Q mode only; not LACP mode
    assert "description" not in kwargs
    assert "lacp_key" not in kwargs


SWITCH_ASSET_PEER = {
    "device_id": 43,
    "host_name": "sw-idf2",
    "serial_number": "SN43",
    "mac_address": "aabbccddee00",
    "product_type": "FabricEngine_5320_48P_8XE",
    "function": "Fabric Engine",
    "os_version": "9.2.1.0",
    "is_connected": True,
    "ip_address": "10.0.0.3",
    "site_name": "Assets-Site",
}

INFERRED_CLUSTER = {
    "id": "cluster-uuid-1",
    "device_one_id": "cs-uuid-42",
    "device_two_id": "cs-uuid-43",
    "device_one_peer_name": "peer-b",
    "device_two_peer_name": "peer-a",
    "type": 1,
}


def test_virtual_chassis_to_entities_maps_inferred_cluster(stub_sdk):
    records_by_cs_id = {
        "cs-uuid-42": _record(),
        "cs-uuid-43": _record(asset=SWITCH_ASSET_PEER, cs_device_id="cs-uuid-43"),
    }

    entities, memberships = mapper.virtual_chassis_to_entities(
        [INFERRED_CLUSTER],
        records_by_cs_id=records_by_cs_id,
    )

    assert len(entities) == 1
    vc = entities[0]._kw["virtual_chassis"]._kw
    # Peer names are sorted for a stable name when primary/backup flips.
    assert vc["name"] == "peer-a / peer-b"
    assert vc["master"] == "sw-idf1"
    assert "description" not in vc
    assert vc["tags"] == ["extreme-networks", "platform-one", "discovered"]
    assert cf(vc["custom_fields"]["platformone_id"]._kw) == "cluster-uuid-1"
    assert "domain" not in vc
    assert "comments" not in vc
    assert memberships == {
        "cs-uuid-42": {"name": "peer-a / peer-b", "position": 1},
        "cs-uuid-43": {"name": "peer-a / peer-b", "position": 2},
    }


def test_virtual_chassis_to_entities_skips_partial_clusters(stub_sdk):
    """Both members must be in scope; a half-known pair is skipped."""
    entities, memberships = mapper.virtual_chassis_to_entities(
        [INFERRED_CLUSTER],
        records_by_cs_id={"cs-uuid-42": _record()},
    )

    assert entities == []
    assert memberships == {}


def test_virtual_chassis_falls_back_to_device_names_without_peer_names(stub_sdk):
    cluster = {
        "id": "cluster-uuid-2",
        "device_one_id": "cs-uuid-42",
        "device_two_id": "cs-uuid-43",
    }
    records_by_cs_id = {
        "cs-uuid-42": _record(),
        "cs-uuid-43": _record(asset=SWITCH_ASSET_PEER, cs_device_id="cs-uuid-43"),
    }

    entities, memberships = mapper.virtual_chassis_to_entities(
        [cluster],
        records_by_cs_id=records_by_cs_id,
    )

    assert entities[0]._kw["virtual_chassis"]._kw["name"] == "sw-idf1 / sw-idf2"
    assert memberships["cs-uuid-42"]["name"] == "sw-idf1 / sw-idf2"


def test_virtual_chassis_ignores_identical_placeholder_peer_names(stub_sdk):
    """Fabric often reports peer_name 'Default' on both members -- that must not
    become the NetBox VirtualChassis name for every cluster."""
    cluster = {
        **INFERRED_CLUSTER,
        "device_one_peer_name": "Default",
        "device_two_peer_name": "Default",
    }
    records_by_cs_id = {
        "cs-uuid-42": _record(),
        "cs-uuid-43": _record(asset=SWITCH_ASSET_PEER, cs_device_id="cs-uuid-43"),
    }

    entities, _ = mapper.virtual_chassis_to_entities([cluster], records_by_cs_id=records_by_cs_id)

    assert entities[0]._kw["virtual_chassis"]._kw["name"] == "sw-idf1 / sw-idf2"


def test_virtual_chassis_warns_on_duplicate_computed_names(stub_sdk, caplog):
    """Colliding names are emitted as-is (no invented suffix): the unique
    platformone_id custom field rejects the merge at ingest, and the
    worker warns so the upstream data problem is visible in the logs."""
    twin = {
        "device_id": 44,
        "host_name": "sw-idf1",
        "serial_number": "SN44",
        "mac_address": "aabbccddee01",
        "product_type": "FabricEngine_5320_48P_8XE",
        "function": "Fabric Engine",
        "is_connected": True,
        "site_name": "Assets-Site",
    }
    twin_peer = {**SWITCH_ASSET_PEER, "device_id": 45, "host_name": "sw-idf2", "serial_number": "SN45"}
    clusters = [
        INFERRED_CLUSTER,
        {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "device_one_id": "cs-uuid-44",
            "device_two_id": "cs-uuid-45",
            "device_one_peer_name": "peer-b",
            "device_two_peer_name": "peer-a",
        },
    ]
    records_by_cs_id = {
        "cs-uuid-42": _record(),
        "cs-uuid-43": _record(asset=SWITCH_ASSET_PEER, cs_device_id="cs-uuid-43"),
        "cs-uuid-44": _record(asset=twin, cs_device_id="cs-uuid-44"),
        "cs-uuid-45": _record(asset=twin_peer, cs_device_id="cs-uuid-45"),
    }

    entities, memberships = mapper.virtual_chassis_to_entities(clusters, records_by_cs_id=records_by_cs_id)

    names = [e._kw["virtual_chassis"]._kw["name"] for e in entities]
    assert names == ["peer-a / peer-b", "peer-a / peer-b"]
    assert memberships["cs-uuid-44"]["name"] == "peer-a / peer-b"
    assert "Duplicate VirtualChassis name" in caplog.text
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in caplog.text


def test_devices_to_entities_attaches_virtual_chassis_membership(stub_sdk):
    peer = _record(asset=SWITCH_ASSET_PEER, cs_device_id="cs-uuid-43")
    vc_entities, memberships = mapper.virtual_chassis_to_entities(
        [INFERRED_CLUSTER],
        records_by_cs_id={"cs-uuid-42": _record(), "cs-uuid-43": peer},
    )

    entities = mapper.devices_to_entities(
        [_record(), peer],
        virtual_chassis_entities=vc_entities,
        vc_memberships=memberships,
    )

    kinds = [next(iter(e._kw)) for e in entities]
    assert kinds == ["site", "virtual_chassis", "device", "device"]
    devices = {e._kw["device"]._kw["name"]: e._kw["device"]._kw for e in entities if "device" in e._kw}
    assert devices["sw-idf1"]["virtual_chassis"]._kw == {"name": "peer-a / peer-b"}
    assert devices["sw-idf1"]["vc_position"] == 1
    assert devices["sw-idf2"]["vc_position"] == 2
    assert entities[1]._kw["virtual_chassis"]._kw["master"] == "sw-idf1"
