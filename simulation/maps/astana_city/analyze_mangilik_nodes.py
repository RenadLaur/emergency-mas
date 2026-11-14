"""Inventory candidate experiment nodes along Mangilik El from the SUMO cutout.

The report describes the current model only.  It is not evidence of real
traffic volume, real signal timing, or surveyed lane geometry.
"""
from __future__ import annotations

import json
from pathlib import Path

import sumolib


BASE = Path(__file__).resolve().parent
NETWORK = BASE / "generated" / "corridors" / "mangilik" / "network.net.xml"
OUTPUT = BASE / "generated" / "mangilik_candidate_nodes.json"
MANGILIK_NAMES = {"Мәңгілік Ел даңғылы", "проспект Мангилик Ел", "Мәңгілік Ел"}


def _edge_payload(edge: object, direction: str) -> dict[str, object]:
    return {
        "edge_id": edge.getID(),
        "direction": direction,
        "road_name": edge.getName(),
        "lane_count": edge.getLaneNumber(),
        "speed_mps": edge.getSpeed(),
    }


def build_report() -> dict[str, object]:
    net = sumolib.net.readNet(str(NETWORK), withInternal=False, withPrograms=True)
    candidates: list[dict[str, object]] = []
    for node in net.getNodes():
        incoming = [edge for edge in node.getIncoming() if edge.getFunction() == ""]
        outgoing = [edge for edge in node.getOutgoing() if edge.getFunction() == ""]
        incident = incoming + outgoing
        core = [edge for edge in incident if edge.getName() in MANGILIK_NAMES]
        if not core:
            continue
        named_roads = sorted({edge.getName() for edge in incident if edge.getName()})
        tls_id = node.getTLSID()
        if len(named_roads) < 2 and not tls_id:
            continue
        x, y = node.getCoord()
        lon, lat = net.convertXY2LonLat(x, y)
        connections = []
        for connection in node.getConnections():
            from_edge = connection.getFrom()
            to_edge = connection.getTo()
            connections.append({
                "from_edge": from_edge.getID(),
                "from_road": from_edge.getName(),
                "from_lane": connection.getFromLane().getIndex(),
                "to_edge": to_edge.getID(),
                "to_road": to_edge.getName(),
                "to_lane": connection.getToLane().getIndex(),
                "direction": connection.getDirection(),
                "tls_id": connection.getTLSID() or None,
                "tls_link_index": connection.getTLLinkIndex(),
            })
        candidates.append({
            "junction_id": node.getID(),
            "junction_type": node.getType(),
            "tls_id": tls_id,
            "longitude": round(lon, 7),
            "latitude": round(lat, 7),
            "named_roads": named_roads,
            "mangilik_incoming_lanes": sum(edge.getLaneNumber() for edge in incoming
                                            if edge.getName() in MANGILIK_NAMES),
            "mangilik_outgoing_lanes": sum(edge.getLaneNumber() for edge in outgoing
                                            if edge.getName() in MANGILIK_NAMES),
            "edges": sorted(
                [_edge_payload(edge, "incoming") for edge in incoming]
                + [_edge_payload(edge, "outgoing") for edge in outgoing],
                key=lambda row: (row["direction"], row["road_name"], row["edge_id"]),
            ),
            "connections": sorted(
                connections,
                key=lambda row: (row["from_edge"], row["from_lane"], row["to_edge"], row["to_lane"]),
            ),
        })
    candidates.sort(key=lambda row: (row["longitude"], row["latitude"], row["junction_id"]))
    report: dict[str, object] = {
        "purpose": "candidate inventory from current SUMO model; not real congestion evidence",
        "network": str(NETWORK.relative_to(BASE)),
        "candidate_count": len(candidates),
        "traffic_light_candidate_count": sum(bool(row["tls_id"]) for row in candidates),
        "candidates": candidates,
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    report = build_report()
    print(json.dumps({key: report[key] for key in ("candidate_count", "traffic_light_candidate_count")},
                     ensure_ascii=False, indent=2))
