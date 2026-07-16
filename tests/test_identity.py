"""identity.py unit tests: naming, switch detection, model mapping, locations."""

from __future__ import annotations

from orb_extreme_platformone.identity import (
    device_name,
    device_type_model_for,
    expand_location_paths,
    is_switch,
    platform_for,
    resolve_location,
)


def test_device_name_prefers_hostname_then_serial_then_mac_then_id():
    assert device_name({"host_name": "sw1", "serial_number": "SN1"}) == "sw1"
    assert device_name({"serial_number": "SN1"}) == "SN1"
    assert device_name({"mac_address": "aabbccddeeff"}) == "aabbccddeeff"
    assert device_name({"device_id": 42}) == "platformone-42"


def test_device_name_serial_source_falls_back_to_hostname_without_a_serial():
    assert device_name({"host_name": "sw1", "serial_number": "SN1"}, "serial") == "SN1"
    assert device_name({"host_name": "sw1"}, "serial") == "sw1"


def test_is_switch_recognizes_the_assets_switch_function_enum_values():
    for function in ("Switch Engine", "Fabric Engine", "EXOS", "VOSS"):
        assert is_switch(function)
    assert not is_switch("AP")
    assert not is_switch("Appliance")
    assert not is_switch(None)


def test_platform_for_maps_switch_os_functions_to_canonical_names():
    assert platform_for("Fabric Engine") == "Fabric Engine"
    assert platform_for("FABRIC ENGINE") == "Fabric Engine"
    assert platform_for("Switch Engine") == "Switch Engine"
    assert platform_for("EXOS") == "EXOS"
    assert platform_for("VOSS") == "VOSS"


def test_platform_for_asserts_nothing_for_non_os_functions():
    assert platform_for("AP") is None
    assert platform_for("Appliance") is None
    assert platform_for("Unknown") is None
    assert platform_for(None) is None


def test_device_type_model_for_moves_the_fabric_engine_prefix_to_a_suffix():
    assert device_type_model_for("FabricEngine_5320_48P_8XE") == "5320-48P-8XE-FabricEngine"


def test_device_type_model_for_passes_unprefixed_codes_through():
    assert device_type_model_for("VSP_SWITCH") == "VSP_SWITCH"
    assert device_type_model_for(None) is None


def test_resolve_location_uses_the_configstate_site_and_building_floor_chain():
    location = {"site_name": "HQ", "building_name": "B1", "floor_name": "F2"}
    assert resolve_location(location, {"site_name": "Assets-Site"}, "Fallback") == ("HQ", ["B1", "F2"])


def test_resolve_location_skips_absent_building_floor_levels():
    assert resolve_location({"site_name": "HQ"}, {}, "Fallback") == ("HQ", [])
    assert resolve_location({"site_name": "HQ", "floor_name": "F2"}, {}, "Fallback") == ("HQ", ["F2"])


def test_resolve_location_falls_back_to_the_assets_site_then_the_default():
    assert resolve_location(None, {"site_name": "Assets-Site"}, "Fallback") == ("Assets-Site", [])
    assert resolve_location(None, {}, "Fallback") == ("Fallback", [])


def test_resolve_location_configstate_record_without_site_uses_assets_site():
    location = {"building_name": "B1"}
    assert resolve_location(location, {"site_name": "Assets-Site"}, "Fallback") == ("Assets-Site", ["B1"])


def test_expand_location_paths_orders_ancestors_first_and_dedupes():
    paths = {("HQ", ("B1", "F1")), ("HQ", ("B1", "F2")), ("Branch", ("B9",))}
    assert expand_location_paths(paths) == [
        ("Branch", ("B9",)),
        ("HQ", ("B1",)),
        ("HQ", ("B1", "F1")),
        ("HQ", ("B1", "F2")),
    ]
