"""VirtualChassis mapping from InferredCluster rows."""

from __future__ import annotations

from netboxlabs.diode.sdk.ingester import Entity, VirtualChassis

from orb_extreme_platformone.identity import device_name, resolve_location

from .common import CF_CLUSTER_ID, PROVENANCE_TAGS, _cf_text, _device_ref, logger


def _virtual_chassis_name(cluster: dict, device_one_name: str, device_two_name: str) -> str | None:
    """Stable VirtualChassis name from peer names or member device names.

    Requires two distinct peer names so a shared placeholder like "Default" does
    not collapse every chassis to the same NetBox name. Falls back to distinct
    member device names. No invented ``cluster-{uuid}`` name.
    """
    peers = sorted(
        {name for name in (cluster.get("device_one_peer_name"), cluster.get("device_two_peer_name")) if name}
    )
    if len(peers) >= 2:
        return " / ".join(peers)
    members = sorted({device_one_name, device_two_name})
    if len(members) >= 2:
        return " / ".join(members)
    return None


def _master_ref(record: dict, name: str):
    """Nested Device stub for VirtualChassis.master (Diode-required fields).

    Name-only master stubs fail Diode generate-diff the same way Interface
    nested Devices do (site/role/device_type required). That failure drops the
    whole VirtualChassis entity — including ``platformone_cluster_id`` — and
    subsequent name-only membership refs create orphan duplicate chassis.
    """
    asset = record["asset"]
    site_name, _ = resolve_location(record.get("location"), asset)
    return _device_ref(
        name=name,
        site_name=site_name,
        function=asset.get("function"),
        product_type=asset.get("product_type"),
    )


def virtual_chassis_to_entities(
    clusters: list[dict],
    *,
    records_by_cs_id: dict[str, dict],
) -> tuple[list[Entity], dict[str, dict]]:
    """Map ConfigState InferredCluster rows to VirtualChassis entities + memberships.

    `device_one_id` / `device_two_id` must already be AssetDevice UUIDs
    (backend remaps from InferredDevice IDs). Both members must be present in
    `records_by_cs_id` (already site-scoped); partial clusters are skipped so
    Diode never creates an orphan half-chassis.

    Returns (VC entities, {cs_device_id: {"name", "position", "cluster_id"?}})
    for `devices_to_entities` to attach `virtual_chassis` / `vc_position`.
    device_one is the primary/master per the InferredCluster schema.
    """
    entities: list[Entity] = []
    memberships: dict[str, dict] = {}
    used_names: set[str] = set()

    for cluster in clusters:
        one_id = str(cluster.get("device_one_id") or "")
        two_id = str(cluster.get("device_two_id") or "")
        if not one_id or not two_id:
            continue
        record_one = records_by_cs_id.get(one_id)
        record_two = records_by_cs_id.get(two_id)
        if record_one is None or record_two is None:
            continue

        name_one = device_name(record_one["asset"])
        name_two = device_name(record_two["asset"])
        if not name_one or not name_two:
            logger.warning(
                "Skipping InferredCluster %s: member device(s) have no Assets host_name",
                cluster.get("id"),
            )
            continue
        chassis_name = _virtual_chassis_name(cluster, name_one, name_two)
        if not chassis_name:
            logger.warning(
                "Skipping InferredCluster %s: no distinct peer or member names for VirtualChassis",
                cluster.get("id"),
            )
            continue
        # Colliding human names are emitted as-is: NetBox does not unique
        # VirtualChassis.name (verified 4.6), so identity is the unique
        # platformone_cluster_id custom field. Warn so upstream hostname
        # collisions stay visible in worker logs.
        if chassis_name in used_names:
            logger.warning(
                "Duplicate VirtualChassis name %r (cluster %s); "
                "identity relies on unique platformone_cluster_id",
                chassis_name,
                cluster.get("id"),
            )
        used_names.add(chassis_name)

        cluster_id = str(cluster["id"]) if cluster.get("id") else None
        vc_kwargs: dict = {
            "name": chassis_name,
            "master": _master_ref(record_one, name_one),
            "tags": PROVENANCE_TAGS,
        }
        if cluster_id:
            vc_kwargs["custom_fields"] = {CF_CLUSTER_ID: _cf_text(cluster_id)}
        entities.append(Entity(virtual_chassis=VirtualChassis(**vc_kwargs)))

        membership_one: dict = {"name": chassis_name, "position": 1}
        membership_two: dict = {"name": chassis_name, "position": 2}
        if cluster_id:
            membership_one["cluster_id"] = cluster_id
            membership_two["cluster_id"] = cluster_id
        memberships[one_id] = membership_one
        memberships[two_id] = membership_two

    return entities, memberships
