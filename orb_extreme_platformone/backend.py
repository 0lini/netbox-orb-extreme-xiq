"""Orb Agent worker entrypoint for the Extreme Platform ONE integration.

Implements the `worker.backend.Backend` contract from `netboxlabs-orb-worker`:
`describe()` reports identity, `run()` returns the Diode entities for one
policy tick. The PolicyRunner owns scheduling and the Diode client; this
module only produces entities.

The per-tick API call budget is flat, not per-device: one paginated Assets
listing, one ConfigState device listing (for correlation), then one batched
ConfigState call per port table covering every in-scope switch at once.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from datetime import timezone

from netboxlabs.diode.sdk.ingester import Entity
from worker.backend import Backend as WorkerBackend
from worker.models import Config, Metadata, Policy

from . import __version__, bootstrap, mapper
from .client import DEFAULT_BASE_URL, PlatformOneApiError, PlatformOneClient
from .identity import device_name, is_switch

logger = logging.getLogger(__name__)

APP_NAME = "netbox-orb-extreme-platformone"
APP_VERSION = __version__
DEFAULT_SITE = "PlatformONE-Unmapped"
DEFAULT_CLASSIFICATION = "SWITCH"

# {mapper table key: (retrieve-* table, GetRequest device filter field)}.
# vlan-properties is the one table whose device filter field is `device_id`
# rather than `asset_device_id`.
PORT_TABLES = {
    "port_configs": ("asset-port-config", "asset_device_id"),
    "port_states": ("asset-port-state", "asset_device_id"),
    "vlan_properties": ("asset-interface-vlan-properties", "device_id"),
}


def _cfg(config, key: str, default=None):
    return getattr(config, key, default) if config is not None else default


def _cfg_or_env(config, key: str, *, default=None):
    """Policy config wins; falls back to the same-named environment variable."""
    return _cfg(config, key, None) or os.environ.get(key, default)


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


def _build_client(config) -> PlatformOneClient:
    return PlatformOneClient(
        base_url=_cfg_or_env(config, "PLATFORMONE_API_URL", default=DEFAULT_BASE_URL),
        api_token=_cfg_or_env(config, "PLATFORMONE_API_TOKEN"),
    )


def _normalize_mac(value) -> str:
    """Lowercase hex digits only: Assets and ConfigState format MACs differently."""
    return "".join(ch for ch in str(value or "").lower() if ch in "0123456789abcdef")


def _correlate(assets: list[dict], cs_devices: list[dict]) -> dict[int, dict]:
    """Match Assets devices to ConfigState AssetDevice records.

    Returns {Assets device_id: ConfigState device record}. Serial number is
    the primary key; base MAC and management IP are fallbacks. Devices with
    no match have no ConfigState data yet and still sync as Devices, minus
    ports and building/floor detail.
    """
    by_serial = {str(d["serial_number"]).casefold(): d for d in cs_devices if d.get("serial_number")}
    by_mac = {_normalize_mac(d["base_mac_address"]): d for d in cs_devices if d.get("base_mac_address")}
    by_ip = {str(d["ip_address"]): d for d in cs_devices if d.get("ip_address")}

    matched: dict[int, dict] = {}
    for asset in assets:
        cs = (
            by_serial.get(str(asset.get("serial_number") or "").casefold())
            or by_mac.get(_normalize_mac(asset.get("mac_address")))
            or by_ip.get(str(asset.get("ip_address") or ""))
        )
        if cs is not None and asset.get("device_id") is not None:
            matched[asset["device_id"]] = cs
    return matched


class Backend(WorkerBackend):
    """Extreme Platform ONE discovery worker backend."""

    @classmethod
    def describe(cls) -> Metadata:
        return Metadata(
            name="orb_extreme_platformone",
            app_name=APP_NAME,
            app_version=APP_VERSION,
            description="Extreme Platform ONE discovery worker: ingests devices + sites into NetBox.",
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

        records = self._correlated_records(client, assets, policy_name)

        scope_sites = _scope_sites(getattr(policy, "scope", None))
        default_site = _cfg(config, "default_site", DEFAULT_SITE)
        # Scope once, up front: the port fan-out must see the same filtered
        # list as devices_to_entities (see mapper.scope_devices).
        scoped = mapper.scope_devices(
            records,
            default_site=default_site,
            site_scope=set(scope_sites) if scope_sites else None,
        )
        logger.info(
            "Policy %s: fetched %d devices from Platform ONE (%d in scope)",
            policy_name,
            len(records),
            len(scoped),
        )

        name_source = _cfg(config, "name_source", "hostname")
        entities = mapper.devices_to_entities(
            scoped,
            default_site=default_site,
            name_source=name_source,
        )

        entities.extend(self._port_entities(client, scoped, name_source, policy_name))

        return entities

    @staticmethod
    def _correlated_records(client: PlatformOneClient, assets: list[dict], policy_name: str) -> list[dict]:
        """Join each Assets device with its ConfigState identity + location.

        A ConfigState outage degrades to Assets-only data (flat site, no
        ports) instead of failing the sync: Diode ingestion is upsert-style,
        so a tick without building/floor/port detail is harmless.
        """
        try:
            cs_devices = list(client.retrieve("asset-device"))
        except PlatformOneApiError as exc:
            logger.warning(
                "Policy %s: ConfigState device listing failed, syncing without location/port detail: %s",
                policy_name,
                exc,
            )
            cs_devices = []
        cs_by_asset_id = _correlate(assets, cs_devices)

        locations: dict[str, dict] = {}
        cs_uuids = sorted({str(cs["id"]) for cs in cs_by_asset_id.values() if cs.get("id")})
        if cs_uuids:
            try:
                locations = {
                    str(loc["asset_device_id"]): loc
                    for loc in client.retrieve("asset-location", {"asset_device_id": cs_uuids})
                    if loc.get("asset_device_id")
                }
            except PlatformOneApiError as exc:
                logger.warning(
                    "Policy %s: ConfigState location fetch failed, falling back to Assets site names: %s",
                    policy_name,
                    exc,
                )

        records = []
        for asset in assets:
            cs = cs_by_asset_id.get(asset.get("device_id"))
            cs_device_id = str(cs["id"]) if cs and cs.get("id") else None
            records.append(
                {
                    "asset": asset,
                    "cs_device_id": cs_device_id,
                    "location": locations.get(cs_device_id) if cs_device_id else None,
                }
            )
        return records

    @staticmethod
    def _port_entities(
        client: PlatformOneClient, records: list[dict], name_source: str, policy_name: str
    ) -> list[Entity]:
        """One batched ConfigState call per port table, covering every
        in-scope switch that resolved to a ConfigState device.

        A failed table degrades that table's fields for this tick instead of
        aborting the sync; ports still map from whichever tables survived.
        """
        switches = {
            record["cs_device_id"]: record
            for record in records
            if record["cs_device_id"] and is_switch(record["asset"].get("function"))
        }
        if not switches:
            return []
        device_ids = sorted(switches)

        tables_by_device: dict[str, dict[str, list[dict]]] = {
            device_id: {key: [] for key in PORT_TABLES} for device_id in device_ids
        }
        for key, (table, filter_field) in PORT_TABLES.items():
            try:
                rows = client.retrieve(table, {filter_field: device_ids})
                for row in rows:
                    device_id = str(row.get("asset_device_id") or row.get("device_id") or "")
                    if device_id in tables_by_device:
                        tables_by_device[device_id][key].append(row)
            except PlatformOneApiError as exc:
                logger.warning(
                    "Policy %s: ConfigState %s fetch failed, ports sync without it: %s",
                    policy_name,
                    table,
                    exc,
                )

        entities: list[Entity] = []
        for device_id in device_ids:
            record = switches[device_id]
            entities.extend(
                mapper.ports_to_entities(
                    tables_by_device[device_id],
                    device=device_name(record["asset"], name_source),
                )
            )
        logger.info("Policy %s: mapped %d wired port entities", policy_name, len(entities))
        return entities


def _load_env_file(path: str = ".env") -> None:
    """Read KEY=VALUE lines into os.environ; exported variables take precedence."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _standalone_config() -> dict:
    return {
        "package": "orb_extreme_platformone",
        "BOOTSTRAP": _env_bool("BOOTSTRAP", False),
        "NETBOX_API_URL": os.environ.get("NETBOX_API_URL"),
        "NETBOX_API_TOKEN": os.environ.get("NETBOX_API_TOKEN"),
        "PLATFORMONE_API_TOKEN": os.environ.get("PLATFORMONE_API_TOKEN"),
        "classification": os.environ.get("PLATFORMONE_CLASSIFICATION", DEFAULT_CLASSIFICATION),
        "name_source": os.environ.get("PLATFORMONE_NAME_SOURCE", "hostname"),
        "default_site": os.environ.get("PLATFORMONE_DEFAULT_SITE", DEFAULT_SITE),
    }


def main() -> None:
    """Standalone dry run: fetch from Platform ONE, map, print the entities (no Diode push)."""
    logging.basicConfig(level=logging.INFO)
    _load_env_file()
    policy = Policy(config=Config(**_standalone_config()), scope={"sites": ["*"]})
    backend = Backend()
    for entity in backend.run("standalone", policy):
        ts = entity.timestamp.ToDatetime(tzinfo=timezone.utc).astimezone()
        entity.ClearField("timestamp")
        print(f"timestamp: {ts.isoformat(timespec='seconds')}")
        print(entity)


if __name__ == "__main__":
    main()
