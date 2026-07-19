"""ConfigState table catalogs used by the discovery worker.

Each entry is ``transform_key -> (retrieve-* table, GetRequest filter field)``.
Transform modules derive their entity table-key frozensets from these catalogs.
"""

from __future__ import annotations

# {transform table key: (retrieve-* table, GetRequest device filter field)}.
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

# Tables that only filter by asset_interface_id (no device filter). Fetched
# after port/LAG rows are collected so interface UUIDs are known.
INTERFACE_ID_TABLES = {
    "poe_configs": ("asset-poe-power-ports-config", "asset_interface_id"),
    "interface_ips": ("asset-interface-ip-address", "asset_interface_id"),
}

# AP radio / WLAN ConfigState tables, batched by AssetDevice UUID.
WIRELESS_TABLES = {
    "wireless_interfaces": ("asset-wireless-interface", "asset_device_id"),
    "wireless_states": ("asset-wireless-interface-state", "asset_device_id"),
    "ssid_configs": ("asset-ssid-config", "asset_device_id"),
    "ssid_states": ("asset-ssid-state", "asset_device_id"),
}

# InferredCluster.device_one_id / device_two_id are InferredDevice UUIDs
# ("User device" in the ConfigState schema), not AssetDevice UUIDs. Resolve
# AssetDevice -> InferredDevice first, then query both member sides and merge
# by cluster id.
CLUSTER_MEMBER_FILTERS = ("device_one_id", "device_two_id")
