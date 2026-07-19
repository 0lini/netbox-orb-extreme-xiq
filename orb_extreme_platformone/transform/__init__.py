"""Transform Extreme Platform ONE records into Diode entities.

Fields are asserted unconditionally whenever Platform ONE reports the
underlying data; fields with no Platform ONE equivalent are never asserted.
Device identity uses the native `serial` field plus deterministic names
(see `identity`), with `platformone_*` custom fields carried as provenance.

Callers pass "device records" pre-joined by backend.py:
{"asset": <Assets Device>, "cs_device_id": str | None,
 "cs_device": <ConfigState AssetDevice> | None,
 "location": <AssetLocation> | None}.

InferredCluster rows map via `virtual_chassis_to_entities`. LAG interfaces
via `ports_to_entities`. AP radios and WLANs via `radios_to_entities`.
"""

from __future__ import annotations

from .devices import devices_to_entities, scope_devices
from .ports import PORT_ENTITY_TABLE_KEYS, ports_to_entities, primary_ips_from_tables
from .virtual_chassis import virtual_chassis_to_entities
from .wireless import WIRELESS_ENTITY_TABLE_KEYS, radios_to_entities

__all__ = [
    "PORT_ENTITY_TABLE_KEYS",
    "WIRELESS_ENTITY_TABLE_KEYS",
    "devices_to_entities",
    "ports_to_entities",
    "primary_ips_from_tables",
    "radios_to_entities",
    "scope_devices",
    "virtual_chassis_to_entities",
]
