"""Tests for bootstrap.ensure_schema: idempotency and the no-credentials no-op."""

from __future__ import annotations

import responses

from orb_extreme_xiq import bootstrap

BASE = "https://nb.example.com"


@responses.activate
def test_ensure_schema_creates_missing_custom_fields_and_tag():
    responses.add(responses.GET, f"{BASE}/api/extras/custom-fields/", json={"count": 0}, status=200)
    responses.add(responses.POST, f"{BASE}/api/extras/custom-fields/", json={}, status=201)
    responses.add(responses.GET, f"{BASE}/api/extras/custom-fields/", json={"count": 1}, status=200)
    for _ in range(len(bootstrap.CUSTOM_FIELDS) - 2):
        responses.add(responses.GET, f"{BASE}/api/extras/custom-fields/", json={"count": 0}, status=200)
        responses.add(responses.POST, f"{BASE}/api/extras/custom-fields/", json={}, status=201)
    responses.add(responses.GET, f"{BASE}/api/extras/tags/", json={"count": 0}, status=200)
    responses.add(responses.POST, f"{BASE}/api/extras/tags/", json={}, status=201)

    bootstrap.ensure_schema(BASE, "nbtok")

    posts = [c for c in responses.calls if c.request.method == "POST"]
    # every custom field but one is "missing" (+ the source tag)
    assert len(posts) == len(bootstrap.CUSTOM_FIELDS) - 1 + 1


@responses.activate
def test_ensure_schema_skips_definitions_that_already_exist():
    for _ in bootstrap.CUSTOM_FIELDS:
        responses.add(responses.GET, f"{BASE}/api/extras/custom-fields/", json={"count": 1}, status=200)
    responses.add(responses.GET, f"{BASE}/api/extras/tags/", json={"count": 1}, status=200)

    bootstrap.ensure_schema(BASE, "nbtok")

    posts = [c for c in responses.calls if c.request.method == "POST"]
    assert posts == []


def test_ensure_schema_is_a_noop_without_credentials():
    # No responses registered -- any HTTP call here would error, proving these are no-ops.
    bootstrap.ensure_schema(None, None)
    bootstrap.ensure_schema(BASE, None)
    bootstrap.ensure_schema(None, "tok")
