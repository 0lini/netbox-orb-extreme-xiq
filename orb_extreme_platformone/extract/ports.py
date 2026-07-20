"""Batched ConfigState port / LAG / VLAN / PoE / interface-IP extracts."""

from __future__ import annotations

from orb_extreme_platformone.client import PlatformOneClient

from .retrieve import extract_device_table_buckets, retrieve_ok
from .tables import INTERFACE_ID_TABLES, PORT_TABLES


def collect_interface_ids(
    tables_by_device: dict[str, dict[str, list[dict]]],
) -> dict[str, str]:
    """Map each collected asset_interface_id to its device UUID.

    Scans tables that carry ``asset_interface_id`` (port/LAG/VLAN/PoE-state).
    ``port_capabilities`` has no interface UUID and is skipped. VLAN rows
    matter so interface-IP retrieves cover VLAN-facing interfaces that never
    appear in port/LAG/PoE-state rows.
    """
    # Keys whose ConfigState rows expose asset_interface_id (see PORT_TABLES).
    source_keys = (
        "port_configs",
        "port_states",
        "vlan_properties",
        "lag_configs",
        "lag_states",
        "poe_states",
    )
    interface_to_device: dict[str, str] = {}
    for device_id, tables in tables_by_device.items():
        for key in source_keys:
            for row in tables.get(key) or []:
                interface_id = str(row.get("asset_interface_id") or "")
                if interface_id:
                    interface_to_device.setdefault(interface_id, device_id)
    return interface_to_device


def attach_interface_id_tables(
    client: PlatformOneClient,
    tables_by_device: dict[str, dict[str, list[dict]]],
    policy_name: str,
    failed_tables: list[str],
) -> None:
    """Fetch interface IPs by collected interface UUIDs.

    ``retrieve-asset-interface-ip-address`` has no device filter; rows are
    bucketed back onto devices via the interface→device map from port/LAG/
    VLAN/PoE-state rows.
    """
    interface_to_device = collect_interface_ids(tables_by_device)
    for tables in tables_by_device.values():
        for key in INTERFACE_ID_TABLES:
            tables.setdefault(key, [])
    if not interface_to_device:
        return

    interface_ids = sorted(interface_to_device)
    jobs = [(table, {filter_field: interface_ids}) for table, filter_field in INTERFACE_ID_TABLES.values()]
    for key, rows in retrieve_ok(
        client,
        jobs,
        list(INTERFACE_ID_TABLES),
        policy_name=policy_name,
        failed_tables=failed_tables,
        degradation="ports sync without it",
    ):
        for row in rows:
            interface_id = str(row.get("asset_interface_id") or "")
            device_id = interface_to_device.get(interface_id)
            if device_id and device_id in tables_by_device:
                tables_by_device[device_id][key].append(row)


def extract_port_tables(
    client: PlatformOneClient,
    device_ids: list[str],
    policy_name: str,
) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    """Batched device-filtered port/LAG tables, then interface-UUID tables.

    Returns ``(tables_by_device, failed_tables)``. Independent device-filtered
    tables retrieve concurrently; interface-IP tables run afterward once
    ``asset_interface_id`` values are known. LAG membership comes from
    nested ``member_ports`` on lag-config rows.
    """
    tables_by_device, failed_tables = extract_device_table_buckets(
        client,
        device_ids,
        PORT_TABLES,
        policy_name=policy_name,
        degradation="ports sync without it",
    )
    attach_interface_id_tables(client, tables_by_device, policy_name, failed_tables)
    return tables_by_device, failed_tables
