"""Unit tests for identity.py: location flattening, site resolution, naming, roles."""

from __future__ import annotations

from orb_extreme_xiq.identity import (
    build_location_index,
    device_name,
    is_ap,
    is_switch,
    resolve_site_name,
    role_for,
)


def test_build_location_index_flattens_tree_to_flat_names():
    tree = [
        {
            "id": 1,
            "name": "HQ",
            "children": [
                {
                    "id": 2,
                    "name": "Floor 1",
                    "children": [{"id": 3, "name": "Wing A", "children": []}],
                },
            ],
        }
    ]
    index = build_location_index(tree)

    assert index == {1: "HQ", 2: "Floor 1", 3: "Wing A"}


def test_resolve_site_name_maps_location_directly_to_site():
    index = build_location_index(
        [{"id": 1, "name": "HQ", "children": [{"id": 2, "name": "Floor 1", "children": []}]}]
    )

    assert resolve_site_name(2, index, "XIQ-Unmapped") == "Floor 1"
    assert resolve_site_name(1, index, "XIQ-Unmapped") == "HQ"


def test_resolve_site_name_falls_back_for_unknown_location():
    assert resolve_site_name(999, {}, "XIQ-Unmapped") == "XIQ-Unmapped"
    assert resolve_site_name(None, {}, "XIQ-Unmapped") == "XIQ-Unmapped"


def test_device_name_prefers_hostname_then_falls_back():
    assert device_name({"hostname": "ap-1", "serial_number": "SN1"}) == "ap-1"
    assert device_name({"serial_number": "SN1"}) == "SN1"
    assert device_name({"mac_address": "AA:BB:CC:00:00:11"}) == "AA:BB:CC:00:00:11"
    assert device_name({"id": 42}) == "xiq-42"


def test_device_name_serial_source_prefers_serial_over_hostname():
    device = {"hostname": "ap-1", "serial_number": "SN1"}
    assert device_name(device, name_source="serial") == "SN1"


def test_role_for_known_and_unknown_device_functions():
    assert role_for("AP") == "Wireless AP"
    assert role_for("SWITCH") == "Switch"
    assert role_for("SWITCH_HAC") == "Switch"
    assert role_for("ROUTER") == "router"
    assert role_for("SOMETHING_NEW") == "network-device"
    assert role_for(None) == "network-device"


def test_is_switch_matches_every_switch_device_function_case_insensitively():
    assert is_switch("SWITCH")
    assert is_switch("switch_hac")
    assert is_switch("SWITCH_DELL")
    assert not is_switch("AP")
    assert not is_switch("ROUTER")
    assert not is_switch(None)


def test_is_ap_matches_only_the_ap_device_function_case_insensitively():
    assert is_ap("AP")
    assert is_ap("ap")
    assert not is_ap("SWITCH")
    assert not is_ap("ROUTER")
    assert not is_ap(None)
