"""Orb Agent worker entrypoint for the Extreme Platform ONE integration.

Implements the `worker.backend.Backend` contract from `netboxlabs-orb-worker`:
`describe()` reports identity, `run()` returns the Diode entities for one
policy tick. The PolicyRunner owns scheduling and the Diode client; this
module only produces entities.

ConfigState table catalogs and batched retrieves live in `fetch/`; entity
mapping lives in `mapper/`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from netboxlabs.diode.sdk.ingester import Entity
from worker.backend import Backend as WorkerBackend
from worker.models import Metadata, Policy

from . import __version__, bootstrap, mapper
from .client import DEFAULT_BASE_URL, PlatformOneApiError, PlatformOneClient
from .fetch import (
    CLUSTER_MEMBER_FILTERS,
    INTERFACE_ID_TABLES,
    LAG_MEMBER_TABLES,
    PORT_TABLES,
    WIRELESS_TABLES,
    correlated_records,
)
from .fetch.clusters import fetch_inferred_clusters
from .fetch.ports import collect_interface_ids, fetch_port_tables
from .fetch.wireless import fetch_wireless_tables
from .identity import device_name, is_ap, is_switch

logger = logging.getLogger(__name__)

APP_NAME = "netbox-orb-extreme-platformone"
APP_VERSION = __version__
# Sync every Assets device class by default (switches, APs, routers, ...);
# narrow with the `classification` policy key. Port sync stays gated on
# switch-OS devices regardless (see is_switch).
DEFAULT_CLASSIFICATION = "ALL"

# Re-exported for tests / contract checks that historically imported catalogs
# from this module.
__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "Backend",
    "CLUSTER_MEMBER_FILTERS",
    "DEFAULT_CLASSIFICATION",
    "INTERFACE_ID_TABLES",
    "LAG_MEMBER_TABLES",
    "PORT_TABLES",
    "WIRELESS_TABLES",
]


def _cfg(config, key: str, default=None):
    return getattr(config, key, default) if config is not None else default


def _cfg_or_env(config, key: str, *, default=None):
    """Policy config wins; falls back to the same-named environment variable."""
    return _cfg(config, key, None) or os.environ.get(key, default)


def _scope_sites(scope) -> list[str] | None:
    if not isinstance(scope, dict):
        return None
    sites = scope.get("sites")
    if not sites or sites == ["*"]:
        return None
    return list(sites)


def _records_by_cs_id(records: list[dict], *, predicate) -> dict[str, dict]:
    """Index records by cs_device_id, keeping the first and warning on collisions.

    Port/radio/VC fan-out is keyed by ConfigState UUID; two Assets rows that
    correlate to the same UUID would otherwise silently overwrite each other.
    """
    by_id: dict[str, dict] = {}
    for record in records:
        cs_id = record.get("cs_device_id")
        if not cs_id or not predicate(record):
            continue
        if cs_id in by_id:
            logger.warning(
                "Duplicate ConfigState device id %s across Assets rows "
                "(%r and %r); keeping the first for table fan-out",
                cs_id,
                device_name(by_id[cs_id]["asset"]),
                device_name(record["asset"]),
            )
            continue
        by_id[cs_id] = record
    return by_id


def _build_client(config) -> PlatformOneClient:
    return PlatformOneClient(
        base_url=_cfg_or_env(config, "PLATFORMONE_API_URL", default=DEFAULT_BASE_URL),
        api_token=_cfg_or_env(config, "PLATFORMONE_API_TOKEN"),
        username=_cfg_or_env(config, "PLATFORMONE_USERNAME"),
        password=_cfg_or_env(config, "PLATFORMONE_PASSWORD"),
    )


class Backend(WorkerBackend):
    """Extreme Platform ONE discovery worker backend."""

    @classmethod
    def describe(cls) -> Metadata:
        return Metadata(
            name="orb_extreme_platformone",
            app_name=APP_NAME,
            app_version=APP_VERSION,
            description=(
                "Extreme Platform ONE discovery worker: ingests devices, sites, "
                "ports, and AP radios/WLANs into NetBox."
            ),
        )

    def run(self, policy_name: str, policy: Policy, **kwargs) -> Iterable[Entity]:  # noqa: ARG002
        config = policy.config

        if _cfg(config, "BOOTSTRAP", False):
            logger.info("Policy %s: running bootstrap (custom fields + provenance tags)", policy_name)
            bootstrap.ensure_schema(
                _cfg_or_env(config, "NETBOX_API_URL"),
                _cfg_or_env(config, "NETBOX_API_TOKEN"),
            )

        client = _build_client(config)
        classification = _cfg(config, "classification", DEFAULT_CLASSIFICATION)
        assets = list(client.get_devices(classification=classification))

        records = correlated_records(client, assets, policy_name)

        scope_sites = _scope_sites(getattr(policy, "scope", None))
        # Backend owns scoping: port fan-out and devices_to_entities must see
        # the same filtered list. Pass site_scope=None into the mapper so it
        # does not re-filter (see mapper.scope_devices / devices_to_entities).
        scoped = mapper.scope_devices(
            records,
            site_scope=set(scope_sites) if scope_sites else None,
        )
        logger.info(
            "Policy %s: fetched %d devices from Platform ONE (%d in scope)",
            policy_name,
            len(records),
            len(scoped),
        )

        name_source = _cfg(config, "name_source", "hostname")
        vc_entities, vc_memberships = self._virtual_chassis_entities(client, scoped, name_source, policy_name)
        # Port/LAG/IP tables are fetched before Device entities so primary_ip
        # can use ConfigState interface CIDRs (mask_length) instead of inventing
        # /32 from the bare Assets management address.
        port_entities, primary_ips_by_cs_id = self._port_entities(client, scoped, name_source, policy_name)
        radio_entities = self._radio_entities(client, scoped, name_source, policy_name)
        entities = mapper.devices_to_entities(
            scoped,
            name_source=name_source,
            virtual_chassis_entities=vc_entities,
            vc_memberships=vc_memberships,
            primary_ips_by_cs_id=primary_ips_by_cs_id,
        )
        entities.extend(port_entities)
        entities.extend(radio_entities)

        return entities

    @staticmethod
    def _virtual_chassis_entities(
        client: PlatformOneClient, records: list[dict], name_source: str, policy_name: str
    ) -> tuple[list[Entity], dict[str, dict]]:
        """Fetch InferredCluster and map to VirtualChassis + memberships.

        A failed fetch degrades to no VC entities for this tick rather than
        aborting the sync.
        """
        records_by_cs_id = {
            record["cs_device_id"]: record for record in records if record.get("cs_device_id")
        }
        device_ids = sorted(records_by_cs_id)
        if not device_ids:
            return [], {}

        try:
            clusters = fetch_inferred_clusters(client, device_ids)
        except PlatformOneApiError as exc:
            logger.warning(
                "Policy %s: ConfigState inferred-cluster fetch failed, syncing without VirtualChassis: %s",
                policy_name,
                exc,
            )
            return [], {}

        entities, memberships = mapper.virtual_chassis_to_entities(
            clusters,
            records_by_cs_id=records_by_cs_id,
            name_source=name_source,
        )
        logger.info(
            "Policy %s: mapped %d VirtualChassis entities from %d InferredCluster rows",
            policy_name,
            len(entities),
            len(clusters),
        )
        return entities, memberships

    @staticmethod
    def _collect_interface_ids(
        tables_by_device: dict[str, dict[str, list[dict]]],
    ) -> dict[str, str]:
        """Map each collected asset_interface_id to its device UUID."""
        return collect_interface_ids(tables_by_device)

    @staticmethod
    def _port_entities(
        client: PlatformOneClient, records: list[dict], name_source: str, policy_name: str
    ) -> tuple[list[Entity], dict[str, dict[str, str]]]:
        """Fetch port/LAG tables for in-scope switches and map to Diode entities.

        Returns ``(port_entities, primary_ips_by_cs_id)`` so Device primary
        IPs can reuse the same ConfigState interface CIDRs.
        """
        switches = _records_by_cs_id(
            records,
            predicate=lambda record: is_switch(record["asset"].get("function")),
        )
        if not switches:
            return [], {}
        device_ids = sorted(switches)

        tables_by_device, failed_tables = fetch_port_tables(client, device_ids, policy_name)

        entities: list[Entity] = []
        primary_ips_by_cs_id: dict[str, dict[str, str]] = {}
        for device_id in device_ids:
            record = switches[device_id]
            tables = tables_by_device[device_id]
            primary = mapper.primary_ips_from_tables(tables, asset_ip=record["asset"].get("ip_address"))
            if primary:
                primary_ips_by_cs_id[device_id] = primary
            entities.extend(
                mapper.ports_to_entities(
                    tables,
                    device=device_name(record["asset"], name_source),
                    function=record["asset"].get("function"),
                )
            )
        logger.info("Policy %s: mapped %d wired port entities", policy_name, len(entities))
        if failed_tables:
            logger.warning(
                "Policy %s: ConfigState degradation this tick; failed tables: %s",
                policy_name,
                ", ".join(failed_tables),
            )
        return entities, primary_ips_by_cs_id

    @staticmethod
    def _radio_entities(
        client: PlatformOneClient, records: list[dict], name_source: str, policy_name: str
    ) -> list[Entity]:
        """Fetch wireless/SSID tables for in-scope APs and map to Diode entities."""
        aps = _records_by_cs_id(
            records,
            predicate=lambda record: is_ap(record["asset"].get("function")),
        )
        if not aps:
            return []
        device_ids = sorted(aps)

        tables_by_device, failed_tables = fetch_wireless_tables(client, device_ids, policy_name)

        device_names = {
            device_id: device_name(aps[device_id]["asset"], name_source) for device_id in device_ids
        }
        entities = mapper.radios_to_entities(tables_by_device, device_names=device_names)
        logger.info("Policy %s: mapped %d wireless radio/WLAN entities", policy_name, len(entities))
        if failed_tables:
            logger.warning(
                "Policy %s: ConfigState wireless degradation this tick; failed tables: %s",
                policy_name,
                ", ".join(failed_tables),
            )
        return entities
