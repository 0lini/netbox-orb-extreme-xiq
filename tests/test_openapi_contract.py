"""Contract check against the *live* XIQ OpenAPI spec.

Unlike every other test here, this one hits the network (fetches
https://api.extremecloudiq.com/openapi, unauthenticated, ~1.2MB) and is
therefore excluded from the default `pytest` run (see the `contract` marker
and `addopts` in pyproject.toml) -- run explicitly with `pytest -m contract`,
or via the scheduled "contract" CI job. The point is to catch upstream drift
(a renamed/removed query param, a removed endpoint) in `/devices` and
`/locations/tree`, the two endpoints client.py hardcodes parameter names for.

Deliberately scoped to paths + query param *names* only, not response body
field names: the live spec's `PagedXiqDevice.data` items resolve to a
`XiqDevice` schema with just `id`/`hostname` ("The Device for QoE Diagnostics
Filtering"), which doesn't match the ~34-field model the installed
extremecloudiq-api SDK actually deserializes at runtime -- confirmed by
inspecting the installed package directly. So this public spec document is
not a reliable source for response-shape drift detection; only structural
(path/param) drift is asserted here. mapper.py's field assumptions are
instead exercised against real recorded responses in test_mapper.py/test_backend.py.
"""

from __future__ import annotations

import pytest
import requests

pytestmark = pytest.mark.contract

OPENAPI_URL = "https://api.extremecloudiq.com/openapi"


@pytest.fixture(scope="session")
def live_spec() -> dict:
    try:
        resp = requests.get(OPENAPI_URL, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        pytest.skip(f"could not reach {OPENAPI_URL}: {exc}")
    return resp.json()


def _param_names(live_spec: dict, path: str, method: str = "get") -> set[str]:
    params = live_spec["paths"][path][method].get("parameters", [])
    return {p.get("name", p.get("$ref", "").rsplit("/", 1)[-1]) for p in params}


def test_devices_endpoint_has_the_query_params_client_py_relies_on(live_spec):
    assert "/devices" in live_spec["paths"], "GET /devices has disappeared from the XIQ API"
    names = _param_names(live_spec, "/devices")
    assert {"page", "limit", "views", "locationIds"} <= names


def test_locations_tree_endpoint_has_the_query_params_client_py_relies_on(live_spec):
    assert "/locations/tree" in live_spec["paths"], "GET /locations/tree has disappeared from the XIQ API"
    names = _param_names(live_spec, "/locations/tree")
    assert {"parentId", "expandChildren"} <= names
