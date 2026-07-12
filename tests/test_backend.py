"""End-to-end Backend.run() test against the real worker/diode-sdk contracts.

Unlike test_mapper.py, this deliberately does NOT stub the Diode SDK -- it
exercises the real protobuf Entity/Device/Site classes plus the real
worker.models.Policy/Config, to catch drift against the installed
netboxlabs-diode-sdk / netboxlabs-orb-worker versions early.

XIQ itself is mocked at the SDK Api-class boundary (see test_client.py's
docstring for why: the official SDK talks HTTP via urllib3, not `requests`,
so `responses` can't intercept it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from extremecloudiq.apis.tags.device_api import DeviceApi
from extremecloudiq.apis.tags.location_api import LocationApi
from worker.models import Config, Policy

from orb_extreme_xiq.backend import Backend


@dataclass
class FakeHttpResponse:
    data: bytes
    status: int = 200


@dataclass
class FakeApiResponse:
    response: FakeHttpResponse


def json_response(payload) -> FakeApiResponse:
    return FakeApiResponse(response=FakeHttpResponse(data=json.dumps(payload).encode()))


def _mock_no_wired_ports(monkeypatch):
    """Switch-port sync and AP radio/WLAN sync both always run (no opt-in
    flag), so every end-to-end test needs both endpoints mocked even when
    it's only exercising one of the two.
    """
    monkeypatch.setattr("orb_extreme_xiq.client.XiqClient.get_wired_portlist", lambda self, device_id: [])


def _mock_no_radios(monkeypatch):
    monkeypatch.setattr(
        DeviceApi,
        "list_devices_radio_information",
        lambda self, **kw: json_response({"page": 1, "total_pages": 1, "data": []}),
    )


def test_describe_reports_stable_identity():
    metadata = Backend.describe()
    assert metadata.app_name == "orb-extreme-xiq"
    assert metadata.name == "orb_extreme_xiq"


def test_run_produces_a_site_and_a_device_entity(monkeypatch):
    _mock_no_wired_ports(monkeypatch)
    _mock_no_radios(monkeypatch)
    monkeypatch.setattr(
        LocationApi,
        "get_location_tree",
        lambda self, **kw: json_response(
            [{"id": 1, "name": "HQ", "children": [{"id": 2, "name": "Floor 1", "children": []}]}]
        ),
    )
    monkeypatch.setattr(
        DeviceApi,
        "list_devices",
        lambda self, **kw: json_response(
            {
                "page": 1,
                "count": 1,
                "total_pages": 1,
                "total_count": 1,
                "data": [
                    {
                        "id": 111,
                        "hostname": "ap-lobby",
                        "serial_number": "SN111",
                        "product_type": "AP305C",
                        "device_function": "AP",
                        "ip_address": "10.0.0.5",
                        "connected": True,
                        "location_id": 2,
                        "org_id": 9,
                    }
                ],
            }
        ),
    )

    config = Config(
        package="orb_extreme_xiq",
        BOOTSTRAP=False,
        XIQ_API_TOKEN="tok",
        default_site="XIQ-Unmapped",
    )
    policy = Policy(config=config, scope={"sites": ["*"]})

    entities = list(Backend().run("extreme_xiq_worker", policy))

    assert len(entities) == 2
    assert entities[0].site.name == "Floor 1"
    assert entities[1].device.name == "ap-lobby"
    assert entities[1].device.site.name == "Floor 1"


def test_run_maps_switch_interfaces(monkeypatch):
    """Exercises wired-port sync end to end, including the switch-detection
    check (identity.is_switch) that only this path calls.

    Includes an AP alongside the switch so this also acts as a regression
    test: switch-detection must key off the device's raw device_function
    (identity.is_switch), not off role_for()'s display string -- comparing
    against the display string is fragile since it silently breaks if that
    string is ever renamed without updating the comparison too.
    """
    _mock_no_radios(monkeypatch)
    monkeypatch.setattr(LocationApi, "get_location_tree", lambda self, **kw: json_response([]))
    monkeypatch.setattr(
        DeviceApi,
        "list_devices",
        lambda self, **kw: json_response(
            {
                "page": 1,
                "count": 2,
                "total_pages": 1,
                "total_count": 2,
                "data": [
                    {
                        "id": 111,
                        "hostname": "ap-lobby",
                        "serial_number": "SN111",
                        "device_function": "AP",
                        "connected": True,
                    },
                    {
                        "id": 222,
                        "hostname": "sw-idf1",
                        "serial_number": "SN222",
                        "device_function": "SWITCH",
                        "connected": True,
                    },
                ],
            }
        ),
    )
    portlist_calls = []

    def fake_get_wired_portlist(self, device_id):
        portlist_calls.append(device_id)
        return [
            {
                "id": 1,
                "ifName": "1/1",
                "status": "UP",
                "portSpeed": "SPEED_1000M",
                "transmissionMode": "Full-duplex",
            }
        ]

    monkeypatch.setattr("orb_extreme_xiq.client.XiqClient.get_wired_portlist", fake_get_wired_portlist)

    config = Config(
        package="orb_extreme_xiq",
        BOOTSTRAP=False,
        XIQ_API_TOKEN="tok",
        default_site="XIQ-Unmapped",
    )
    policy = Policy(config=config, scope={"sites": ["*"]})

    entities = list(Backend().run("extreme_xiq_worker", policy))

    interfaces = [e.interface for e in entities if e.HasField("interface")]
    assert len(interfaces) == 1
    assert interfaces[0].device.name == "sw-idf1"
    assert interfaces[0].name == "1/1"
    # get_wired_portlist is only ever called for the switch, never the AP.
    assert portlist_calls == [222]


def test_run_maps_ap_radios_and_wlans(monkeypatch):
    """Exercises AP radio/WLAN sync end to end: one bulk get_radio_information
    call covering only APs (identity.is_ap), never switches, producing both
    Interface (per radio) and WirelessLAN (per unique SSID) entities.
    """
    _mock_no_wired_ports(monkeypatch)
    monkeypatch.setattr(LocationApi, "get_location_tree", lambda self, **kw: json_response([]))
    monkeypatch.setattr(
        DeviceApi,
        "list_devices",
        lambda self, **kw: json_response(
            {
                "page": 1,
                "count": 2,
                "total_pages": 1,
                "total_count": 2,
                "data": [
                    {
                        "id": 111,
                        "hostname": "ap-lobby",
                        "serial_number": "SN111",
                        "device_function": "AP",
                        "connected": True,
                    },
                    {
                        "id": 222,
                        "hostname": "sw-idf1",
                        "serial_number": "SN222",
                        "device_function": "SWITCH",
                        "connected": True,
                    },
                ],
            }
        ),
    )
    radio_calls = []

    def fake_list_devices_radio_information(self, **kw):
        radio_calls.append(kw["query_params"]["deviceIds"])
        return json_response(
            {
                "page": 1,
                "total_pages": 1,
                "data": [
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
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )

    monkeypatch.setattr(DeviceApi, "list_devices_radio_information", fake_list_devices_radio_information)

    config = Config(
        package="orb_extreme_xiq",
        BOOTSTRAP=False,
        XIQ_API_TOKEN="tok",
        default_site="XIQ-Unmapped",
    )
    policy = Policy(config=config, scope={"sites": ["*"]})

    entities = list(Backend().run("extreme_xiq_worker", policy))

    interfaces = [e.interface for e in entities if e.HasField("interface")]
    wlans = [e.wireless_lan for e in entities if e.HasField("wireless_lan")]
    assert len(interfaces) == 1
    assert interfaces[0].device.name == "ap-lobby"
    assert interfaces[0].name == "Radio1"
    assert [w.ssid for w in interfaces[0].wireless_lans] == ["Corp-WiFi"]
    assert len(wlans) == 1
    assert wlans[0].ssid == "Corp-WiFi"
    assert wlans[0].auth_type == "wpa-enterprise"
    # get_radio_information is only ever called with the AP's device id, never the switch's.
    assert radio_calls == [[111]]
