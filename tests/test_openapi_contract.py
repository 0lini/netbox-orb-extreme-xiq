"""Contract checks against the Platform ONE OpenAPI specs.

The Platform ONE specs are published on the developer portal
(https://developer.extremeplatformone.com/api-reference) behind a
browser/login wall -- there is no stable unauthenticated URL to fetch them
from, so unlike the old live-XIQ contract job these checks run against
*local copies*: download the Asset Management and Config State specs from
the portal and point these env vars at them:

    PLATFORMONE_ASSETS_SPEC=/path/to/assets-openapi.json
    PLATFORMONE_CONFIGSTATE_SPEC=/path/to/configstate-openapi.json

Marked `contract` and skipped by default (and always skipped when the env
vars are unset), so the offline test suite stays self-contained. Run with
`pytest -m contract` after refreshing the spec downloads to catch upstream
drift in the endpoints/params client.py and backend.py hardcode.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orb_extreme_platformone.backend import PORT_TABLES
from orb_extreme_platformone.client import configstate_response_key

pytestmark = pytest.mark.contract


def _load_spec(env_var: str) -> dict:
    path = os.environ.get(env_var)
    if not path:
        pytest.skip(f"{env_var} not set -- point it at a downloaded spec to run contract checks")
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def assets_spec() -> dict:
    return _load_spec("PLATFORMONE_ASSETS_SPEC")


@pytest.fixture(scope="session")
def configstate_spec() -> dict:
    return _load_spec("PLATFORMONE_CONFIGSTATE_SPEC")


def test_assets_devices_endpoint_and_params_still_exist(assets_spec):
    post = assets_spec["paths"]["/devices"]["post"]
    param_names = set()
    for param in post.get("parameters", []):
        if "$ref" in param:
            param = assets_spec["components"]["parameters"][param["$ref"].rsplit("/", 1)[-1]]
        param_names.add(param["name"])
    assert {"page", "limit"} <= param_names


def test_assets_filter_still_supports_classification(assets_spec):
    schemas = assets_spec["components"]["schemas"]
    assert "classification" in schemas["ListDevicesRequestFilter"]["properties"]


def test_configstate_tables_client_uses_still_exist(configstate_spec):
    paths = configstate_spec["paths"]
    used_tables = ["asset-device", "asset-location", *(t for t, _ in PORT_TABLES.values())]
    for table in used_tables:
        assert f"/retrieve-{table}" in paths, f"retrieve-{table} disappeared from ConfigState"


def test_configstate_response_keys_and_filter_fields_match(configstate_spec):
    """The response key must equal the schema name the spec wraps records in,
    and the batching filter field must exist on the table's GetRequest."""
    schemas = configstate_spec["components"]["schemas"]
    for table, filter_field in [
        ("asset-device", None),
        ("asset-location", "asset_device_id"),
        *PORT_TABLES.values(),
    ]:
        key = configstate_response_key(table)
        response_schema = schemas[f"{key}GetResponse"]["properties"]
        assert key in response_schema, f"{key}GetResponse no longer wraps records under {key}"
        if filter_field:
            request_schema = schemas[f"{key}GetRequest"]["properties"]
            assert filter_field in request_schema, f"{key}GetRequest lost filter field {filter_field}"


def test_configstate_pagination_params_still_exist(configstate_spec):
    post = configstate_spec["paths"]["/retrieve-asset-device"]["post"]
    names = {p.get("name") for p in post.get("parameters", [])}
    assert {"page_number", "page_size"} <= names
