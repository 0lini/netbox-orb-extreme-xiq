"""Unit tests for identity.py: location flattening, site resolution, naming, roles."""

from __future__ import annotations

from orb_extreme_xiq.identity import (
    build_location_index,
    device_name,
    resolve_site_name,
    role_for,
)


def test_build_location_index_flattens_tree_to_root_name():
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

    assert index[1] == {"name": "HQ", "root_name": "HQ", "path": ["HQ"]}
    assert index[2]["root_name"] == "HQ"
    assert index[3]["root_name"] == "HQ"
    assert index[3]["path"] == ["HQ", "Floor 1", "Wing A"]


def test_resolve_site_name_uses_mapping_or_default():
    index = build_location_index(
        [{"id": 1, "name": "HQ", "children": [{"id": 2, "name": "Floor 1", "children": []}]}]
    )

    assert resolve_site_name(2, index, {"HQ": "Corporate-HQ"}, "XIQ-Unmapped") == ("Corporate-HQ", "HQ")
    assert resolve_site_name(2, index, {}, "XIQ-Unmapped") == ("XIQ-Unmapped", "HQ")


def test_resolve_site_name_falls_back_for_unknown_location():
    assert resolve_site_name(999, {}, {"HQ": "Corporate-HQ"}, "XIQ-Unmapped") == ("XIQ-Unmapped", None)
    assert resolve_site_name(None, {}, {"HQ": "Corporate-HQ"}, "XIQ-Unmapped") == ("XIQ-Unmapped", None)


def test_device_name_prefers_hostname_then_falls_back():
    assert device_name({"hostname": "ap-1", "serial_number": "SN1"}) == "ap-1"
    assert device_name({"serial_number": "SN1"}) == "SN1"
    assert device_name({"mac_address": "AA:BB:CC:00:00:11"}) == "AA:BB:CC:00:00:11"
    assert device_name({"id": 42}) == "xiq-42"


def test_device_name_serial_source_prefers_serial_over_hostname():
    device = {"hostname": "ap-1", "serial_number": "SN1"}
    assert device_name(device, name_source="serial") == "SN1"


def test_role_for_known_and_unknown_device_functions():
    assert role_for("AP") == "wireless-ap"
    assert role_for("SWITCH") == "network-switch"
    assert role_for("SWITCH_HAC") == "network-switch"
    assert role_for("ROUTER") == "router"
    assert role_for("SOMETHING_NEW") == "network-device"
    assert role_for(None) == "network-device"
