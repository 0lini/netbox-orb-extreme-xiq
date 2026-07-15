"""End-to-end Backend.run() test against the real worker/diode-sdk contracts.

Unlike test_mapper.py, this deliberately does NOT stub the Diode SDK -- it
exercises the real protobuf Entity/Device/Site classes plus the real
worker.models.Policy/Config, to catch drift against the installed
netboxlabs-diode-sdk / netboxlabs-orb-worker versions early.

Platform ONE itself is mocked at the HTTP boundary with `responses`
(client.py talks plain `requests`).
"""

from __future__ import annotations

import json

import responses
from worker.models import Config, Policy

from orb_extreme_platformone.backend import Backend
from orb_extreme_platformone.client import DEFAULT_BASE_URL

ASSETS_URL = f"{DEFAULT_BASE_URL}/assets/v1/devices"


def _cs_url(table: str) -> str:
    return f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-{table}"


def _mock_assets(devices: list[dict]):
    responses.add(
        responses.POST,
        ASSETS_URL,
        json={"data": devices, "page": 1, "total_pages": 1, "total_count": len(devices)},
        status=200,
    )


def _mock_cs(table: str, key: str, records: list[dict], status: int = 200):
    body = {key: records, "Pagination": {"total_pages": 1}} if status == 200 else {"error": "boom"}
    responses.add(responses.POST, _cs_url(table), json=body, status=status)


def _policy(**config_overrides) -> Policy:
    config = Config(
        package="orb_extreme_platformone",
        BOOTSTRAP=False,
        PLATFORMONE_API_TOKEN="tok",
        default_site="PlatformONE-Unmapped",
        **config_overrides,
    )
    return Policy(config=config, scope=config_overrides.get("scope", {"sites": ["*"]}))


SWITCH_ASSET = {
    "device_id": 42,
    "host_name": "sw-idf1",
    "serial_number": "SN42",
    "mac_address": "aabbccddeeff",
    "product_type": "FabricEngine_5320_48P_8XE",
    "function": "Fabric Engine",
    "is_connected": True,
    "site_name": "Assets-Site",
}

CS_SWITCH = {"id": "cs-uuid-42", "serial_number": "SN42", "base_mac_address": "AA:BB:CC:DD:EE:FF"}


def test_describe_reports_stable_identity():
    metadata = Backend.describe()
    assert metadata.app_name == "netbox-orb-extreme-platformone"
    assert metadata.name == "orb_extreme_platformone"


@responses.activate
def test_run_produces_site_location_device_and_interface_entities():
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_cs(
        "asset-location",
        "AssetLocation",
        [
            {
                "asset_device_id": "cs-uuid-42",
                "site_name": "HQ",
                "building_name": "B1",
                "floor_name": "F2",
            }
        ],
    )
    _mock_cs(
        "asset-port-config",
        "AssetPortConfig",
        [
            {
                "asset_device_id": "cs-uuid-42",
                "asset_interface_id": "if-1",
                "name": "1/1",
                "enabled": True,
                "description": "uplink",
            }
        ],
    )
    _mock_cs(
        "asset-port-state",
        "AssetPortState",
        [
            {
                "asset_device_id": "cs-uuid-42",
                "asset_interface_id": "if-1",
                "name": "1/1",
                "oper_state": 1,
                "oper_speed": 4,
                "oper_duplex": 2,
                "connector_type": 1,
            }
        ],
    )
    _mock_cs(
        "asset-interface-vlan-properties",
        "AssetInterfaceVlanProperties",
        [
            {
                "device_id": "cs-uuid-42",
                "asset_interface_id": "if-1",
                "interface_name": "1/1",
                "port_vlan": 10,
                "vlans": [{"vlan_number": 10}, {"vlan_number": 20}],
            }
        ],
    )

    entities = list(Backend().run("platformone_worker", _policy()))

    assert len(entities) == 5
    assert entities[0].site.name == "HQ"
    assert entities[1].location.name == "B1"
    assert entities[2].location.name == "F2"
    assert entities[2].location.parent.name == "B1"
    assert entities[3].device.name == "sw-idf1"
    assert entities[3].device.site.name == "HQ"
    assert entities[3].device.location.name == "F2"
    interface = entities[4].interface
    assert interface.name == "1/1"
    assert interface.device.name == "sw-idf1"
    assert interface.enabled is True
    assert interface.mark_connected is True
    assert interface.speed == 1_000_000
    assert interface.type == "1000base-t"
    assert interface.untagged_vlan.vid == 10
    assert [v.vid for v in interface.tagged_vlans] == [20]
    assert interface.mode == "tagged"


@responses.activate
def test_run_batches_every_switch_into_one_call_per_port_table():
    switch2 = {**SWITCH_ASSET, "device_id": 43, "host_name": "sw-idf2", "serial_number": "SN43"}
    cs2 = {"id": "cs-uuid-43", "serial_number": "SN43"}
    _mock_assets([SWITCH_ASSET, switch2])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH, cs2])
    _mock_cs("asset-location", "AssetLocation", [])
    for table, key in [
        ("asset-port-config", "AssetPortConfig"),
        ("asset-port-state", "AssetPortState"),
        ("asset-interface-vlan-properties", "AssetInterfaceVlanProperties"),
    ]:
        _mock_cs(table, key, [])

    list(Backend().run("platformone_worker", _policy()))

    port_calls = [c for c in responses.calls if "/retrieve-asset-port-config" in c.request.url]
    assert len(port_calls) == 1
    assert json.loads(port_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42", "cs-uuid-43"]}
    vlan_calls = [c for c in responses.calls if "/retrieve-asset-interface-vlan-properties" in c.request.url]
    assert json.loads(vlan_calls[0].request.body) == {"device_id": ["cs-uuid-42", "cs-uuid-43"]}


@responses.activate
def test_run_correlates_by_mac_when_configstate_has_no_serial():
    """Assets sends MACs as bare hex, ConfigState may use separators -- the
    match must normalize both sides."""
    cs = {"id": "cs-uuid-42", "base_mac_address": "AA:BB:CC:DD:EE:FF"}
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [cs])
    _mock_cs("asset-location", "AssetLocation", [])
    for table, key in [
        ("asset-port-config", "AssetPortConfig"),
        ("asset-port-state", "AssetPortState"),
        ("asset-interface-vlan-properties", "AssetInterfaceVlanProperties"),
    ]:
        _mock_cs(table, key, [])

    list(Backend().run("platformone_worker", _policy()))

    port_calls = [c for c in responses.calls if "/retrieve-asset-port-config" in c.request.url]
    assert json.loads(port_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42"]}


@responses.activate
def test_run_out_of_scope_devices_get_no_port_calls_and_no_entities():
    """Scope regression: an out-of-scope switch must not leak back in as
    Interface entities via the port fan-out (Diode would re-create its
    Device through implicit reference handling)."""
    branch_switch = {**SWITCH_ASSET, "device_id": 43, "host_name": "sw-branch", "serial_number": "SN43"}
    cs2 = {"id": "cs-uuid-43", "serial_number": "SN43"}
    _mock_assets([SWITCH_ASSET, branch_switch])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH, cs2])
    _mock_cs(
        "asset-location",
        "AssetLocation",
        [
            {"asset_device_id": "cs-uuid-42", "site_name": "HQ"},
            {"asset_device_id": "cs-uuid-43", "site_name": "Branch"},
        ],
    )
    for table, key in [
        ("asset-port-config", "AssetPortConfig"),
        ("asset-port-state", "AssetPortState"),
        ("asset-interface-vlan-properties", "AssetInterfaceVlanProperties"),
    ]:
        _mock_cs(table, key, [])

    policy = Policy(
        config=Config(package="orb_extreme_platformone", PLATFORMONE_API_TOKEN="tok"),
        scope={"sites": ["HQ"]},
    )
    entities = list(Backend().run("platformone_worker", policy))

    device_names = [e.device.name for e in entities if e.HasField("device")]
    assert device_names == ["sw-idf1"]
    port_calls = [c for c in responses.calls if "/retrieve-asset-port-config" in c.request.url]
    assert json.loads(port_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42"]}


@responses.activate
def test_run_survives_a_failed_port_table_and_keeps_the_rest():
    """One failing ConfigState table (here port-state) degrades that table's
    fields for the tick; ports still map from port-config."""
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_cs(
        "asset-port-config",
        "AssetPortConfig",
        [{"asset_device_id": "cs-uuid-42", "asset_interface_id": "if-1", "name": "1/1", "enabled": True}],
    )
    _mock_cs("asset-port-state", "AssetPortState", [], status=500)
    _mock_cs("asset-interface-vlan-properties", "AssetInterfaceVlanProperties", [])

    entities = list(Backend().run("platformone_worker", _policy()))

    interfaces = [e.interface for e in entities if e.HasField("interface")]
    assert [i.name for i in interfaces] == ["1/1"]
    assert interfaces[0].enabled is True


@responses.activate
def test_run_survives_a_configstate_outage_with_assets_only_data():
    """ConfigState down entirely: devices still sync from Assets (flat site,
    no ports), the tick does not fail."""
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [], status=500)

    entities = list(Backend().run("platformone_worker", _policy()))

    assert [e.site.name for e in entities if e.HasField("site")] == ["Assets-Site"]
    device_names = [e.device.name for e in entities if e.HasField("device")]
    assert device_names == ["sw-idf1"]
    assert not [e for e in entities if e.HasField("interface")]


@responses.activate
def test_run_uncorrelated_device_syncs_without_ports():
    """A device Assets knows but ConfigState doesn't (not collected yet)
    still becomes a Device entity -- just with no ports or building/floor."""
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [{"id": "cs-other", "serial_number": "OTHER"}])
    _mock_cs("asset-location", "AssetLocation", [])

    entities = list(Backend().run("platformone_worker", _policy()))

    device_names = [e.device.name for e in entities if e.HasField("device")]
    assert device_names == ["sw-idf1"]
    assert not [e for e in entities if e.HasField("interface")]
    port_calls = [c for c in responses.calls if "/retrieve-asset-port" in c.request.url]
    assert not port_calls
