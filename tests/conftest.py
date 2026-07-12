"""Shared pytest fixtures for orb_extreme_xiq tests."""

from __future__ import annotations

import pytest

from orb_extreme_xiq import mapper


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


class Manufacturer(Rec):
    pass


class Interface(Rec):
    pass


class IPAddress(Rec):
    pass


class Site(Rec):
    pass


class Location(Rec):
    pass


class WirelessLAN(Rec):
    pass


class Entity(Rec):
    pass


class CustomFieldValue(Rec):
    pass


STUB_CLASSES = {
    "Device": Device,
    "DeviceType": DeviceType,
    "Platform": Platform,
    "Manufacturer": Manufacturer,
    "Interface": Interface,
    "IPAddress": IPAddress,
    "Site": Site,
    "Location": Location,
    "WirelessLAN": WirelessLAN,
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
        # raising=False: mapper.py only imports the SDK classes it actually
        # uses, so not every stub (e.g. Interface, IPAddress) is already an
        # attribute of the module.
        monkeypatch.setattr(mapper, name, cls, raising=False)
    return STUB_CLASSES


def cf(custom_field_value_kw: dict):
    """Unwrap a stubbed CustomFieldValue's kwargs back to its plain scalar."""
    return custom_field_value_kw.get("text", custom_field_value_kw.get("json"))
