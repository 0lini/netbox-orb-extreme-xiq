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

import pytest
import responses
from worker.models import Config, Policy

from orb_extreme_platformone.backend import INTERFACE_ID_TABLES, PORT_TABLES, Backend
from orb_extreme_platformone.client import DEFAULT_BASE_URL, PlatformOneApiError, configstate_response_key
from tests.conftest import CS_SWITCH, SWITCH_ASSET

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


def _mock_empty_clusters():
    """Existing port-focused tests do not care about VC; no InferredDevice rows
    means the backend skips retrieve-inferred-cluster entirely."""
    _mock_cs("inferred-device", "InferredDevice", [])


def _mock_empty_port_and_lag_tables():
    """Empty mocks for every PORT_TABLES entry so adding a table cannot drift."""
    for table, _ in PORT_TABLES.values():
        _mock_cs(table, configstate_response_key(table), [])


def _mock_interface_id_tables_empty():
    """Empty mocks for PoE-config / interface-IP (fetched when interface UUIDs exist)."""
    for table, _ in INTERFACE_ID_TABLES.values():
        _mock_cs(table, configstate_response_key(table), [])


def _mock_port_tables_empty():
    _mock_empty_port_and_lag_tables()
    _mock_empty_clusters()


def _policy(**config_overrides) -> Policy:
    config = Config(
        package="orb_extreme_platformone",
        BOOTSTRAP=False,
        PLATFORMONE_API_TOKEN="tok",
        **config_overrides,
    )
    return Policy(config=config, scope=config_overrides.get("scope", {"sites": ["*"]}))


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
    _mock_cs("asset-lag-config", "AssetLagConfig", [])
    _mock_cs("asset-lag-state", "AssetLagState", [])
    _mock_cs("asset-port-capabilities", "AssetPortCapabilities", [])
    _mock_cs("asset-poe-power-ports-state", "AssetPoePowerPortsState", [])
    _mock_interface_id_tables_empty()
    _mock_empty_clusters()

    entities = list(Backend().run("platformone_worker", _policy()))

    assert len(entities) == 5
    assert entities[0].site.name == "HQ"
    assert entities[1].location.name == "B1"
    assert entities[2].location.name == "F2"
    assert entities[2].location.parent.name == "B1"
    assert entities[3].device.name == "sw-idf1"
    assert entities[3].device.site.name == "HQ"
    assert entities[3].device.location.name == "F2"
    assert entities[3].device.role.name == "Fabric Engine"
    assert entities[3].device.custom_fields["platformone_configstate_device_id"].text == "cs-uuid-42"
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
    _mock_port_tables_empty()

    list(Backend().run("platformone_worker", _policy()))

    device_calls = [c for c in responses.calls if "/retrieve-asset-device" in c.request.url]
    assert len(device_calls) == 1
    assert json.loads(device_calls[0].request.body) == {"serial_number": ["SN42", "SN43"]}
    port_calls = [c for c in responses.calls if "/retrieve-asset-port-config" in c.request.url]
    assert len(port_calls) == 1
    assert json.loads(port_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42", "cs-uuid-43"]}
    vlan_calls = [c for c in responses.calls if "/retrieve-asset-interface-vlan-properties" in c.request.url]
    assert json.loads(vlan_calls[0].request.body) == {"device_id": ["cs-uuid-42", "cs-uuid-43"]}
    inferred_calls = [c for c in responses.calls if "/retrieve-inferred-device" in c.request.url]
    assert len(inferred_calls) == 1
    assert json.loads(inferred_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42", "cs-uuid-43"]}
    # No InferredDevice rows -> cluster retrieve is skipped (AssetDevice UUIDs
    # are the wrong ID space for device_one_id / device_two_id).
    assert not [c for c in responses.calls if "/retrieve-inferred-cluster" in c.request.url]


@responses.activate
def test_run_correlates_by_mac_when_configstate_has_no_serial():
    """Assets sends MACs as bare hex, ConfigState may use separators -- the
    match must normalize both sides."""
    cs = {"id": "cs-uuid-42", "base_mac_address": "AA:BB:CC:DD:EE:FF"}
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [cs])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_port_tables_empty()

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
    _mock_port_tables_empty()

    policy = Policy(
        config=Config(package="orb_extreme_platformone", PLATFORMONE_API_TOKEN="tok"),
        scope={"sites": ["HQ"]},
    )
    entities = list(Backend().run("platformone_worker", policy))

    device_names = [e.device.name for e in entities if e.HasField("device")]
    assert device_names == ["sw-idf1"]
    port_calls = [c for c in responses.calls if "/retrieve-asset-port-config" in c.request.url]
    assert json.loads(port_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42"]}
    inferred_calls = [c for c in responses.calls if "/retrieve-inferred-device" in c.request.url]
    assert json.loads(inferred_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42"]}
    assert not [c for c in responses.calls if "/retrieve-inferred-cluster" in c.request.url]


@responses.activate
def test_run_aborts_when_a_port_table_fails():
    """A failed ConfigState port table aborts the tick -- no per-table degradation.

    Full-outage resilience is handled earlier, before any cs_device_id
    resolves (see test_run_survives_a_configstate_outage_with_assets_only_data);
    a partial failure after correlation succeeds propagates.
    """
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
    _mock_cs("asset-lag-config", "AssetLagConfig", [])
    _mock_cs("asset-lag-state", "AssetLagState", [])
    _mock_cs("asset-port-capabilities", "AssetPortCapabilities", [])
    _mock_cs("asset-poe-power-ports-state", "AssetPoePowerPortsState", [])
    _mock_empty_clusters()

    with pytest.raises(PlatformOneApiError):
        list(Backend().run("platformone_worker", _policy()))


def test_correlate_warns_on_duplicate_serial(caplog):
    from orb_extreme_platformone.backend import _correlate

    assets = [{"device_id": 1, "serial_number": "SN1"}]
    cs_devices = [
        {"id": "a", "serial_number": "SN1"},
        {"id": "b", "serial_number": "sn1"},
    ]
    matched = _correlate(assets, cs_devices)

    assert matched[1]["id"] == "a"
    assert "Duplicate ConfigState AssetDevice serial_number" in caplog.text


@responses.activate
def test_run_maps_inferred_cluster_to_virtual_chassis():
    switch2 = {**SWITCH_ASSET, "device_id": 43, "host_name": "sw-idf2", "serial_number": "SN43"}
    cs2 = {"id": "cs-uuid-43", "serial_number": "SN43"}
    _mock_assets([SWITCH_ASSET, switch2])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH, cs2])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_empty_port_and_lag_tables()
    # device_one_id / device_two_id are InferredDevice UUIDs, not AssetDevice.
    _mock_cs(
        "inferred-device",
        "InferredDevice",
        [
            {"id": "inf-uuid-42", "asset_device_id": "cs-uuid-42"},
            {"id": "inf-uuid-43", "asset_device_id": "cs-uuid-43"},
        ],
    )
    cluster = {
        "id": "cluster-uuid-1",
        "device_one_id": "inf-uuid-42",
        "device_two_id": "inf-uuid-43",
        "device_one_peer_name": "peer-b",
        "device_two_peer_name": "peer-a",
    }
    # Both member-filter calls return the same cluster; backend dedupes by id.
    _mock_cs("inferred-cluster", "InferredCluster", [cluster])
    _mock_cs("inferred-cluster", "InferredCluster", [cluster])

    entities = list(Backend().run("platformone_worker", _policy()))

    inferred_calls = [c for c in responses.calls if "/retrieve-inferred-device" in c.request.url]
    assert json.loads(inferred_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42", "cs-uuid-43"]}
    cluster_calls = [c for c in responses.calls if "/retrieve-inferred-cluster" in c.request.url]
    assert len(cluster_calls) == 2
    bodies = [json.loads(c.request.body) for c in cluster_calls]
    assert {"device_one_id": ["inf-uuid-42", "inf-uuid-43"]} in bodies
    assert {"device_two_id": ["inf-uuid-42", "inf-uuid-43"]} in bodies

    chassis = [e.virtual_chassis for e in entities if e.HasField("virtual_chassis")]
    assert len(chassis) == 1
    assert chassis[0].name == "peer-a / peer-b"
    assert chassis[0].master.name == "sw-idf1"
    assert not chassis[0].description
    assert chassis[0].custom_fields["platformone_cluster_id"].text == "cluster-uuid-1"

    devices = {e.device.name: e.device for e in entities if e.HasField("device")}
    assert devices["sw-idf1"].virtual_chassis.name == "peer-a / peer-b"
    assert devices["sw-idf1"].vc_position == 1
    assert devices["sw-idf2"].vc_position == 2


@responses.activate
def test_run_maps_lag_interfaces_and_member_lag_refs():
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_cs(
        "asset-port-config",
        "AssetPortConfig",
        [
            {"asset_device_id": "cs-uuid-42", "asset_interface_id": "if-1", "name": "1/1", "enabled": True},
            {"asset_device_id": "cs-uuid-42", "asset_interface_id": "if-2", "name": "1/2", "enabled": True},
        ],
    )
    _mock_cs("asset-port-state", "AssetPortState", [])
    _mock_cs("asset-interface-vlan-properties", "AssetInterfaceVlanProperties", [])
    _mock_cs(
        "asset-lag-config",
        "AssetLagConfig",
        [
            {
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
        ],
    )
    _mock_cs("asset-lag-state", "AssetLagState", [])
    _mock_cs("asset-port-capabilities", "AssetPortCapabilities", [])
    _mock_cs("asset-poe-power-ports-state", "AssetPoePowerPortsState", [])
    _mock_interface_id_tables_empty()
    _mock_empty_clusters()

    entities = list(Backend().run("platformone_worker", _policy()))

    interfaces = {e.interface.name: e.interface for e in entities if e.HasField("interface")}
    assert set(interfaces) == {"lag1", "1/1", "1/2"}
    assert interfaces["lag1"].type == "lag"
    assert interfaces["lag1"].enabled is True
    assert interfaces["lag1"].custom_fields["platformone_interface_id"].text == "lag-if-1"
    assert interfaces["1/1"].lag.name == "lag1"
    assert interfaces["1/2"].lag.name == "lag1"
    # Nested members present -> no separate member-port retrieve.
    assert not [c for c in responses.calls if "/retrieve-asset-lag-config-member-port" in c.request.url]


@responses.activate
def test_run_fetches_lag_member_ports_when_nested_empty():
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_cs(
        "asset-port-config",
        "AssetPortConfig",
        [{"asset_device_id": "cs-uuid-42", "asset_interface_id": "if-1", "name": "1/1", "enabled": True}],
    )
    _mock_cs("asset-port-state", "AssetPortState", [])
    _mock_cs("asset-interface-vlan-properties", "AssetInterfaceVlanProperties", [])
    _mock_cs(
        "asset-lag-config",
        "AssetLagConfig",
        [
            {
                "id": "lag-cfg-1",
                "asset_device_id": "cs-uuid-42",
                "asset_interface_id": "lag-if-1",
                "lag_number": "1",
                "name": "lag1",
                "enabled": True,
            }
        ],
    )
    _mock_cs("asset-lag-state", "AssetLagState", [])
    _mock_cs("asset-port-capabilities", "AssetPortCapabilities", [])
    _mock_cs("asset-poe-power-ports-state", "AssetPoePowerPortsState", [])
    _mock_cs(
        "asset-lag-config-member-port",
        "AssetLagConfigMemberPort",
        [{"asset_lag_config_id": "lag-cfg-1", "interface_name": "1/1"}],
    )
    _mock_interface_id_tables_empty()
    _mock_empty_clusters()

    entities = list(Backend().run("platformone_worker", _policy()))

    interfaces = {e.interface.name: e.interface for e in entities if e.HasField("interface")}
    assert interfaces["lag1"].type == "lag"
    assert interfaces["1/1"].lag.name == "lag1"
    member_calls = [c for c in responses.calls if "/retrieve-asset-lag-config-member-port" in c.request.url]
    assert len(member_calls) == 1
    assert json.loads(member_calls[0].request.body) == {"asset_lag_config_id": ["lag-cfg-1"]}


@responses.activate
def test_run_survives_a_failed_inferred_cluster_fetch():
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_empty_port_and_lag_tables()
    _mock_cs("inferred-device", "InferredDevice", [{"id": "inf-uuid-42", "asset_device_id": "cs-uuid-42"}])
    _mock_cs("inferred-cluster", "InferredCluster", [], status=500)

    entities = list(Backend().run("platformone_worker", _policy()))

    assert [e.device.name for e in entities if e.HasField("device")] == ["sw-idf1"]
    assert not [e for e in entities if e.HasField("virtual_chassis")]


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
    assert not [c for c in responses.calls if "/retrieve-inferred-cluster" in c.request.url]


def test_collect_interface_ids_includes_vlan_only_interfaces():
    """VLAN-facing interfaces absent from port/LAG rows must still feed IP/PoE fetches."""
    tables_by_device = {
        "cs-uuid-42": {
            "port_configs": [
                {"asset_interface_id": "if-port", "name": "1/1"},
            ],
            "vlan_properties": [
                {"asset_interface_id": "if-port", "interface_name": "1/1"},
                {"asset_interface_id": "if-svi", "interface_name": "vlan10"},
            ],
            "lag_configs": [],
            "lag_states": [],
            "poe_states": [],
            "port_states": [],
        }
    }

    ids, mapping = Backend._collect_interface_ids(tables_by_device)

    assert ids == ["if-port", "if-svi"]
    assert mapping == {"if-port": "cs-uuid-42", "if-svi": "cs-uuid-42"}
