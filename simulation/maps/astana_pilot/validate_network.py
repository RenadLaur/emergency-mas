"""Create an auditable structural report for the Astana pilot SUMO network."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path


MAP_DIR = Path(__file__).resolve().parent
RAW_FILE = MAP_DIR / "raw" / "astana_pilot_bbox.osm.xml"
NETWORK_FILE = MAP_DIR / "generated" / "astana_pilot.net.xml"
REPORT_FILE = MAP_DIR / "validation_report.json"
TRAFFIC_LIGHT_REPORT_FILE = MAP_DIR / "traffic_light_inventory.json"


def osm_way_tag_counts() -> dict[str, int]:
    root = ET.parse(RAW_FILE).getroot()
    counts = {"bridges": 0, "tunnels": 0, "non_zero_layers": 0, "traffic_signals": 0}
    for way in root.findall("way"):
        tags = {tag.attrib["k"]: tag.attrib["v"] for tag in way.findall("tag")}
        counts["bridges"] += tags.get("bridge") not in (None, "no")
        counts["tunnels"] += tags.get("tunnel") not in (None, "no")
        counts["non_zero_layers"] += tags.get("layer") not in (None, "0")
    for node in root.findall("node"):
        tags = {tag.attrib["k"]: tag.attrib["v"] for tag in node.findall("tag")}
        counts["traffic_signals"] += tags.get("highway") == "traffic_signals"
    return counts


def validate() -> dict[str, object]:
    if not RAW_FILE.exists() or not NETWORK_FILE.exists():
        raise FileNotFoundError("Run import_map.py before validating the pilot network")

    root = ET.parse(NETWORK_FILE).getroot()
    road_edges = [edge for edge in root.findall("edge") if edge.attrib.get("function") != "internal"]
    junctions = root.findall("junction")
    tls_junctions = [node for node in junctions if node.attrib.get("type") == "traffic_light"]
    tls_programs = root.findall("tlLogic")
    controlled_connections: dict[str, list[ET.Element]] = defaultdict(list)
    controlled_junctions: dict[str, set[str]] = defaultdict(set)
    for connection in root.findall("connection"):
        controller_id = connection.attrib.get("tl")
        if controller_id:
            controlled_connections[controller_id].append(connection)
            via = connection.attrib.get("via", "")
            if via.startswith(":") and via.count("_") >= 2:
                controlled_junctions[controller_id].add(via[1:].rsplit("_", 2)[0])

    junction_by_id = {junction.attrib["id"]: junction for junction in junctions}
    program_ids = {program.attrib["id"] for program in tls_programs}
    traffic_light_junction_ids = {junction.attrib["id"] for junction in tls_junctions}
    signal_inventory = []
    for program in tls_programs:
        controller_id = program.attrib["id"]
        phases = program.findall("phase")
        connections = controlled_connections.get(controller_id, [])
        controller_junction_ids = sorted(controlled_junctions.get(controller_id, set()))
        controller_junctions = [
            junction_by_id[junction_id]
            for junction_id in controller_junction_ids
            if junction_id in junction_by_id
        ]
        if controller_junctions:
            junction_x = sum(float(item.attrib["x"]) for item in controller_junctions) / len(
                controller_junctions
            )
            junction_y = sum(float(item.attrib["y"]) for item in controller_junctions) / len(
                controller_junctions
            )
        else:
            junction_x = None
            junction_y = None
        signal_inventory.append(
            {
                "controller_id": controller_id,
                "program_id": program.attrib.get("programID", "0"),
                "program_type": program.attrib.get("type", "unknown"),
                "phase_count": len(phases),
                "cycle_duration_seconds": sum(float(phase.attrib["duration"]) for phase in phases),
                "controlled_connection_count": len(connections),
                "controlled_link_count": len(
                    {connection.attrib.get("linkIndex") for connection in connections}
                ),
                "controlled_junction_ids": controller_junction_ids,
                "junction_x": junction_x,
                "junction_y": junction_y,
                "requires_manual_timing_audit": True,
            }
        )

    traffic_light_report = {
        "source": str(NETWORK_FILE.relative_to(MAP_DIR)),
        "important_note": (
            "SUMO phase plans were generated from OSM topology and are not authoritative "
            "real-world Astana timings."
        ),
        "controller_count": len(tls_programs),
        "program_types": dict(Counter(program.attrib.get("type", "unknown") for program in tls_programs)),
        "phase_count": sum(len(program.findall("phase")) for program in tls_programs),
        "controllers_without_connections": sorted(
            program_ids - set(controlled_connections)
        ),
        "traffic_light_junctions_without_controller": sorted(
            traffic_light_junction_ids
            - set().union(*controlled_junctions.values())
        ),
        "controllers": signal_inventory,
    }
    TRAFFIC_LIGHT_REPORT_FILE.write_text(
        json.dumps(traffic_light_report, indent=2), encoding="utf-8"
    )
    report: dict[str, object] = {
        "network_file": str(NETWORK_FILE.relative_to(MAP_DIR)),
        "road_edge_count": len(road_edges),
        "junction_count": len(junctions),
        "traffic_light_junction_count": len(tls_junctions),
        "traffic_light_program_count": len(tls_programs),
        "traffic_light_phase_count": traffic_light_report["phase_count"],
        "traffic_light_controllers_without_connections": len(
            traffic_light_report["controllers_without_connections"]
        ),
        "traffic_light_junctions_without_controller": len(
            traffic_light_report["traffic_light_junctions_without_controller"]
        ),
        "connection_count": len(root.findall("connection")),
        "raw_osm_features": osm_way_tag_counts(),
        "manual_audit_required": [
            "Check each emergency corridor for false joins between bridges, tunnels and roads at different layers.",
            "Inspect every traffic light on emergency corridors; OSM does not provide authoritative timing plans.",
            "Match imported controllers to physical signal heads, stop lines and pedestrian crossings.",
            "Check turn restrictions, one-way streets, lane counts and speed limits at audited junctions.",
        ],
    }
    if not road_edges:
        raise ValueError("Converted SUMO network has no road edges")
    REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(validate(), indent=2))
