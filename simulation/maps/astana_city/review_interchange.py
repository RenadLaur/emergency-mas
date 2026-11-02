"""Build and validate the Mangilik El / Saraishyk review cutout offline."""
from __future__ import annotations

import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import sumolib

BASE = Path(__file__).resolve().parent
EXPECTED = {
    "165816979": 3, "735041697": 3, "735041698": 3, "688297715": 3,
    "24672587": 2, "165975916": 2, "37883864": 2, "24672593": 2,
    "24672599": 2, "37883874": 2, "37883866": 2, "1381198546": 2,
    "24672609": 2, "1381198544": 2, "1186145848": 4,
}


def check_lanes(root):
    matched = {key: [] for key in EXPECTED}
    for edge in root.findall("edge"):
        if edge.get("function") == "internal":
            continue
        ids = {edge.get("id").split("#")[0]}
        for param in edge.findall(".//param[@key='origId']"):
            ids.update(param.get("value").split())
        for way in ids & EXPECTED.keys():
            count = len(edge.findall("lane"))
            if count != EXPECTED[way]:
                raise ValueError(f"Wrong lanes: {way}/{edge.get('id')}: {count}")
            matched[way].append(edge.get("id"))
    if any(not values for values in matched.values()):
        raise ValueError(f"Missing reviewed ways: {matched}")
    return matched


def main():
    output = BASE / "generated/interchange"
    output.mkdir(parents=True, exist_ok=True)
    source = BASE / "generated/astana_city.net.xml"
    command = [sumolib.checkBinary("netconvert"), "--sumo-net-file", str(source),
               "--keep-edges.in-geo-boundary", "71.434,51.128,71.446,51.138",
               "--output-file", str(output / "network.net.xml"),
               "--junctions.corner-detail", "10", "--junctions.internal-link-detail", "10",
               "--output.original-names", "--output.street-names"]
    with (output / "build.log").open("w") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    root = ET.parse(output / "network.net.xml").getroot()
    matched = check_lanes(root)
    # The elevated straight bridge must not share junctions with ground-level
    # main carriageways; ramps legitimately connect levels away from crossings.
    def nodes(prefixes):
        return {e.get(side) for e in root.findall("edge")
                if e.get("id").split("#")[0] in prefixes for side in ("from", "to")}
    overlap = nodes({"37883868", "37883870"}) & nodes({"165816979", "735041697"})
    if overlap:
        raise ValueError(f"Bridge falsely connected to underpass: {overlap}")
    net = sumolib.net.readNet(str(output / "network.net.xml"))
    routes = ET.Element("routes")
    ET.SubElement(routes, "vType", id="check_car", vClass="passenger")
    trips = []
    # Exercise every lane at each corrected way's entry, including loops.
    for way, edge_ids in matched.items():
        ordered = sorted(edge_ids, key=lambda value: int(value.split("#")[1]) if "#" in value else 0)
        for lane in range(EXPECTED[way]):
            trips.append((f"way_{way}_{lane}", ordered[0], ordered[-1], lane))
    # Both main directions, bridge directions, and continuous turning paths.
    for index, (start, end) in enumerate([
        ("165816979#0", "735041698#2"), ("688297715#0", "735041697#5"),
        ("1186145848", "37883868#3"), ("37883870#0", "1186145847"),
        ("1381198544", "735041698#2"), ("1381198546", "735041697#5"),
        ("24672593", "735041697#5"), ("688297715#0", "24672599"),
    ]):
        trips.append((f"through_{index}", start, end, 0))
    for index, (name, start, end, lane) in enumerate(trips):
        path, cost = net.getFastestPath(net.getEdge(start), net.getEdge(end), vClass="passenger")
        if path is None:
            raise ValueError(f"Unreachable: {name}")
        vehicle = ET.SubElement(routes, "vehicle", id=name, type="check_car",
                                depart=str(index * 8), departLane=str(lane))
        ET.SubElement(vehicle, "route", edges=" ".join(e.getID() for e in path))
    ET.ElementTree(routes).write(output / "check.rou.xml", encoding="utf-8", xml_declaration=True)
    with (output / "simulation.log").open("w") as log:
        subprocess.run([sumolib.checkBinary("sumo"), "-n", str(output / "network.net.xml"),
                        "-r", str(output / "check.rou.xml"), "--end", "1800", "--seed", "42",
                        "--time-to-teleport", "-1", "--collision.action", "warn",
                        "--no-step-log", "true", "--tripinfo-output", str(output / "trips.xml")],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    completed = ET.parse(output / "trips.xml").getroot().findall("tripinfo")
    if {t.get("id") for t in completed} != {t[0] for t in trips}:
        raise ValueError("Not all lane/route probes completed")
    log_text = (output / "simulation.log").read_text()
    if "collision" in log_text.lower() or "teleport" in log_text.lower():
        raise ValueError("Unexpected collision/teleport warning; inspect simulation.log")
    config = ET.Element("configuration")
    inputs = ET.SubElement(config, "input")
    ET.SubElement(inputs, "net-file", value="network.net.xml")
    ET.SubElement(inputs, "route-files", value="check.rou.xml")
    ET.SubElement(ET.SubElement(config, "time"), "end", value="1800")
    ET.SubElement(ET.SubElement(config, "processing"), "time-to-teleport", value="-1")
    ET.SubElement(ET.SubElement(config, "random_number"), "seed", value="42")
    ET.SubElement(ET.SubElement(config, "gui_only"), "delay", value="100")
    ET.ElementTree(config).write(output / "demo.sumocfg", encoding="utf-8", xml_declaration=True)
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"source_sha256": digest, "matched_edges": matched,
              "expected_lanes": EXPECTED, "completed": len(completed),
              "bridge_ground_shared_nodes": sorted(overlap), "command": command,
              "limitations": "Visual lane-count correction, not surveyed geometry or calibrated traffic."}
    (output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Validated {len(matched)} ways, {len(completed)} completed probes; {output / 'demo.sumocfg'}")


if __name__ == "__main__":
    main()
