# orb-extreme-xiq

An ExtremeCloud IQ (XIQ) discovery worker for the NetBox Labs **Orb Agent**. It
pulls inventory from the XIQ cloud API and ingests it into NetBox via **Diode**,
mirroring the proprietary `nbl_cisco_meraki` integration — but this one is yours
and runs on the free, open-source `netboxlabs/orb-agent:latest` image. No Orb
Agent Pro / private registry required.

```
XIQ cloud API ─► orb_extreme_xiq (collector + mapper) ─► Diode ─► NetBox (+Assurance if licensed)
```

## Scope: basic inventory only (for now)

This worker deliberately keeps a small footprint: devices (name, serial,
status, site) and, optionally, their interfaces. It does **not** assert
device_type, platform, description, or primary IP — those stay entirely
NetBox/human-owned. This keeps the field set, the custom fields, and the
mapping code small and easy to reason about. See the Roadmap for what's
intentionally left out for now.

## Assurance-ready by design

Assurance is a **consumer-side** feature: there's no separate API to code
against. Any source that ingests via Diode surfaces as deviations once an
Assurance license is enabled — with **zero code changes** to this worker. So
"future-proofing" just means producing clean, stable Diode output:

- **Fixed, minimal field set.** The worker only ever asserts name, serial,
  status, and site — never rack, tenant, description, etc. — so human-owned
  fields can never generate phantom drift.
- **Stable identity** (`identity.py`). Deterministic device names; `serial` is
  asserted natively on the NetBox Device rather than via a separate immutable
  ID custom field -- neither the real Cisco Meraki integration nor NetBox
  Labs' generic discovery backends carry one; they rely on the native
  `serial` field the same way.
- **Direct site mapping.** Each XIQ location a device belongs to becomes a
  NetBox site of the same name (1:1); `default_site` is used only when a
  device's location is missing or unresolvable.
- **Bootstrap step** (`bootstrap.py`). Idempotently creates the custom-field
  definitions + `extreme-networks`/`xiq`/`discovered` tags before the first
  sync — the same first-run pattern the official integrations use.
- **Stable producer + tags.** Fixed `app_name="orb-extreme-xiq"` and flat
  `extreme-networks` / `xiq` / `discovered` tags (mirroring how NetBox Labs'
  own Cisco Meraki integration tags synced data — `cisco` / `meraki` /
  `discovered` — rather than one namespaced tag) keep your XIQ data cleanly
  attributable/filterable in Assurance.

## Layout

- `client.py` — thin XIQ client: paginated `/devices` and `/locations/tree` via
  the official `extremecloudiq-api` SDK (token or user/pass), plus a plain
  `requests` call for the legacy per-switch `/xiq/v0/monitor/device/wired/portlist`
  endpoint the SDK doesn't cover.
- `identity.py` — stable device naming + direct (1:1) site resolution.
- `mapper.py` — XIQ → Diode entities: fixed basic device/site fields, custom fields, tags.
- `bootstrap.py` — one-time idempotent NetBox schema setup (custom fields + tags).
- `backend.py` — worker entrypoint + standalone runner.
- `agent.yaml` — example policy (bootstrap, site mapping, field authority).
- `tests/` — pytest suite: `test_mapper.py`/`test_identity.py` are offline
  (stubbed SDK, no network); `test_client.py`/`test_backend.py` monkeypatch the
  XIQ SDK's Api classes directly (it talks HTTP via urllib3, not `requests`, so
  `responses` can't intercept it -- see test_client.py's docstring) plus
  `responses` for the still-`requests`-based legacy port-list call;
  `test_bootstrap.py` mocks the NetBox REST API with `responses`;
  `test_openapi_contract.py` is the one exception to "offline" -- see below.

## Contract check against the live XIQ OpenAPI spec

`tests/test_openapi_contract.py` fetches `https://api.extremecloudiq.com/openapi`
(unauthenticated, always the current spec) and asserts `/devices` and
`/locations/tree` still have the query params `client.py` hardcodes
(`page`/`limit`/`views`/`locationIds`, `parentId`/`expandChildren`) -- a
tripwire for upstream renames/removals. It's marked `contract` and excluded
from the default `pytest` run (`addopts = "-m 'not contract'"` in
pyproject.toml) so normal test runs stay fast and offline; run it explicitly
with `pytest -m contract`, or via the scheduled weekly `contract` job in
`.github/workflows/ci.yml`.

Deliberately scoped to paths/param *names*, not response body field names:
the live spec's own `XiqDevice` schema entry (`PagedXiqDevice.data`'s items)
is a different, thinner schema than what the installed SDK actually
deserializes at runtime -- confirmed by inspecting the installed package
directly, so it's not a reliable signal for response-shape drift. `mapper.py`'s
field assumptions are instead exercised against real recorded responses in
`test_mapper.py`/`test_backend.py`.

## First-run flow (mirrors Meraki/ACI)

1. Set `BOOTSTRAP: true` and provide `NETBOX_API_URL` + `NETBOX_API_TOKEN`
   (one-time, to create the custom fields + tags). Run once.
2. Set `BOOTSTRAP: false` for all scheduled runs afterward.

Bootstrap uses the NetBox REST API because field *definitions* are schema and
that path works regardless of Diode SDK version. If your SDK exposes a
CustomField ingest entity you can move it into the Diode path to match Meraki
exactly. Bootstrap **skips gracefully** if no NetBox token is set.

## Develop without the full agent

```bash
pip install -e ".[dev]"
pip install --no-deps "extremecloudiq-api==25.11.1.post3"  # see note below -- must be separate
export XIQ_API_TOKEN=...                 # or XIQ_USERNAME / XIQ_PASSWORD
python -m orb_extreme_xiq.backend        # DRY RUN: fetches from XIQ, maps, prints entities (no Diode push)
pytest                                    # full test suite
ruff check .                              # lint
```

**Why two install commands:** `extremecloudiq-api`'s metadata requires
`typing-extensions~=4.3.0`; `netboxlabs-orb-worker`'s requires `~=4.5`. Those
ranges don't overlap at all, so `pip install extremecloudiq-api
netboxlabs-orb-worker` fails with `ResolutionImpossible` even with nothing
else from this project involved -- confirmed directly. There is no dependency
declaration that fixes this (that's *why* `extremecloudiq-api` isn't in
`pyproject.toml`'s normal `dependencies`), so it must always be installed
separately with `--no-deps`, which skips checking its own declared
dependencies -- the packages it actually needs at runtime (`frozendict`,
`certifi`, `python-dateutil`, `urllib3`) are covered by this project's normal
dependencies at versions that don't conflict with anything else.

**Deploying via the real Orb Agent container:** this project's own install
(`pip install -e .`/`pip install .`, e.g. via `workers.txt`'s
`INSTALL_WORKERS_PATH` mechanism) only covers the normal dependencies. Unless
you've verified that mechanism supports a post-install hook, you'll need to
separately run the same `pip install --no-deps "extremecloudiq-api==25.11.1.post3"`
inside the container (e.g. a custom Dockerfile layer, or an entrypoint
wrapper) before the worker can actually import `extremecloudiq` -- this
hasn't been verified against a real orb-agent image.

The Orb Agent worker (`netboxlabs-orb-worker`) owns the Diode client and the
actual ingest entirely — `Backend.run()` only ever *produces* entities. There
is no dev-mode "push to Diode" path here by design; run it inside the real
`orb-agent` container (see `agent.yaml`) to actually ingest.

## Worker Backend contract (verified against `netboxlabs-orb-worker` 1.16.0)

`backend.Backend` subclasses `worker.backend.Backend` and implements:

- `describe()` (classmethod) — returns `Metadata(name, app_name, app_version, description)`
  so the worker can identify the backend before constructing it.
- `run(self, policy_name, policy, **kwargs) -> Iterable[Entity]` — does the
  XIQ fetch + mapping and returns the Diode entities for one tick; the
  PolicyRunner handles scheduling, chunking, and the Diode client itself.

If you're on a different `netboxlabs-orb-worker` version, re-check this
against the installed package: `python -c "import worker.backend as b, inspect; print(inspect.getsource(b.Backend))"`.

## Quirks in `extremecloudiq-api` 25.11.1.post3 worth knowing about

This SDK is OpenAPI Generator's verbose "oapg" style (frozendict query params,
schema-validated response bodies), which is why `client.py` calls every
endpoint with `skip_deserialization=True` and parses `result.response.data`
as plain JSON itself rather than using the SDK's own deserialization -- far
less ceremony, and the SDK still owns URL building, query serialization, the
Bearer header, and status-based `ApiException`s. Three real bugs found while
integrating it, all re-check-worthy on a version bump:

- `Configuration(access_token=...)` is a no-op — its `__init__` unconditionally
  sets `self.access_token = None` regardless of what you pass in. Set the
  attribute directly after construction instead (see `client.py`'s comment).
- Boolean FORM-style query params can't be serialized at all: the value gets
  cast through the SDK's own `BoolSchema` and then back to a native Python
  `bool` before its URI-template expansion step, which only handles
  `str`/`float`/`int` and raises `ApiValueError` for anything else. There's no
  way to route around this by wrapping the value differently -- confirmed.
  `get_location_tree`'s `expand_children=False` therefore can't be sent; it
  raises `NotImplementedError` rather than silently sending a broken request.
  (`expand_children=True`, the only value this worker needs, is simply
  omitted from the query string since it's XIQ's own server-side default.)
- Its metadata pins `typing-extensions~=4.3.0`, which directly conflicts with
  `netboxlabs-orb-worker`'s own `~=4.5` pin -- the ranges don't overlap at
  all, so pip can never install both together normally (confirmed: `pip
  install extremecloudiq-api netboxlabs-orb-worker` alone fails with
  `ResolutionImpossible`, nothing from this project involved). That's why
  `extremecloudiq-api` isn't a normal dependency in `pyproject.toml` -- see
  "Develop without the full agent" above for the required `--no-deps` install
  step, and its "Deploying via the real Orb Agent container" note for the
  unresolved risk that entails there.

## The one thing to keep VERIFIED against your installed Diode SDK

`mapper._device_kwargs()` / `_device_custom_fields()` funnel `custom_fields=`
and `tags=` through one place. As of `netboxlabs-diode-sdk` (generated from
NetBox v4.6.0), `custom_fields` values must be wrapped —
`CustomFieldValue(text=...)` — a plain string raises `ValueError`. Confirm
this still holds for your SDK version: `python -c "import netboxlabs.diode.sdk.ingester as i; help(i.Device)"`.

## XIQ auth

- **API token** (recommended): XIQ UI → Global Settings → API Token Management.
- **Username/password**: `POST /login` → JWT; the client auto-refreshes.
- Base URL: `https://api.extremecloudiq.com`.
- The wired-port-list call (`get_wired_portlist`) is the one exception: it lives on
  `https://cloudapi.extremecloudiq.com` (`client.LEGACY_BASE_URL`), an older host not
  covered by the current XIQ OpenAPI spec, but takes the same bearer token.

## Config knobs (policy `config:`)

| key | meaning |
|-----|---------|
| `BOOTSTRAP` | run schema setup before sync (first run only) |
| `default_site` | site for devices with a missing/unresolvable location |
| `name_source` | `hostname` (default) or `serial` |
| `scope.sites` | limit sync to specific resolved sites |

## Wired switch ports

Every device whose `device_function` is a switch (see
`identity.SWITCH_DEVICE_FUNCTIONS` / `identity.is_switch`) gets one `get_wired_portlist` call,
mapped to NetBox `Interface` entities: `name`, link state (`mark_connected`),
`speed` (parsed from `portSpeed`, Kbps), `duplex`, `description` (`ifAlias`), plus
the `xiq_port_id` custom field (created by `BOOTSTRAP`, same as the device-level one).

Link state is asserted as `mark_connected`, not `enabled`. XIQ's port `status`
is link/operational state (is there an active physical link), not
administrative shut/no-shut state -- this endpoint doesn't expose admin state
at all. `enabled` conventionally means administrative state in NetBox, so
asserting it from link state would misrepresent a link-down port as "shut
down by an operator" when XIQ can't actually tell us that. `mark_connected` is
NetBox's field for exactly this instead: "is this interface physically
connected to something" (drives the cabling/topology view without a full
`Cable` object). `enabled` is left unset rather than asserting a fake default.

NetBox's `type` is a best-effort guess from XIQ's negotiated `portSpeed` alone
(e.g. `1000base-t`, `10gbase-x-sfpp`) -- XIQ doesn't expose a capability list
or SFP-vs-copper signal, so this is left unset when the speed is unrecognized
(e.g. `SPEED_AUTO`) rather than guessing further.

`mode` is deliberately **not** asserted. On FLEX-UNI / Fabric-Attach
deployments, a port is mapped straight into an I-SID rather than a VLAN —
`portMode`/`taggedVlans` don't describe real port configuration there, and
this endpoint (and every other documented XIQ API path, as of this writing)
doesn't expose I-SID membership to assert instead. VLAN data (`taggedVlans`)
isn't currently mapped at all, rather than wired up as a real (and
potentially wrong) VLAN link.

## Roadmap

This project deliberately starts with a small, fixed field set. Candidates
for later, once the basics are proven out:

- Optional/configurable fields beyond name/serial/status/site (device_type,
  platform, description, primary IP) — likely via a field-authority-style
  opt-in again, not on by default.
- `xiq_tagged_vlans` / `xiq_lldp_neighbor` port custom fields, and
  consolidating multiple XIQ locations into one NetBox site, if a deployment
  needs either.
- I-SID / Fabric-Attach service mapping, if XIQ ever exposes it via a documented endpoint.
- VLANs / Prefixes from network policies.
- Move bootstrap custom-field creation to Diode if your SDK supports it.
