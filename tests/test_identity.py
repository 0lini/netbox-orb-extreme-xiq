"""identity.py unit tests: naming, switch detection, model mapping, locations."""

from __future__ import annotations

from orb_extreme_platformone.identity import (
    device_name,
    device_type_model_for,
    expand_location_paths,
    is_ap,
    is_switch,
    native_port_name,
    platform_name,
    resolve_location,
    role_for,
    slugify,
)


def test_native_port_name_rewrites_colon_ports_for_slash_native_oses():
    assert native_port_name("1:52", "Fabric Engine") == "1/52"
    assert native_port_name("1:52", "FABRIC ENGINE") == "1/52"
    assert native_port_name("1:52", "VOSS") == "1/52"
    assert native_port_name("2:52:1", "Fabric Engine") == "2/52/1"


def test_native_port_name_keeps_colon_native_oses_and_non_port_names():
    assert native_port_name("1:52", "Switch Engine") == "1:52"
    assert native_port_name("1:52", "EXOS") == "1:52"
    assert native_port_name("1:52", None) == "1:52"
    assert native_port_name("vlan10", "Fabric Engine") == "vlan10"
    assert native_port_name("lag 1", "Fabric Engine") == "lag 1"
    assert native_port_name("mgmt", "Fabric Engine") == "mgmt"


def test_device_name_prefers_hostname_then_serial_then_id():
    assert device_name({"host_name": "sw1", "serial_number": "SN1"}) == "sw1"
    assert device_name({"serial_number": "SN1"}) == "SN1"
    assert device_name({"device_id": 42}) == "platformone-42"


def test_is_switch_recognizes_the_assets_switch_function_enum_values():
    for function in ("Switch Engine", "Fabric Engine", "EXOS", "VOSS"):
        assert is_switch(function)
    assert not is_switch("AP")
    assert not is_switch("Appliance")
    assert not is_switch(None)


def test_is_ap_recognizes_access_points():
    assert is_ap("AP")
    assert is_ap("ap")
    assert not is_ap("Fabric Engine")
    assert not is_ap(None)


def test_platform_name_combines_os_family_and_version_into_one_value():
    assert platform_name("Fabric Engine", "9.2.1.0") == "Fabric Engine 9.2.1.0"
    assert platform_name("FABRIC ENGINE", "9.2.1.0") == "Fabric Engine 9.2.1.0"
    assert platform_name("Switch Engine", "33.2.1.5") == "Switch Engine 33.2.1.5"


def test_platform_name_tolerates_a_missing_family_or_version():
    assert platform_name("Fabric Engine", None) == "Fabric Engine"
    assert platform_name("AP", "10.6.4.0") == "10.6.4.0"
    assert platform_name(None, "10.6.4.0") == "10.6.4.0"
    assert platform_name(None, None) is None
    assert platform_name("Unknown", None) is None


def test_role_for_maps_functions_to_functional_roles():
    assert role_for("Fabric Engine") == ("Switch", "switch")
    assert role_for("Switch Engine") == ("Switch", "switch")
    assert role_for("EXOS") == ("Switch", "switch")
    assert role_for("VOSS") == ("Switch", "switch")
    assert role_for("AP") == ("Wireless AP", "wireless-ap")
    assert role_for("  Switch Engine  ") == ("Switch", "switch")


def test_role_for_passes_unlisted_functions_through_slugified():
    assert role_for("Router") == ("Router", "router")
    assert role_for(None) is None
    assert role_for("") is None
    assert role_for("   ") is None
    assert role_for("Unknown") is None
    assert role_for("!!!") is None
    assert slugify("VOSS") == "voss"
    assert slugify("!!!") == ""


def test_device_type_model_for_moves_the_fabric_engine_prefix_to_a_suffix():
    assert device_type_model_for("FabricEngine_5320_48P_8XE") == "5320-48P-8XE-FabricEngine"


def test_device_type_model_for_passes_unprefixed_codes_through():
    assert device_type_model_for("VSP_SWITCH") == "VSP_SWITCH"
    assert device_type_model_for(None) is None


def test_resolve_location_uses_the_configstate_site_and_building_floor_chain():
    location = {"site_name": "HQ", "building_name": "B1", "floor_name": "F2"}
    assert resolve_location(location, {"site_name": "Assets-Site"}) == ("HQ", ["B1", "F2"])


def test_resolve_location_skips_absent_building_floor_levels():
    assert resolve_location({"site_name": "HQ"}, {}) == ("HQ", [])
    assert resolve_location({"site_name": "HQ", "floor_name": "F2"}, {}) == ("HQ", ["F2"])


def test_resolve_location_falls_back_to_the_assets_site_then_none():
    assert resolve_location(None, {"site_name": "Assets-Site"}) == ("Assets-Site", [])
    assert resolve_location(None, {}) == (None, [])


def test_resolve_location_configstate_record_without_site_uses_assets_site():
    location = {"building_name": "B1"}
    assert resolve_location(location, {"site_name": "Assets-Site"}) == ("Assets-Site", ["B1"])


def test_expand_location_paths_orders_ancestors_first_and_dedupes():
    paths = {("HQ", ("B1", "F1")), ("HQ", ("B1", "F2")), ("Branch", ("B9",))}
    assert expand_location_paths(paths) == [
        ("Branch", ("B9",)),
        ("HQ", ("B1",)),
        ("HQ", ("B1", "F1")),
        ("HQ", ("B1", "F2")),
    ]
