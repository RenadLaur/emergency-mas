"""Small routing invariants shared by SUMO adapters and future RL policies."""
from __future__ import annotations


def route_avoids_closed_lanes(route_edges: tuple[str, ...], closed_lane_ids: set[str]) -> bool:
    """Return whether an edge route avoids every currently closed lane's edge."""
    closed_edges = {lane_id.rsplit("_", 1)[0] for lane_id in closed_lane_ids}
    return not bool(set(route_edges) & closed_edges)
