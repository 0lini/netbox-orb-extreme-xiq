"""bootstrap.ensure_schema tests -- NetBox REST API mocked with `responses`."""

from __future__ import annotations

import json

import pytest
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


def test_ensure_schema_rejects_non_https_netbox_url():
    with pytest.raises(ValueError, match="https://"):
        bootstrap.ensure_schema("http://netbox.example.com", "token")


@responses.activate
def test_ensure_schema_creates_missing_definitions():
    responses.add(responses.GET, CF_URL, json={"count": 0, "results": []}, status=200)
    responses.add(responses.POST, CF_URL, json={}, status=201)
    responses.add(responses.GET, TAG_URL, json={"count": 0, "results": []}, status=200)
    responses.add(responses.POST, TAG_URL, json={}, status=201)

    bootstrap.ensure_schema(NETBOX, "token")

    created = [
        c.request.url.rstrip("/").rsplit("/", 1)[-1] for c in responses.calls if c.request.method == "POST"
    ]
    assert created.count("custom-fields") == len(bootstrap.CUSTOM_FIELDS)
    assert created.count("tags") == len(bootstrap.TAGS)
    field_bodies = [
        json.loads(c.request.body)
        for c in responses.calls
        if c.request.method == "POST" and "custom-fields" in c.request.url
    ]
    assert all(body["unique"] is True for body in field_bodies)


@responses.activate
def test_ensure_schema_is_idempotent_when_definitions_exist():
    responses.add(
        responses.GET, CF_URL, json={"count": 1, "results": [{"id": 1, "unique": True}]}, status=200
    )
    responses.add(responses.GET, TAG_URL, json={"count": 1, "results": [{"id": 2}]}, status=200)

    bootstrap.ensure_schema(NETBOX, "token")

    assert not [c for c in responses.calls if c.request.method in ("POST", "PATCH")]


@responses.activate
def test_ensure_schema_patches_unique_onto_existing_fields():
    """Fields created by a pre-uniqueness bootstrap must gain the flag."""
    responses.add(
        responses.GET, CF_URL, json={"count": 1, "results": [{"id": 7, "unique": False}]}, status=200
    )
    responses.add(responses.PATCH, f"{CF_URL}7/", json={}, status=200)
    responses.add(responses.GET, TAG_URL, json={"count": 1, "results": [{"id": 2}]}, status=200)

    bootstrap.ensure_schema(NETBOX, "token")

    patches = [c for c in responses.calls if c.request.method == "PATCH"]
    assert len(patches) == len(bootstrap.CUSTOM_FIELDS)
    assert all(json.loads(c.request.body) == {"unique": True} for c in patches)
    # Tags carry no `unique` concept; existing tags are left untouched.
    assert not [c for c in responses.calls if c.request.method == "POST"]


def test_custom_fields_and_tags_speak_platform_one():
    names = {field["name"] for field in bootstrap.CUSTOM_FIELDS}
    assert names == {
        "platformone_device_id",
        "platformone_configstate_device_id",
        "platformone_interface_id",
        "platformone_cluster_id",
    }
    by_name = {f["name"]: f for f in bootstrap.CUSTOM_FIELDS}
    assert by_name["platformone_device_id"]["object_types"] == ["dcim.device"]
    assert by_name["platformone_configstate_device_id"]["object_types"] == ["dcim.device"]
    assert by_name["platformone_interface_id"]["object_types"] == ["dcim.interface"]
    assert by_name["platformone_cluster_id"]["object_types"] == ["dcim.virtualchassis"]
    assert all(field["unique"] is True for field in bootstrap.CUSTOM_FIELDS)
    slugs = {tag["slug"] for tag in bootstrap.TAGS}
    assert slugs == {"extreme-networks", "platform-one", "discovered"}
