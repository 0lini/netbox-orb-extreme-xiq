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


def test_describe_reports_stable_identity():
    metadata = Backend.describe()
    assert metadata.app_name == "orb-extreme-xiq"
    assert metadata.name == "orb_extreme_xiq"


def test_run_produces_a_site_and_a_device_entity(monkeypatch):
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
        location_site_mapping={"HQ": "Corporate-HQ"},
    )
    policy = Policy(config=config, scope={"sites": ["*"]})

    entities = list(Backend().run("extreme_xiq_worker", policy))

    assert len(entities) == 2
    assert entities[0].site.name == "Corporate-HQ"
    assert entities[1].device.name == "ap-lobby"
    assert entities[1].device.site.name == "Corporate-HQ"
