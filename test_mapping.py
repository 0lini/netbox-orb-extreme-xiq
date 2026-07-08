"""Exercise the mapping logic with stubbed Diode entities (SDK not installed)."""
import sys, json
sys.path.insert(0, ".")
from orb_extreme_xiq import mapper

# --- stub Diode ingester entities: record kwargs so we can assert on them ----
class Rec:
    def __init__(self, **kw): self.__dict__.update(kw); self._kw = kw
    def __repr__(self): return f"{type(self).__name__}({self._kw})"
class Device(Rec): pass
class DeviceType(Rec): pass
class Platform(Rec): pass
class Manufacturer(Rec): pass
class Interface(Rec): pass
class IPAddress(Rec): pass
class Site(Rec): pass
class Entity(Rec): pass
class CustomFieldValue(Rec): pass
for name, cls in dict(Device=Device, DeviceType=DeviceType, Platform=Platform,
                       Manufacturer=Manufacturer, Interface=Interface,
                       IPAddress=IPAddress, Site=Site, Entity=Entity,
                       CustomFieldValue=CustomFieldValue).items():
    setattr(mapper, name, cls)

def cf(kw):
    """Unwrap a stubbed CustomFieldValue back to its plain scalar for asserts."""
    return kw.get("text", kw.get("json"))

# --- fake XIQ data -----------------------------------------------------------
loc_tree = [{"id": 1, "name": "HQ", "children": [
    {"id": 2, "name": "Floor 1", "children": []},
    {"id": 3, "name": "Floor 2", "children": []}]}]
idx = mapper.build_location_index(loc_tree)

devices = [
    {"id": 111, "hostname": "ap-lobby", "serial_number": "SN111",
     "product_type": "AP305C", "device_function": "AP",
     "ip_address": "10.0.0.5", "connected": True, "location_id": 2,
     "software_version": "10.6r3", "network_policy_name": "Corp-WiFi",
     "mac_address": "AA:BB:CC:00:00:11", "org_id": "org-9"},
    {"id": 222, "hostname": "sw-idf1", "serial_number": "SN222",
     "product_type": "5420F", "device_function": "SWITCH",
     "ip_address": "10.0.0.6", "connected": False, "location_id": 3,
     "org_id": "org-9"},
]

mapping = {"HQ": "Corporate-HQ"}   # both floors roll up to HQ -> one site
ents = mapper.devices_to_entities(devices, location_index=idx,
                                  location_site_mapping=mapping,
                                  default_site="XIQ-Unmapped")

# --- assertions --------------------------------------------------------------
sites = [e.__dict__["site"] for e in ents if "site" in e.__dict__ and isinstance(e.__dict__["site"], Site)]
site_entities = [e for e in ents if "site" in e._kw and getattr(e._kw.get("site"), "__dict__", {}).get("custom_fields")]
devs = [e._kw["device"] for e in ents if "device" in e._kw]

# 1. consolidation: exactly ONE Site entity (both floors -> Corporate-HQ)
assert len(site_entities) == 1, site_entities
xiq_locs = json.loads(cf(site_entities[0]._kw["site"]._kw["custom_fields"]["xiq_locations"]._kw))
assert xiq_locs == ["HQ"], xiq_locs
print("consolidation OK -> 1 site, xiq_locations:", xiq_locs)

# 2. device carries custom fields (immutable id anchor) + tags + site
d0 = devs[0]
assert cf(d0._kw["custom_fields"]["xiq_device_id"]._kw) == "111"
assert cf(d0._kw["custom_fields"]["xiq_network_policy"]._kw) == "Corp-WiFi"
assert d0._kw["site"]._kw["name"] == "Corporate-HQ"
assert "source:xiq" in d0._kw["tags"] and "xiq-org:org-9" in d0._kw["tags"]
assert d0._kw["role"] == "wireless-ap"
assert d0._kw["status"] == "active"
print("device0 OK:", d0._kw["name"], d0._kw["role"], d0._kw["tags"])

# 3. switch with no policy: empty custom fields dropped, status offline
d1 = devs[1]
assert "xiq_network_policy" not in d1._kw["custom_fields"]
assert d1._kw["status"] == "offline"
print("device1 OK: empty CF dropped, status offline")

# 4. FIELD AUTHORITY: remove 'site' -> device has no site, no Site entities
ents2 = mapper.devices_to_entities(devices, location_index=idx,
                                   location_site_mapping=mapping,
                                   default_site="XIQ-Unmapped",
                                   authority=set(mapper.DEFAULT_AUTHORITY) - {"site"})
devs2 = [e._kw["device"] for e in ents2 if "device" in e._kw]
assert "site" not in devs2[0]._kw, "site should be omitted when human-owned"
assert not any("site" in e._kw and getattr(e._kw["site"], "_kw", {}).get("custom_fields") for e in ents2)
print("field authority OK: dropping 'site' omits it entirely (no re-drift)")

print("\nALL MAPPING TESTS PASSED")
