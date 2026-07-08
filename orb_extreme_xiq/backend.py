"""Orb Agent Worker entrypoint for the ExtremeCloud IQ integration.

Implements the `worker.backend.Backend` contract from `netboxlabs-orb-worker`:
`describe()` reports identity, `run()` returns the Diode entities for one
policy tick. The worker (PolicyRunner) owns scheduling and the Diode client
entirely -- this module only ever produces entities, it never pushes them.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from netboxlabs.diode.sdk.ingester import Entity
from worker.backend import Backend as WorkerBackend
from worker.models import Config, Metadata, Policy

from . import bootstrap, mapper
from .client import XiqClient

logger = logging.getLogger(__name__)

APP_NAME = "orb-extreme-xiq"
APP_VERSION = "0.1.0"


def _cfg(config, key: str, default=None):
    return getattr(config, key, default) if config is not None else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _scope_sites(scope) -> list[str] | None:
    if not isinstance(scope, dict):
        return None
    sites = scope.get("sites")
    if not sites or sites == ["*"]:
        return None
    return list(sites)


def _authority(config) -> frozenset:
    base = set(_cfg(config, "field_authority", None) or mapper.DEFAULT_AUTHORITY)
    base -= set(_cfg(config, "field_authority_remove", None) or [])
    base |= set(_cfg(config, "field_authority_add", None) or [])
    return frozenset(base)


def _build_client(config) -> XiqClient:
    return XiqClient(
        base_url=_cfg(config, "XIQ_API_URL", None) or os.environ.get("XIQ_API_URL", "https://api.extremecloudiq.com"),
        api_token=_cfg(config, "XIQ_API_TOKEN", None) or os.environ.get("XIQ_API_TOKEN"),
        username=_cfg(config, "XIQ_USERNAME", None) or os.environ.get("XIQ_USERNAME"),
        password=_cfg(config, "XIQ_PASSWORD", None) or os.environ.get("XIQ_PASSWORD"),
    )


class Backend(WorkerBackend):
    """ExtremeCloud IQ discovery worker backend."""

    @classmethod
    def describe(cls) -> Metadata:
        return Metadata(
            name="orb_extreme_xiq",
            app_name=APP_NAME,
            app_version=APP_VERSION,
            description="ExtremeCloud IQ discovery worker: ingests devices + sites into NetBox via Diode.",
        )

    def run(self, policy_name: str, policy: Policy, **kwargs) -> Iterable[Entity]:
        config = policy.config

        if _cfg(config, "BOOTSTRAP", False):
            logger.info("Policy %s: running bootstrap (custom fields + source:xiq tag)", policy_name)
            bootstrap.ensure_schema(
                _cfg(config, "NETBOX_API_URL", None) or os.environ.get("NETBOX_API_URL"),
                _cfg(config, "NETBOX_API_TOKEN", None) or os.environ.get("NETBOX_API_TOKEN"),
            )

        client = _build_client(config)
        location_index = mapper.build_location_index(client.get_location_tree())
        devices = list(client.get_devices())
        logger.info("Policy %s: fetched %d devices from XIQ", policy_name, len(devices))

        scope_sites = _scope_sites(getattr(policy, "scope", None))
        entities = mapper.devices_to_entities(
            devices,
            location_index=location_index,
            location_site_mapping=_cfg(config, "location_site_mapping", {}) or {},
            default_site=_cfg(config, "default_site", "XIQ-Unmapped"),
            authority=_authority(config),
            name_source=_cfg(config, "name_source", "hostname"),
            site_scope=set(scope_sites) if scope_sites else None,
        )
        return entities


def _standalone_config() -> dict:
    return {
        "package": "orb_extreme_xiq",
        "BOOTSTRAP": _env_bool("BOOTSTRAP", False),
        "NETBOX_API_URL": os.environ.get("NETBOX_API_URL"),
        "NETBOX_API_TOKEN": os.environ.get("NETBOX_API_TOKEN"),
        "XIQ_API_TOKEN": os.environ.get("XIQ_API_TOKEN"),
        "XIQ_USERNAME": os.environ.get("XIQ_USERNAME"),
        "XIQ_PASSWORD": os.environ.get("XIQ_PASSWORD"),
        "name_source": os.environ.get("XIQ_NAME_SOURCE", "hostname"),
        "default_site": os.environ.get("XIQ_DEFAULT_SITE", "XIQ-Unmapped"),
        "location_site_mapping": {},
    }


def main() -> None:
    """Standalone dry run: fetch from XIQ, map, print the entities (no Diode push)."""
    logging.basicConfig(level=logging.INFO)
    policy = Policy(config=Config(**_standalone_config()), scope={"sites": ["*"]})
    backend = Backend()
    for entity in backend.run("standalone", policy):
        print(entity)


if __name__ == "__main__":
    main()
