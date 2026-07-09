"""End-to-end Backend.run() test against the real worker/diode-sdk contracts.

Unlike test_mapper.py, this deliberately does NOT stub the Diode SDK -- it
exercises the real protobuf Entity/Device/Site classes plus the real
worker.models.Policy/Config, to catch drift against the installed
netboxlabs-diode-sdk / netboxlabs-orb-worker versions early.
"""

from __future__ import annotations

import responses
from worker.models import Config, Policy

from orb_extreme_xiq.backend import Backend

BASE = "https://api.extremecloudiq.com"


def test_describe_reports_stable_identity():
    metadata = Backend.describe()
    assert metadata.app_name == "orb-extreme-xiq"
    assert metadata.name == "orb_extreme_xiq"


@responses.activate
def test_run_produces_the_location_tree_and_a_device_entity():
    responses.add(
        responses.GET,
        f"{BASE}/locations/tree",
        json=[{"id": 1, "name": "HQ", "children": [{"id": 2, "name": "Floor 1", "children": []}]}],
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/devices",
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
        location_site_mapping={"HQ": "Corporate-HQ"},
    )
    policy = Policy(config=config, scope={"sites": ["*"]})

    entities = list(Backend().run("extreme_xiq_worker", policy))

    # Location tree (root-first) + one Device entity referencing both.
    assert len(entities) == 3

    root_location = entities[0].location
    assert root_location.name == "HQ"
    assert root_location.site.name == "Corporate-HQ"
    assert not root_location.HasField("parent")

    leaf_location = entities[1].location
    assert leaf_location.name == "Floor 1"
    assert leaf_location.site.name == "Corporate-HQ"
    assert leaf_location.parent.name == "HQ"

    device = entities[2].device
    assert device.name == "ap-lobby"
    assert device.site.name == "Corporate-HQ"
    assert device.location.name == "Floor 1"
