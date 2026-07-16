"""Shared pytest fixtures for orb_extreme_platformone tests."""

from __future__ import annotations

import pytest

from orb_extreme_platformone import mapper

# ---------------------------------------------------------------------------
# Shared Platform ONE payload shapes (Assets Device + ConfigState port rows)
# ---------------------------------------------------------------------------

SWITCH_ASSET = {
    "device_id": 42,
    "host_name": "sw-idf1",
    "serial_number": "SN42",
    "mac_address": "aabbccddeeff",
    "product_type": "FabricEngine_5320_48P_8XE",
    "function": "Fabric Engine",
    "os_version": "9.2.1.0",
    "is_connected": True,
    "ip_address": "10.0.0.2",
    "site_name": "Assets-Site",
}

CS_SWITCH = {
    "id": "cs-uuid-42",
    "serial_number": "SN42",
    "base_mac_address": "AA:BB:CC:DD:EE:FF",
}

PORT_CONFIG = {
    "asset_device_id": "cs-uuid-42",
    "asset_interface_id": "if-uuid-1",
    "name": "1/1",
    "enabled": True,
    "description": "uplink to core",
}

PORT_STATE = {
    "asset_device_id": "cs-uuid-42",
    "asset_interface_id": "if-uuid-1",
    "name": "1/1",
    "oper_state": 1,
    "oper_speed": 4,
    "oper_duplex": 2,
    "connector_type": 1,
    "mac_address": "aa:bb:cc:dd:ee:01",
    "if_index": 1,
}

VLAN_PROPERTIES = {
    "device_id": "cs-uuid-42",
    "asset_interface_id": "if-uuid-1",
    "interface_name": "1/1",
    "port_vlan": 10,
    "vlans": [{"vlan_number": 10}, {"vlan_number": 20}, {"vlan_number": 30}],
}


class Rec:
    """Records constructor kwargs so tests can assert on them without the real protobuf SDK."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self._kw = kw

    def __repr__(self):
        return f"{type(self).__name__}({self._kw})"


class Device(Rec):
    pass


class DeviceType(Rec):
    pass


class Platform(Rec):
    pass


class Interface(Rec):
    pass


class Site(Rec):
    pass


class Location(Rec):
    pass


class VLAN(Rec):
    pass


class VirtualChassis(Rec):
    pass


class DeviceRole(Rec):
    pass


class IPAddress(Rec):
    pass


class Entity(Rec):
    pass


class CustomFieldValue(Rec):
    pass


# One stub per Diode SDK class mapper.py imports.
STUB_CLASSES = {
    "Device": Device,
    "DeviceType": DeviceType,
    "DeviceRole": DeviceRole,
    "Platform": Platform,
    "Interface": Interface,
    "IPAddress": IPAddress,
    "Site": Site,
    "Location": Location,
    "VLAN": VLAN,
    "VirtualChassis": VirtualChassis,
    "Entity": Entity,
    "CustomFieldValue": CustomFieldValue,
}


@pytest.fixture
def stub_sdk(monkeypatch):
    """Swap the real Diode SDK classes `mapper` imported for kwargs-recording stubs.

    The real classes build protobuf messages, which are awkward to assert on
    directly; these stand-ins record constructor kwargs on `._kw` instead, so
    tests can assert on the *shape* of what mapper.py builds.
    """
    for name, cls in STUB_CLASSES.items():
        monkeypatch.setattr(mapper, name, cls)
    return STUB_CLASSES


def cf(custom_field_value_kw: dict):
    """Unwrap a stubbed CustomFieldValue's kwargs back to its plain scalar."""
    return custom_field_value_kw.get("text", custom_field_value_kw.get("json"))
