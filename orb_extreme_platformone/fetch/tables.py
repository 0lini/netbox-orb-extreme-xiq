"""ConfigState table catalogs used by the discovery worker.

Each entry is ``mapper_key -> (retrieve-* table, GetRequest filter field)``.
Mapper entity table-key frozensets must stay aligned with these catalogs
(see tests).
"""

from __future__ import annotations

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
