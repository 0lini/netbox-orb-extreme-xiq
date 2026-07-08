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
    responses.add(responses.GET, f"{BASE}/api/extras/custom-fields/", json={"count": 0}, status=200)
    responses.add(responses.POST, f"{BASE}/api/extras/custom-fields/", json={}, status=201)
    responses.add(responses.GET, f"{BASE}/api/extras/tags/", json={"count": 0}, status=200)
    responses.add(responses.POST, f"{BASE}/api/extras/tags/", json={}, status=201)

    bootstrap.ensure_schema(BASE, "nbtok")

    posts = [c for c in responses.calls if c.request.method == "POST"]
    assert len(posts) == 3  # 2 missing custom fields + the source tag


@responses.activate
def test_ensure_schema_skips_definitions_that_already_exist():
    responses.add(responses.GET, f"{BASE}/api/extras/custom-fields/", json={"count": 1}, status=200)
    responses.add(responses.GET, f"{BASE}/api/extras/custom-fields/", json={"count": 1}, status=200)
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
