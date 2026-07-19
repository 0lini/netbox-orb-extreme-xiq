"""VirtualChassis mapping from InferredCluster rows."""

from __future__ import annotations

from netboxlabs.diode.sdk.ingester import Entity, VirtualChassis

from orb_extreme_platformone.identity import device_name

from .common import PROVENANCE_TAGS, _cf_text, logger


def _virtual_chassis_name(cluster: dict, device_one_name: str, device_two_name: str) -> str:
    """Stable VirtualChassis name from InferredCluster peer names or member device names.

    Requires two distinct peer names so a shared placeholder like "Default" does
    not collapse every chassis to the same NetBox name. Falls back to distinct
    member device names, then the cluster UUID.
    """
    peers = sorted(
        {name for name in (cluster.get("device_one_peer_name"), cluster.get("device_two_peer_name")) if name}
    )
    if len(peers) >= 2:
        return " / ".join(peers)
    members = sorted({device_one_name, device_two_name})
    if len(members) >= 2:
        return " / ".join(members)
    return f"cluster-{cluster.get('id')}"


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

    Returns (VC entities, {cs_device_id: {"name", "position"}}) for
    `devices_to_entities` to attach `virtual_chassis` / `vc_position`.
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
        # Colliding names are emitted as-is: the unique platformone_cluster_id
        # custom field makes NetBox reject the merge at ingest, surfacing the
        # upstream data problem (e.g. stale Assets hostnames) instead of
        # hiding it behind an invented suffix.
        if chassis_name in used_names:
            logger.warning(
                "Duplicate VirtualChassis name %r (cluster %s); NetBox uniqueness will reject it at ingest",
                chassis_name,
                cluster.get("id"),
            )
        used_names.add(chassis_name)

        vc_kwargs: dict = {
            "name": chassis_name,
            "master": name_one,
            "tags": PROVENANCE_TAGS,
        }
        if cluster.get("id"):
            vc_kwargs["custom_fields"] = {"platformone_cluster_id": _cf_text(str(cluster["id"]))}
        entities.append(Entity(virtual_chassis=VirtualChassis(**vc_kwargs)))

        memberships[one_id] = {"name": chassis_name, "position": 1}
        memberships[two_id] = {"name": chassis_name, "position": 2}

    return entities, memberships
