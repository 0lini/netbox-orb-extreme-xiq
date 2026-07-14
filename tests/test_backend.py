"""End-to-end Backend.run() test against the real worker/diode-sdk contracts.

Unlike test_mapper.py, this deliberately does NOT stub the Diode SDK -- it
exercises the real protobuf Entity/Device/Site classes plus the real
worker.models.Policy/Config, to catch drift against the installed
netboxlabs-diode-sdk / netboxlabs-orb-worker versions early.

XIQ itself is mocked at the HTTP boundary with `responses` (client.py talks
plain `requests`, see client.py's module docstring).
"""

from __future__ import annotations

import responses
from worker.models import Config, Policy

from orb_extreme_xiq.backend import Backend
from orb_extreme_xiq.client import DEFAULT_BASE_URL


def _mock_no_wired_ports(device_id: int):
    """get_wired_portlist is only ever called for switches, but every switch
    in a test's device fixture needs it mocked regardless of which sync path
    the test is exercising.
    """
    responses.add(
        responses.GET,
        "https://cloudapi.extremecloudiq.com/xiq/v0/monitor/device/wired/portlist",
        match=[responses.matchers.query_param_matcher({"deviceId": str(device_id)})],
        json={"data": {"portList": []}},
        status=200,
    )


def _mock_no_radios():
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/devices/radio-information",
        json={"page": 1, "total_pages": 1, "data": []},
        status=200,
    )


def test_describe_reports_stable_identity():
    metadata = Backend.describe()
    assert metadata.app_name == "orb-extreme-xiq"
    assert metadata.name == "orb_extreme_xiq"


@responses.activate
def test_run_produces_a_site_a_location_and_a_device_entity():
    _mock_no_radios()
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/locations/tree",
        json=[{"id": 1, "name": "HQ", "children": [{"id": 2, "name": "Floor 1", "children": []}]}],
        status=200,
    )
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/devices",
        json={
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
        },
        status=200,
    )

    config = Config(
        package="orb_extreme_xiq",
        BOOTSTRAP=False,
        XIQ_API_TOKEN="tok",
        default_site="XIQ-Unmapped",
    )
    policy = Policy(config=config, scope={"sites": ["*"]})

    entities = list(Backend().run("extreme_xiq_worker", policy))

    assert len(entities) == 3
    assert entities[0].site.name == "HQ"
    assert entities[1].location.name == "Floor 1"
    assert entities[1].location.site.name == "HQ"
    assert entities[2].device.name == "ap-lobby"
    assert entities[2].device.site.name == "HQ"
    assert entities[2].device.location.name == "Floor 1"


@responses.activate
def test_run_maps_switch_interfaces():
    """Exercises wired-port sync end to end, including the switch-detection
    check (identity.is_switch) that only this path calls.

    Includes an AP alongside the switch so this also acts as a regression
    test: switch-detection must key off the device's raw device_function
    (identity.is_switch), not off role_for()'s display string -- comparing
    against the display string is fragile since it silently breaks if that
    string is ever renamed without updating the comparison too.
    """
    _mock_no_radios()
    responses.add(responses.GET, f"{DEFAULT_BASE_URL}/locations/tree", json=[], status=200)
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/devices",
        json={
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
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://cloudapi.extremecloudiq.com/xiq/v0/monitor/device/wired/portlist",
        json={
            "data": {
                "portList": [
                    {
                        "id": 1,
                        "ifName": "1/1",
                        "status": "UP",
                        "portSpeed": "SPEED_1000M",
                        "transmissionMode": "Full-duplex",
                    }
                ]
            }
        },
        status=200,
    )

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
    portlist_calls = [
        c
        for c in responses.calls
        if c.request.url.split("?")[0].endswith("/xiq/v0/monitor/device/wired/portlist")
    ]
    assert [c.request.params["deviceId"] for c in portlist_calls] == ["222"]


@responses.activate
def test_run_maps_ap_radios_and_wlans():
    """Exercises AP radio/WLAN sync end to end: one bulk get_radio_information
    call covering only APs (identity.is_ap), never switches, producing both
    Interface (per radio) and WirelessLAN (per unique SSID) entities.
    """
    _mock_no_wired_ports(222)
    responses.add(responses.GET, f"{DEFAULT_BASE_URL}/locations/tree", json=[], status=200)
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/devices",
        json={
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
        },
        status=200,
    )
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/devices/radio-information",
        json={
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
        },
        status=200,
    )

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
    radio_calls = [
        c for c in responses.calls if c.request.url.split("?")[0].endswith("/devices/radio-information")
    ]
    assert [c.request.params["deviceIds"] for c in radio_calls] == ["111"]
