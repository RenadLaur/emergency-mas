"""Cache OSM API tiles, merge road objects, apply reviewed tags and build SUMO.

The rectangle contains both banks. This is an import, not ground-truth validation.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, UTC
from pathlib import Path

import sumo
import sumolib

BASE = Path(__file__).resolve().parent
RAW = BASE / "raw"
OUT = BASE / "generated"
LEFT = BASE.parent / "astana_left_bank"


def fetch_tile(bounds: tuple, depth: int = 0) -> list[Path]:
    key = ",".join(f"{x:.7f}" for x in bounds)
    path = RAW / f"tile_{key}.osm.xml"
    if path.exists():
        ET.parse(path)
        return [path]
    marker = path.with_suffix(".split")
    split = marker.exists()
    if not split:
        request = urllib.request.Request(
            f"https://api.openstreetmap.org/api/0.6/map?bbox={key}",
            headers={"User-Agent": "emergency-mas-research/0.1"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = response.read()
                root = ET.fromstring(data)
                if root.tag != "osm":
                    raise ValueError(f"Non-OSM response: {key}")
                temporary = path.with_suffix(".part")
                temporary.write_bytes(data)
                temporary.replace(path)
                print(f"Downloaded {key}: {len(data)} bytes", flush=True)
                return [path]
            except urllib.error.HTTPError as error:
                if error.code == 400 and depth < 5:
                    # The API limits objects per bounding-box request.
                    split = True
                    marker.touch()
                    break
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
    if not split:
        raise RuntimeError(f"Download failed: {key}")
    west, south, east, north = bounds
    mx, my = (west + east) / 2, (south + north) / 2
    paths = []
    for child in ((west, south, mx, my), (mx, south, east, my),
                  (west, my, mx, north), (mx, my, east, north)):
        paths.extend(fetch_tile(child, depth + 1))
    return paths


def merge_roads(paths: list[Path]) -> tuple[ET.Element, dict]:
    objects: dict[tuple[str, str], ET.Element] = {}
    for path in paths:
        root = ET.parse(path).getroot()
        ways = [w for w in root.findall("way")
                if any(t.get("k") == "highway" for t in w.findall("tag"))]
        refs = {n.get("ref") for w in ways for n in w.findall("nd")}
        selected = [n for n in root.findall("node") if n.get("id") in refs] + ways
        selected += [r for r in root.findall("relation")
                     if any(t.get("k") == "type" and t.get("v") == "restriction"
                            for t in r.findall("tag"))]
        for item in selected:
            key = (item.tag, item.attrib["id"])
            old = objects.get(key)
            if old is None or int(item.get("version", "0")) > int(old.get("version", "0")):
                objects[key] = item
    result = ET.Element("osm", {"version": "0.6", "generator": "emergency-mas"})
    incomplete = []
    order = {"node": 0, "way": 1, "relation": 2}
    for key, item in sorted(objects.items(), key=lambda pair: (order[pair[0][0]], int(pair[0][1]))):
        if item.tag == "way":
            missing = [n.get("ref") for n in item.findall("nd")
                       if ("node", n.get("ref")) not in objects]
            if missing:
                raise ValueError(f"Incomplete road {key}: {missing}")
        if item.tag == "relation" and any(
                (m.get("type"), m.get("ref")) not in objects for m in item.findall("member")):
            incomplete.append(item.attrib["id"])
            continue
        result.append(item)
    return result, {"road_ways": len(result.findall("way")),
                    "nodes": len(result.findall("node")),
                    "restrictions": len(result.findall("relation")),
                    "incomplete_restriction_ids": incomplete}


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    area = json.loads((BASE / "area.json").read_text())
    west, south, east, north = area["bounds"]
    paths = []
    for y in range(8):
        for x in range(8):
            paths.extend(fetch_tile((west + (east-west)*x/8, south + (north-south)*y/8,
                                     west + (east-west)*(x+1)/8, south + (north-south)*(y+1)/8)))
    root, inventory = merge_roads(paths)
    overrides = json.loads((LEFT / "osm_overrides.json").read_text())["way_overrides"]
    bridge_groups = json.loads((BASE / "bridge_lane_overrides.json").read_text())["groups"]
    overrides += [dict(osm_way_id=way, tags=group["tags"])
                  for group in bridge_groups for way in group["ways"]]
    ways = {w.attrib["id"]: w for w in root.findall("way")}
    for override in overrides:
        way = ways[override["osm_way_id"]]
        tags = {t.attrib["k"]: t for t in way.findall("tag")}
        for key, value in override["tags"].items():
            if value is None:
                if key in tags:
                    way.remove(tags[key])
            elif key in tags:
                tags[key].set("v", value)
            else:
                ET.SubElement(way, "tag", {"k": key, "v": value})
    merged = OUT / "city_roads.osm.xml"
    ET.ElementTree(root).write(merged, encoding="utf-8", xml_declaration=True)
    package = Path(sumo.__file__).resolve().parent
    command = [sumolib.checkBinary("netconvert"), "--osm-files", str(merged),
               # Preserve the existing project's 50 km/h urban fallback for
               # missing maxspeed, rather than silently switching to rural speeds.
               "--type-files", ",".join(str(package / "data/typemap" / name) for name in
                   ("osmNetconvert.typ.xml", "osmNetconvertUrbanDe.typ.xml")),
               "--output-file", str(OUT / "astana_city.pending.net.xml"),
               "--geometry.remove", "--junctions.join", "--roundabouts.guess",
               "--tls.guess-signals", "--tls.discard-simple", "--tls.join",
               "--osm.turn-lanes", "--osm.lane-access",
               "--junctions.corner-detail", "10", "--junctions.internal-link-detail", "10",
               "--output.original-names", "--output.street-names",
               "--remove-edges.by-vclass", "tram,rail_urban,subway,cable_car,rail_electric,bicycle,pedestrian"]
    with (OUT / "netconvert.log").open("w") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    # Readers must never see a partially written XML during a rebuild.
    (OUT / "astana_city.pending.net.xml").replace(OUT / "astana_city.net.xml")
    manifest = {"built_at": datetime.now(UTC).isoformat(), "area": area, **inventory,
                "overrides": len(overrides), "command": command,
                "tiles": [{"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in paths], "status": "imported_unverified"}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(inventory, indent=2), flush=True)


if __name__ == "__main__":
    main()
