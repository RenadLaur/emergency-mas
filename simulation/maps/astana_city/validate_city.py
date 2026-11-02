"""Check network structure and drive two cross-city routes in SUMO.

These are import/connectivity checks, not a certification of real-world accuracy.
"""
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import sumolib

BASE = Path(__file__).resolve().parent
OUT = BASE / "generated"
sys.path.insert(0, str(BASE.parent / "astana_left_bank"))
from rebuild_review import audit


def main():
    network = OUT / "astana_city.net.xml"
    report = audit(network, OUT / "netconvert.log", "Astana city bounding rectangle, both banks")
    osm = ET.parse(OUT / "city_roads.osm.xml").getroot()
    missing_lanes = []
    contradictions = []
    for way in osm.findall("way"):
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        if tags.get("highway") not in {"trunk", "primary", "secondary", "tertiary"}:
            continue
        item = {"osm_way_id": way.get("id"), "street": tags.get("name", "")}
        if "lanes" not in tags:
            missing_lanes.append(item)
        values = [tags.get(k, "") for k in ("lanes", "lanes:forward", "lanes:backward")]
        if (all(v.isdigit() for v in values) and tags.get("oneway") not in {"yes", "1", "-1"}
                and "lanes:both_ways" not in tags and int(values[0]) != int(values[1]) + int(values[2])):
            contradictions.append({**item, "total_forward_backward": values})
    report["source_gaps"] = {"major_ways_missing_lanes": missing_lanes,
                              "contradictory_directional_lanes": contradictions}
    net = sumolib.net.readNet(str(network))
    anchors = [(71.4305, 51.1283), (71.405, 51.185)]
    endpoints = []
    for lon, lat in anchors:
        x, y = net.convertLonLat2XY(lon, lat)
        candidates = [(distance, edge.getID(), edge) for edge, distance
                      in net.getNeighboringEdges(x, y, 1500)
                      if edge.allows("passenger") and edge.getType().split("|")[0]
                      in {"highway.primary", "highway.secondary", "highway.tertiary"}]
        if not candidates:
            raise ValueError(f"No road near coverage anchor: {lon}, {lat}")
        endpoints.append(sorted(candidates)[0][2])
    routes = ET.Element("routes")
    ET.SubElement(routes, "vType", {"id": "coverage_car", "vClass": "passenger"})
    route_report = []
    for index, (start, end) in enumerate((endpoints, endpoints[::-1])):
        path, cost = net.getFastestPath(start, end, vClass="passenger")
        if path is None:
            raise ValueError(f"No cross-city route: {start.getID()} -> {end.getID()}")
        route_id = f"coverage_{index}"
        ET.SubElement(routes, "route", {"id": route_id, "edges": " ".join(e.getID() for e in path)})
        ET.SubElement(routes, "vehicle", {"id": route_id, "type": "coverage_car",
                                         "route": route_id, "depart": str(index * 30)})
        route_report.append({"route": route_id, "edges": len(path), "free_flow_cost": cost})
    route_file = OUT / "coverage.rou.xml"
    ET.ElementTree(routes).write(route_file, encoding="utf-8", xml_declaration=True)
    trips = OUT / "coverage.tripinfo.xml"
    with (OUT / "coverage.log").open("w") as log:
        subprocess.run([sumolib.checkBinary("sumo"), "-n", str(network),
                        "-r", str(route_file), "--end", "7200", "--seed", "42",
                        "--time-to-teleport", "-1", "--no-step-log", "true",
                        "--tripinfo-output", str(trips)], stdout=log, stderr=subprocess.STDOUT, check=True)
    completed = ET.parse(trips).getroot().findall("tripinfo")
    if len(completed) != 2 or any(float(t.get("arrival", "-1")) < 0 for t in completed):
        raise ValueError("Cross-city smoke trips did not both arrive")
    report["coverage_smoke"] = {"seed": 42, "routes": route_report,
                                 "completed": len(completed), "teleport_enabled": False}
    (OUT / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"road_edges": report["road_edges"],
                      "lane_drop_candidates": len(report["suspected_lane_drops"]),
                      "junction_types": report["junction_types"],
                      "major_ways_missing_lanes": len(missing_lanes),
                      "contradictory_lane_tags": len(contradictions),
                      "completed_cross_city_trips": len(completed)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
