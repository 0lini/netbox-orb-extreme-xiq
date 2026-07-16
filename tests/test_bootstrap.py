"""bootstrap.ensure_schema tests -- NetBox REST API mocked with `responses`."""

from __future__ import annotations

import responses

from orb_extreme_platformone import bootstrap

NETBOX = "https://netbox.example.com"
CF_URL = f"{NETBOX}/api/extras/custom-fields/"
TAG_URL = f"{NETBOX}/api/extras/tags/"


def test_ensure_schema_skips_gracefully_without_credentials():
    # No responses.activate: a real HTTP call would error loudly here.
    bootstrap.ensure_schema(None, None)
    bootstrap.ensure_schema(NETBOX, None)
    bootstrap.ensure_schema(None, "token")


@responses.activate
def test_ensure_schema_creates_missing_definitions():
    responses.add(responses.GET, CF_URL, json={"count": 0}, status=200)
    responses.add(responses.POST, CF_URL, json={}, status=201)
    responses.add(responses.GET, TAG_URL, json={"count": 0}, status=200)
    responses.add(responses.POST, TAG_URL, json={}, status=201)

    bootstrap.ensure_schema(NETBOX, "token")

    created = [
        c.request.url.rstrip("/").rsplit("/", 1)[-1] for c in responses.calls if c.request.method == "POST"
    ]
    assert created.count("custom-fields") == len(bootstrap.CUSTOM_FIELDS)
    assert created.count("tags") == len(bootstrap.TAGS)


@responses.activate
def test_ensure_schema_is_idempotent_when_definitions_exist():
    responses.add(responses.GET, CF_URL, json={"count": 1}, status=200)
    responses.add(responses.GET, TAG_URL, json={"count": 1}, status=200)

    bootstrap.ensure_schema(NETBOX, "token")

    assert not [c for c in responses.calls if c.request.method == "POST"]


def test_custom_fields_and_tags_speak_platform_one():
    names = {field["name"] for field in bootstrap.CUSTOM_FIELDS}
    assert names == {
        "platformone_device_id",
        "platformone_interface_id",
        "platformone_cluster_id",
        "platformone_configstate_device_id",
    }
    assert bootstrap.CUSTOM_FIELDS[2]["object_types"] == ["dcim.virtualchassis"]
    assert bootstrap.CUSTOM_FIELDS[3]["object_types"] == ["dcim.device"]
    slugs = {tag["slug"] for tag in bootstrap.TAGS}
    assert slugs == {"extreme-networks", "platform-one", "discovered"}
