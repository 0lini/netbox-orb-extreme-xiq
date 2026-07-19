"""InferredCluster extract (VirtualChassis source rows)."""

from __future__ import annotations

from orb_extreme_platformone.client import PlatformOneClient

from .tables import CLUSTER_MEMBER_FILTERS


def extract_inferred_clusters(client: PlatformOneClient, asset_device_ids: list[str]) -> list[dict]:
    """Fetch InferredCluster rows for the given AssetDevice UUIDs.

    Filtering `retrieve-inferred-cluster` by AssetDevice UUIDs silently
    returns zero rows: `device_one_id` / `device_two_id` are InferredDevice
    UUIDs. Resolve via `retrieve-inferred-device` (`asset_device_id`), query
    both cluster member filters, then rewrite member IDs back to
    AssetDevice UUIDs so transform can join on `cs_device_id`.
    """
    if not asset_device_ids:
        return []

    inferred_to_asset: dict[str, str] = {}
    for device in client.retrieve("inferred-device", {"asset_device_id": asset_device_ids}):
        inferred_id = str(device.get("id") or "")
        asset_id = str(device.get("asset_device_id") or "")
        if inferred_id and asset_id:
            inferred_to_asset[inferred_id] = asset_id
    if not inferred_to_asset:
        return []

    inferred_ids = sorted(inferred_to_asset)
    by_id: dict[str, dict] = {}
    for filter_field in CLUSTER_MEMBER_FILTERS:
        for cluster in client.retrieve("inferred-cluster", {filter_field: inferred_ids}):
            one = str(cluster.get("device_one_id") or "")
            two = str(cluster.get("device_two_id") or "")
            # Members the map misses are out of scope; transform skips
            # those clusters, so the raw InferredDevice UUID passes through.
            remapped = {
                **cluster,
                "device_one_id": inferred_to_asset.get(one, one),
                "device_two_id": inferred_to_asset.get(two, two),
            }
            by_id[str(remapped.get("id"))] = remapped
    return [by_id[key] for key in sorted(by_id)]
