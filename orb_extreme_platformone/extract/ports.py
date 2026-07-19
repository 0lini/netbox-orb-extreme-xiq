"""Batched ConfigState port / LAG / VLAN / PoE / interface-IP fetches."""

from __future__ import annotations

from orb_extreme_platformone.client import PlatformOneClient

from .retrieve import retrieve_ok
from .tables import INTERFACE_ID_TABLES, LAG_MEMBER_TABLES, PORT_TABLES


def attach_lag_members(
    client: PlatformOneClient,
    tables_by_device: dict[str, dict[str, list[dict]]],
    policy_name: str,
    failed_tables: list[str],
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
        rows_by_id: dict[str, dict] = {}
        for tables in tables_by_device.values():
            for row in tables.get(table_key) or []:
                lag_id = str(row.get("id") or "")
                if lag_id and not row.get("member_ports"):
                    rows_by_id[lag_id] = row
        if not rows_by_id:
            continue
        jobs.append((member_table, {id_field: sorted(rows_by_id)}))
        job_meta.append((id_field, rows_by_id))

    for (id_field, rows_by_id), members in retrieve_ok(
        client,
        jobs,
        job_meta,
        policy_name=policy_name,
        failed_tables=failed_tables,
        degradation="LAG membership may be incomplete",
    ):
        by_lag: dict[str, list[dict]] = {}
        for member in members:
            parent_id = str(member.get(id_field) or "")
            if parent_id:
                by_lag.setdefault(parent_id, []).append(member)
        for lag_id, row in rows_by_id.items():
            if lag_id in by_lag:
                row["member_ports"] = by_lag[lag_id]


def collect_interface_ids(
    tables_by_device: dict[str, dict[str, list[dict]]],
) -> dict[str, str]:
    """Map each collected asset_interface_id to its device UUID.

    Scans every PORT_TABLES key: vlan_properties rows matter so
    interface-IP / PoE-config retrieves cover VLAN-facing interfaces that
    never appear in port/LAG/PoE-state rows; port_capabilities rows carry
    no asset_interface_id and contribute nothing.
    """
    interface_to_device: dict[str, str] = {}
    for device_id, tables in tables_by_device.items():
        for key in PORT_TABLES:
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
    """Fetch PoE config + interface IPs by collected interface UUIDs.

    These ConfigState tables have no device filter; rows are bucketed back
    onto devices via the interface→device map from port/LAG/PoE-state rows.
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
    """Batched device-filtered port/LAG tables, then dependent member/IP phases.

    Returns ``(tables_by_device, failed_tables)``. Independent device-filtered
    tables fetch concurrently; LAG member-port and interface-UUID tables run
    in later phases once their filter IDs are known.
    """
    failed_tables: list[str] = []
    tables_by_device: dict[str, dict[str, list[dict]]] = {
        device_id: {key: [] for key in PORT_TABLES} for device_id in device_ids
    }
    jobs = [(table, {filter_field: device_ids}) for table, filter_field in PORT_TABLES.values()]
    for key, rows in retrieve_ok(
        client,
        jobs,
        list(PORT_TABLES),
        policy_name=policy_name,
        failed_tables=failed_tables,
        degradation="ports sync without it",
    ):
        for row in rows:
            device_id = str(row.get("asset_device_id") or row.get("device_id") or "")
            if device_id in tables_by_device:
                tables_by_device[device_id][key].append(row)

    attach_lag_members(client, tables_by_device, policy_name, failed_tables)
    attach_interface_id_tables(client, tables_by_device, policy_name, failed_tables)
    return tables_by_device, failed_tables
