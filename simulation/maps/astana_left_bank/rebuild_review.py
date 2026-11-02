"""Build an isolated review network and inventory suspected lane discontinuities.

The report flags candidates, not confirmed mapping errors. Raw OSM and the
previously displayed network are never modified. Run with the project environment.
"""
from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import sumolib

BASE = Path(__file__).resolve().parent
OUT = BASE / "generated" / "review"


def audit(network: Path, log_file: Path | None = None,
          scope: str = "Downloaded left-bank snapshot") -> dict:
    net = sumolib.net.readNet(str(network), withInternal=False)
    issues = []
    for edge in net.getEdges():
        if not edge.allows("passenger"):
            continue
        for target, connections in edge.getOutgoing().items():
            straight = [c for c in connections if c.getDirection() == "s"]
            if not straight or not target.allows("passenger"):
                continue
            before = sum(l.allows("passenger") for l in edge.getLanes())
            after = sum(l.allows("passenger") for l in target.getLanes())
            if before <= after:
                continue
            lon, lat = net.convertXY2LonLat(*edge.getToNode().getCoord())
            issues.append({
                "category": "straight_lane_drop", "status": "needs_review",
                "from_edge": edge.getID(), "to_edge": target.getID(),
                "street": edge.getName(), "incoming_passenger_lanes": before,
                "outgoing_passenger_lanes": after, "lon": lon, "lat": lat,
                "note": "May be a legitimate turn pocket or lane reduction; verify before editing.",
            })
    root = ET.parse(network).getroot()
    warnings = [line for line in (log_file or OUT / "netconvert.log").read_text().splitlines()
                if line.startswith("Warning:")]
    categories = Counter(re.sub(r"'[^']*'|\b\d+(?:\.\d+)?\b", "<id>", w)
                         for w in warnings)
    return {
        "scope": scope,
        "accuracy": "Unverified candidate; no claim of city-wide ground-truth accuracy",
        "road_edges": len(net.getEdges()), "suspected_lane_drops": issues,
        "junction_types": dict(Counter(j.get("type") for j in root.findall("junction"))),
        "warnings": warnings, "warning_categories": dict(categories),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    config = ET.parse(BASE / "generated" / "astana_left_bank.netccfg")
    root = config.getroot()
    # Resolve inputs against the original generated directory before moving config.
    for element in root.findall("./input/*"):
        if element.tag.endswith("files"):
            element.set("value", ",".join(str((BASE / "generated" / p).resolve())
                                          for p in element.get("value", "").split(",")))
    options = {
        "output-file": str(OUT / "astana_review.net.xml"),
        "osm.turn-lanes": "true", "osm.lane-access": "true",
        # Retain existing signal grouping until individual roadside heads have
        # been distinguished from standalone pedestrian signals.
        "tls.discard-simple": "true",
        # Avoid inventing auxiliary lanes on guessed ramps.
        "ramps.guess": "false",
        "junctions.corner-detail": "10", "junctions.internal-link-detail": "10",
        "aggregate-warnings": "1000000",
    }
    for name, value in options.items():
        element = root.find(f".//{name}")
        if element is None:
            element = ET.SubElement(root, name)
        element.set("value", value)
    cfg = OUT / "review.netccfg"
    config.write(cfg, encoding="utf-8", xml_declaration=True)
    with (OUT / "netconvert.log").open("w") as log:
        subprocess.run([sumolib.checkBinary("netconvert"), "-c", str(cfg)],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    report = audit(OUT / "astana_review.net.xml")
    (OUT / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    features = [{"type": "Feature", "geometry": {
        "type": "Point", "coordinates": [item["lon"], item["lat"]]},
        "properties": item} for item in report["suspected_lane_drops"]]
    (OUT / "lane_drops.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items()
                      if k not in {"suspected_lane_drops", "warnings", "warning_categories"}},
                     ensure_ascii=False, indent=2))
    print(f"Lane-drop candidates: {len(report['suspected_lane_drops'])}")
    print(f"Conversion warnings: {len(report['warnings'])}")


if __name__ == "__main__":
    main()
