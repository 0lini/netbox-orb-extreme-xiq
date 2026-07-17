"""Orb Agent worker entrypoint for the Extreme Platform ONE integration.

Implements the `worker.backend.Backend` contract from `netboxlabs-orb-worker`:
`describe()` reports identity, `run()` returns the Diode entities for one
policy tick. The PolicyRunner owns scheduling and the Diode client; this
module only produces entities.

The per-tick API call budget is flat, not per-device: one paginated Assets
listing, one serial-filtered ConfigState device retrieval (for correlation),
then one batched ConfigState call per port/LAG/capabilities/PoE-state table
covering every in-scope switch at once (independent tables run concurrently),
optional LAG member-port retrieves when nested members are absent, optional
PoE-config and interface-IP retrieves filtered by collected interface UUIDs
(also concurrent within each dependent phase), plus one InferredDevice
retrieve and up to two InferredCluster retrieves (device_one_id /
device_two_id) for VirtualChassis membership.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

from google.protobuf.json_format import MessageToDict
from netboxlabs.diode.sdk.ingester import Entity
from worker.backend import Backend as WorkerBackend
from worker.models import Config, Metadata, Policy

from . import __version__, bootstrap, mapper
from .client import DEFAULT_BASE_URL, PlatformOneApiError, PlatformOneClient
from .identity import device_name, is_switch

logger = logging.getLogger(__name__)

APP_NAME = "netbox-orb-extreme-platformone"
APP_VERSION = __version__
DEFAULT_CLASSIFICATION = "SWITCH"

# {mapper table key: (retrieve-* table, GetRequest device filter field)}.
# vlan-properties and poe-state use `device_id`; capabilities use
# `asset_device_id` like port config/state.
PORT_TABLES = {
    "port_configs": ("asset-port-config", "asset_device_id"),
    "port_states": ("asset-port-state", "asset_device_id"),
    "vlan_properties": ("asset-interface-vlan-properties", "device_id"),
    "lag_configs": ("asset-lag-config", "asset_device_id"),
    "lag_states": ("asset-lag-state", "asset_device_id"),
    "port_capabilities": ("asset-port-capabilities", "asset_device_id"),
    "poe_states": ("asset-poe-power-ports-state", "device_id"),
}

# Nested `member_ports` on AssetLagConfig/State may be empty on retrieve;
# fall back to the dedicated member-port tables filtered by lag row id.
LAG_MEMBER_TABLES = {
    "lag_configs": ("asset-lag-config-member-port", "asset_lag_config_id"),
    "lag_states": ("asset-lag-state-member-port", "asset_lag_state_id"),
}

# Tables that only filter by asset_interface_id (no device filter). Fetched
# after port/LAG rows are collected so interface UUIDs are known.
INTERFACE_ID_TABLES = {
    "poe_configs": ("asset-poe-power-ports-config", "asset_interface_id"),
    "interface_ips": ("asset-interface-ip-address", "asset_interface_id"),
}

# InferredCluster.device_one_id / device_two_id are InferredDevice UUIDs
# ("User device" in the ConfigState schema), not AssetDevice UUIDs. Resolve
# AssetDevice -> InferredDevice first, then query both member sides and merge
# by cluster id.
CLUSTER_MEMBER_FILTERS = ("device_one_id", "device_two_id")


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


def _colon_mac(value) -> str:
    """Colon-separated lowercase MAC: the format ConfigState filters expect."""
    digits = _normalize_mac(value)
    return ":".join(digits[i : i + 2] for i in range(0, len(digits), 2))


def _retrieve_parallel(
    client: PlatformOneClient, jobs: list[tuple[str, dict]]
) -> list[tuple[str, list[dict]]]:
    """Run independent ConfigState retrieves concurrently.

    Returns one ``(table, rows)`` result per job in submission order
    (deterministic merge). A job's PlatformOneApiError propagates out of
    ``result()`` and aborts the batch.
    """
    if not jobs:
        return []

    def _one(table: str, filters: dict) -> tuple[str, list[dict]]:
        return table, list(client.retrieve(table, filters))

    workers = min(len(jobs), 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, table, filters) for table, filters in jobs]
        # result() in submit order: work still overlaps; merge stays deterministic.
        return [fut.result() for fut in futures]


def _fetch_cs_devices(client: PlatformOneClient, assets: list[dict]) -> list[dict]:
    """Fetch the ConfigState AssetDevice records for the given Assets devices.

    ConfigState rejects an empty GetRequest body (code 1727: at least one
    filter attribute is required), so the listing is filtered by the Assets
    serial numbers, plus a colon-format MAC query for serial-less assets.
    """
    serials = sorted({str(a["serial_number"]) for a in assets if a.get("serial_number")})
    macs = sorted(
        {_colon_mac(a["mac_address"]) for a in assets if not a.get("serial_number") and a.get("mac_address")}
    )
    records: dict[str, dict] = {}
    if serials:
        for rec in client.retrieve("asset-device", {"serial_number": serials}):
            records[str(rec.get("id"))] = rec
    if macs:
        for rec in client.retrieve("asset-device", {"base_mac_address": macs}):
            records.setdefault(str(rec.get("id")), rec)
    return list(records.values())


def _index_unique(items: list[dict], key_fn, *, label: str) -> dict:
    """Build {key: item}, keeping the first on collision and warning."""
    index: dict = {}
    for item in items:
        key = key_fn(item)
        if not key:
            continue
        if key in index:
            logger.warning(
                "Duplicate ConfigState AssetDevice %s %r; keeping the first match",
                label,
                key,
            )
            continue
        index[key] = item
    return index


def _correlate(assets: list[dict], cs_devices: list[dict]) -> dict[int, dict]:
    """Match Assets devices to ConfigState AssetDevice records.

    Returns {Assets device_id: ConfigState device record}. Serial number is
    the primary key; base MAC and management IP are fallbacks. Devices with
    no match have no ConfigState data yet and still sync as Devices, minus
    ports and building/floor detail.
    """
    by_serial = _index_unique(
        cs_devices,
        lambda d: str(d["serial_number"]).casefold() if d.get("serial_number") else None,
        label="serial_number",
    )
    by_mac = _index_unique(
        cs_devices,
        lambda d: _normalize_mac(d["base_mac_address"]) if d.get("base_mac_address") else None,
        label="base_mac_address",
    )
    by_ip = _index_unique(
        cs_devices,
        lambda d: str(d["ip_address"]) if d.get("ip_address") else None,
        label="ip_address",
    )

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
        entities = mapper.devices_to_entities(
            scoped,
            name_source=name_source,
            virtual_chassis_entities=vc_entities,
            vc_memberships=vc_memberships,
        )

        entities.extend(self._port_entities(client, scoped, name_source, policy_name))

        return entities

    @staticmethod
    def _asset_id_for_cluster_member(
        cluster: dict, member_field: str, nested_field: str, inferred_to_asset: dict[str, str]
    ) -> str:
        """Resolve an InferredCluster member to an AssetDevice UUID.

        Prefers the InferredDevice lookup map; falls back to a nested
        `device_one` / `device_two` object's `asset_device_id` when the API
        expands it (undocumented on InferredCluster but present in practice).
        """
        raw = str(cluster.get(member_field) or "")
        if raw in inferred_to_asset:
            return inferred_to_asset[raw]
        nested = cluster.get(nested_field)
        if isinstance(nested, dict):
            nested_asset_id = str(nested.get("asset_device_id") or "")
            if nested_asset_id:
                return nested_asset_id
        return raw

    @staticmethod
    def _fetch_inferred_clusters(client: PlatformOneClient, asset_device_ids: list[str]) -> list[dict]:
        """Fetch InferredCluster rows for the given AssetDevice UUIDs.

        Filtering `retrieve-inferred-cluster` by AssetDevice UUIDs silently
        returns zero rows: `device_one_id` / `device_two_id` are InferredDevice
        UUIDs. Resolve via `retrieve-inferred-device` (`asset_device_id`), query
        both cluster member filters, then rewrite member IDs back to
        AssetDevice UUIDs so the mapper can join on `cs_device_id`.
        """
        if not asset_device_ids:
            return []

        inferred_to_asset: dict[str, str] = {}
        for device in client.retrieve("inferred-device", {"asset_device_id": asset_device_ids}):
            inferred_id = str(device.get("id") or "")
            asset_id = str(device.get("asset_device_id") or "")
            if inferred_id and asset_id:
                inferred_to_asset[inferred_id] = asset_id
        if not inferred_to_asset:
            return []

        inferred_ids = sorted(inferred_to_asset)
        by_id: dict[str, dict] = {}
        for filter_field in CLUSTER_MEMBER_FILTERS:
            for cluster in client.retrieve("inferred-cluster", {filter_field: inferred_ids}):
                remapped = {
                    **cluster,
                    "device_one_id": Backend._asset_id_for_cluster_member(
                        cluster, "device_one_id", "device_one", inferred_to_asset
                    ),
                    "device_two_id": Backend._asset_id_for_cluster_member(
                        cluster, "device_two_id", "device_two", inferred_to_asset
                    ),
                }
                cluster_id = str(remapped.get("id") or "")
                if cluster_id:
                    by_id[cluster_id] = remapped
                else:
                    # Spec marks id optional in practice; fall back to member pair.
                    key = f"{remapped.get('device_one_id')}:{remapped.get('device_two_id')}"
                    by_id.setdefault(key, remapped)
        return list(by_id.values())

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
            clusters = Backend._fetch_inferred_clusters(client, device_ids)
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
    def _correlated_records(client: PlatformOneClient, assets: list[dict], policy_name: str) -> list[dict]:
        """Join each Assets device with its ConfigState identity + location.

        A ConfigState outage degrades to Assets-only data (flat site, no
        ports) instead of failing the sync: Diode ingestion is upsert-style,
        so a tick without building/floor/port detail is harmless.
        """
        try:
            cs_devices = _fetch_cs_devices(client, assets)
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
                for loc in client.retrieve("asset-location", {"asset_device_id": cs_uuids}):
                    device_id = str(loc.get("asset_device_id") or "")
                    if not device_id:
                        continue
                    if device_id in locations:
                        logger.warning(
                            "Policy %s: duplicate asset-location for device %s; keeping the first",
                            policy_name,
                            device_id,
                        )
                        continue
                    locations[device_id] = loc
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
                    "cs_device": cs,
                    "location": locations.get(cs_device_id) if cs_device_id else None,
                }
            )
        return records

    @staticmethod
    def _attach_lag_members(
        client: PlatformOneClient,
        tables_by_device: dict[str, dict[str, list[dict]]],
    ) -> None:
        """Fill empty nested `member_ports` from dedicated member-port retrieves.

        Member-port GetRequests filter by lag row id (not device id), so this
        runs after lag config/state rows are collected. Mutates rows in place.
        The two member-port tables are independent of each other and fetch
        concurrently.
        """
        jobs: list[tuple[str, dict]] = []
        job_meta: list[tuple[str, dict[str, dict]]] = []
        for table_key, (member_table, id_field) in LAG_MEMBER_TABLES.items():
            lag_ids: list[str] = []
            rows_by_id: dict[str, dict] = {}
            for tables in tables_by_device.values():
                for row in tables.get(table_key) or []:
                    lag_id = str(row.get("id") or "")
                    if not lag_id:
                        continue
                    if row.get("member_ports"):
                        continue
                    lag_ids.append(lag_id)
                    rows_by_id[lag_id] = row
            if not lag_ids:
                continue
            jobs.append((member_table, {id_field: sorted(set(lag_ids))}))
            job_meta.append((id_field, rows_by_id))

        for (id_field, rows_by_id), (_table, members) in zip(
            job_meta, _retrieve_parallel(client, jobs), strict=True
        ):
            by_lag: dict[str, list[dict]] = {}
            for member in members:
                parent_id = str(member.get(id_field) or "")
                if parent_id:
                    by_lag.setdefault(parent_id, []).append(member)
            for lag_id, row in rows_by_id.items():
                if lag_id in by_lag:
                    row["member_ports"] = by_lag[lag_id]

    @staticmethod
    def _collect_interface_ids(
        tables_by_device: dict[str, dict[str, list[dict]]],
    ) -> tuple[list[str], dict[str, str]]:
        """Collect asset_interface_id values and map each to its device UUID.

        Includes vlan_properties so interface-IP / PoE-config retrieves cover
        VLAN-facing interfaces that never appear in port/LAG/PoE-state rows.
        """
        interface_to_device: dict[str, str] = {}
        for device_id, tables in tables_by_device.items():
            for key in (
                "port_configs",
                "port_states",
                "vlan_properties",
                "lag_configs",
                "lag_states",
                "poe_states",
            ):
                for row in tables.get(key) or []:
                    interface_id = str(row.get("asset_interface_id") or "")
                    if interface_id:
                        interface_to_device.setdefault(interface_id, device_id)
        return sorted(interface_to_device), interface_to_device

    @staticmethod
    def _attach_interface_id_tables(
        client: PlatformOneClient,
        tables_by_device: dict[str, dict[str, list[dict]]],
    ) -> None:
        """Fetch PoE config + interface IPs by collected interface UUIDs.

        These ConfigState tables have no device filter; rows are bucketed back
        onto devices via the interface→device map from port/LAG/PoE-state rows.
        """
        interface_ids, interface_to_device = Backend._collect_interface_ids(tables_by_device)
        for tables in tables_by_device.values():
            for key in INTERFACE_ID_TABLES:
                tables.setdefault(key, [])
        if not interface_ids:
            return

        jobs = [
            (table, {filter_field: interface_ids})
            for table, filter_field in INTERFACE_ID_TABLES.values()
        ]
        for key, (_table, rows) in zip(INTERFACE_ID_TABLES, _retrieve_parallel(client, jobs), strict=True):
            for row in rows:
                interface_id = str(row.get("asset_interface_id") or "")
                device_id = interface_to_device.get(interface_id)
                if device_id and device_id in tables_by_device:
                    tables_by_device[device_id][key].append(row)

    @staticmethod
    def _port_entities(
        client: PlatformOneClient, records: list[dict], name_source: str, policy_name: str
    ) -> list[Entity]:
        """One batched ConfigState call per port/LAG table, covering every
        in-scope switch that resolved to a ConfigState device.

        Independent device-filtered tables fetch concurrently; LAG member-port
        and interface-UUID tables run in later phases once their filter IDs
        are known. Any table's failure aborts the tick (the full-outage case
        is handled earlier, before any cs_device_id resolves). Entity order
        stays deterministic (device_ids sorted, tables merged in PORT_TABLES
        key order).
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
        jobs = [
            (table, {filter_field: device_ids}) for table, filter_field in PORT_TABLES.values()
        ]
        for key, (_table, rows) in zip(PORT_TABLES, _retrieve_parallel(client, jobs), strict=True):
            for row in rows:
                device_id = str(row.get("asset_device_id") or row.get("device_id") or "")
                if device_id in tables_by_device:
                    tables_by_device[device_id][key].append(row)

        Backend._attach_lag_members(client, tables_by_device)
        Backend._attach_interface_id_tables(client, tables_by_device)

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
    }


def _quote_values(value):
    """Render every scalar as a string so the JSON dry-run output quotes all values."""
    if isinstance(value, dict):
        return {key: _quote_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_quote_values(item) for item in value]
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


def main() -> None:
    """Standalone dry run: fetch from Platform ONE, map, print the entities (no Diode push)."""
    logging.basicConfig(level=logging.INFO)
    _load_env_file()
    policy = Policy(config=Config(**_standalone_config()), scope={"sites": ["*"]})
    backend = Backend()
    for entity in backend.run("standalone", policy):
        data = MessageToDict(entity, preserving_proto_field_name=True)
        ts = entity.timestamp.ToDatetime(tzinfo=timezone.utc).astimezone()
        data["timestamp"] = ts.isoformat(timespec="seconds")
        print(json.dumps(_quote_values(data), indent=2, ensure_ascii=False))
        print()


if __name__ == "__main__":
    main()
