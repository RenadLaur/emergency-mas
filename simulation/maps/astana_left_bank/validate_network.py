"""Create a structural validation report for the full left-bank network."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path


MAP_DIR = Path(__file__).resolve().parent
RAW_FILES = [MAP_DIR / "raw" / "nura_city.osm.xml", MAP_DIR / "raw" / "esil_city.osm.xml"]
NETWORK_FILE = MAP_DIR / "generated" / "astana_left_bank.net.xml"
OVERRIDES_FILE = MAP_DIR / "osm_overrides.json"
REPORT_FILE = MAP_DIR / "validation_report.json"
TRAFFIC_LIGHT_REPORT_FILE = MAP_DIR / "traffic_light_inventory.json"
INFRASTRUCTURE_REPORT_FILE = MAP_DIR / "infrastructure_inventory.json"


def raw_osm_inventory() -> dict[str, object]:
    nodes: dict[str, tuple[float, float, dict[str, str]]] = {}
    ways: dict[str, tuple[list[str], dict[str, str]]] = {}
    for raw_file in RAW_FILES:
        root = ET.parse(raw_file).getroot()
        for node in root.findall("node"):
            tags = {tag.attrib["k"]: tag.attrib["v"] for tag in node.findall("tag")}
            nodes[node.attrib["id"]] = (
                float(node.attrib["lat"]),
                float(node.attrib["lon"]),
                tags,
            )
        for way in root.findall("way"):
            tags = {tag.attrib["k"]: tag.attrib["v"] for tag in way.findall("tag")}
            ways[way.attrib["id"]] = ([nd.attrib["ref"] for nd in way.findall("nd")], tags)

    traffic_signals = [
        {"osm_node_id": node_id, "lat": lat, "lon": lon}
        for node_id, (lat, lon, tags) in nodes.items()
        if tags.get("highway") == "traffic_signals"
    ]
    grade_separated_ways = []
    for way_id, (node_refs, tags) in ways.items():
        bridge = tags.get("bridge") not in (None, "no")
        tunnel = tags.get("tunnel") not in (None, "no")
        layer = tags.get("layer")
        if not (bridge or tunnel or layer not in (None, "0")):
            continue
        coordinates = [nodes[node_ref][:2] for node_ref in node_refs if node_ref in nodes]
        center_lat = sum(item[0] for item in coordinates) / len(coordinates) if coordinates else None
        center_lon = sum(item[1] for item in coordinates) / len(coordinates) if coordinates else None
        grade_separated_ways.append(
            {
                "osm_way_id": way_id,
                "name": tags.get("name") or tags.get("name:ru") or tags.get("name:kk"),
                "highway": tags.get("highway"),
                "bridge": tags.get("bridge"),
                "tunnel": tags.get("tunnel"),
                "layer": layer,
                "center_lat": center_lat,
                "center_lon": center_lon,
                "audit_status": "pending",
            }
        )

    inventory = {
        "source_files": [str(path.relative_to(MAP_DIR)) for path in RAW_FILES],
        "deduplication_key": "OSM object type and ID",
        "traffic_signals": sorted(traffic_signals, key=lambda item: int(item["osm_node_id"])),
        "grade_separated_ways": sorted(
            grade_separated_ways, key=lambda item: int(item["osm_way_id"])
        ),
    }
    INFRASTRUCTURE_REPORT_FILE.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    return {
        "bridges": sum(item[1].get("bridge") not in (None, "no") for item in ways.values()),
        "tunnels": sum(item[1].get("tunnel") not in (None, "no") for item in ways.values()),
        "non_zero_layers": sum(item[1].get("layer") not in (None, "0") for item in ways.values()),
        "traffic_signal_nodes": len(traffic_signals),
        "grade_separated_way_inventory_count": len(grade_separated_ways),
    }


def validate() -> dict[str, object]:
    missing = [path for path in [*RAW_FILES, NETWORK_FILE] if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Run import_map.py first; missing: {missing}")

    root = ET.parse(NETWORK_FILE).getroot()
    road_edges = [edge for edge in root.findall("edge") if edge.attrib.get("function") != "internal"]
    override_payload = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    lane_expectations = {
        item["osm_way_id"]: int(item["tags"]["lanes"])
        for item in override_payload["way_overrides"]
        if "lanes" in item["tags"]
    }
    directional_expectations = {
        item["osm_way_id"]: item["tags"]
        for item in override_payload["way_overrides"]
    }
    observed_override_edges: dict[str, list[dict[str, object]]] = defaultdict(list)
    for edge in road_edges:
        original_ids = {edge.attrib["id"].lstrip("-").split("#")[0]}
        for lane in edge.findall("lane"):
            for parameter in lane.findall("param"):
                if parameter.attrib.get("key") == "origId":
                    original_ids.update(parameter.attrib.get("value", "").split())
        for original_id in original_ids & lane_expectations.keys():
            direction = "backward" if edge.attrib["id"].startswith("-") else "forward"
            expected = int(directional_expectations[original_id].get(
                f"lanes:{direction}", lane_expectations[original_id]))
            observed_override_edges[original_id].append(
                {"sumo_edge_id": edge.attrib["id"], "lane_count": len(edge.findall("lane")),
                 "expected_lane_count": expected}
            )

    absorbed_by_junction: set[str] = set()
    for junction in root.findall("junction"):
        for parameter in junction.findall("param"):
            if parameter.attrib.get("key") == "origEdgeIds":
                absorbed_by_junction.update(
                    set(parameter.attrib.get("value", "").split()) & lane_expectations.keys()
                )

    missing_overrides = sorted(
        set(lane_expectations) - set(observed_override_edges) - absorbed_by_junction
    )
    lane_mismatches = [
        {
            "osm_way_id": original_id,
            "expected_lane_count": lane_expectations[original_id],
            "sumo_edges": edges,
        }
        for original_id, edges in observed_override_edges.items()
        if any(edge["lane_count"] != edge["expected_lane_count"] for edge in edges)
    ]
    if missing_overrides or lane_mismatches:
        raise ValueError(
            f"OSM override validation failed: missing={missing_overrides}, mismatches={lane_mismatches}"
        )
    junctions = root.findall("junction")
    tls_junctions = [item for item in junctions if item.attrib.get("type") == "traffic_light"]
    programs = root.findall("tlLogic")
    controlled_connections: dict[str, list[ET.Element]] = defaultdict(list)
    controlled_junctions: dict[str, set[str]] = defaultdict(set)
    for connection in root.findall("connection"):
        controller_id = connection.attrib.get("tl")
        if not controller_id:
            continue
        controlled_connections[controller_id].append(connection)
        via = connection.attrib.get("via", "")
        if via.startswith(":") and via.count("_") >= 2:
            controlled_junctions[controller_id].add(via[1:].rsplit("_", 2)[0])

    program_ids = {program.attrib["id"] for program in programs}
    signal_junction_ids = {junction.attrib["id"] for junction in tls_junctions}
    controlled_signal_junctions = set().union(*controlled_junctions.values())
    inventory = {
        "important_note": "Generated SUMO timings are experimental, not authoritative city timings.",
        "controller_count": len(programs),
        "program_types": dict(Counter(item.attrib.get("type", "unknown") for item in programs)),
        "phase_count": sum(len(item.findall("phase")) for item in programs),
        "controllers_without_connections": sorted(program_ids - set(controlled_connections)),
        "traffic_light_junctions_without_controller": sorted(
            signal_junction_ids - controlled_signal_junctions
        ),
        "controllers": [
            {
                "controller_id": program.attrib["id"],
                "phase_count": len(program.findall("phase")),
                "cycle_duration_seconds": sum(
                    float(phase.attrib["duration"]) for phase in program.findall("phase")
                ),
                "controlled_connection_count": len(
                    controlled_connections.get(program.attrib["id"], [])
                ),
                "controlled_junction_ids": sorted(
                    controlled_junctions.get(program.attrib["id"], set())
                ),
                "requires_manual_timing_audit": True,
            }
            for program in programs
        ],
    }
    TRAFFIC_LIGHT_REPORT_FILE.write_text(json.dumps(inventory, indent=2), encoding="utf-8")

    report = {
        "network_file": str(NETWORK_FILE.relative_to(MAP_DIR)),
        "road_edge_count": len(road_edges),
        "junction_count": len(junctions),
        "connection_count": len(root.findall("connection")),
        "traffic_light_junction_count": len(tls_junctions),
        "traffic_light_program_count": len(programs),
        "traffic_light_phase_count": inventory["phase_count"],
        "traffic_light_controllers_without_connections": len(
            inventory["controllers_without_connections"]
        ),
        "traffic_light_junctions_without_controller": len(
            inventory["traffic_light_junctions_without_controller"]
        ),
        "raw_osm_features": raw_osm_inventory(),
        "osm_override_validation": {
            "expected_way_count": len(lane_expectations),
            "matched_edge_way_count": len(observed_override_edges),
            "absorbed_by_junction_way_count": len(absorbed_by_junction),
            "lane_mismatch_count": len(lane_mismatches),
        },
        "manual_audit_required": [
            "All bridges, tunnels and grade-separated junctions on primary roads.",
            "All ordinary traffic lights on emergency corridors.",
            "Lane counts, legal turns and one-way restrictions on primary roads.",
            "Detailed signal heads and real timing plans are outside automatic OSM import.",
        ],
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(validate(), indent=2))
