"""Create detailed, bounded inspection networks; never edit the research net.

These cutouts are for visual QA, not experiments: boundary junctions are rebuilt.
Names are explicit snapshot aliases, not a claim about administrative geography.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import sumolib

BASE = Path(__file__).resolve().parent
CORRIDORS = {
    "mangilik": "Мәңгілік Ел даңғылы",
    "turan": "Тұран даңғылы",
    "uly_dala": "Ұлы Дала даңғылы",
    "saraishyk": "Сарайшық көшесі",
    "dostyk": "Достық көшесі",
    "baglanova": "Роза Бағланова көшесі",
    "respublika": "Республика даңғылы",
    "momyshuly": "Бауыржан Момышұлы даңғылы",
    "bogenbay": "Бөгенбай Батыр даңғылы",
    "abay": "Абай даңғылы",
    "zhenis": "Жеңіс даңғылы",
    "abylai": "Абылай Хан даңғылы",
    "alash": "Алаш тас жолы",
    "kudaiberdiuly": "Шәкәрім Құдайбердіұлы даңғылы",
    "syganak": "Сығанақ көшесі",
    "kabanbay": "Қабанбай Батыр даңғылы",
    "tauelsizdik": "Тәуелсіздік даңғылы",
    "kaldayakov": "Шәмші Қалдаяқов көшесі",
    "korgalzhyn": "Қорғалжын тас жолы",
    "kerey": "Керей Және Жәнібек Хандар көшесі",
}


def select_edges(edges, name):
    """Keep the entire named street and all approaches to its junctions."""
    core = {key for key, value in edges.items() if value.get("name") == name}
    nodes = {edges[key][side] for key in core for side in ("from", "to")}
    kept = core | {key for key, value in edges.items()
                   if value.get("from") in nodes or value.get("to") in nodes}
    return core, kept


def inventory(path):
    result = {name: [] for name in CORRIDORS.values()}
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag == "way":
            tags = {tag.get("k"): tag.get("v") for tag in element.findall("tag")}
            if tags.get("name") in result:
                result[tags["name"]].append({"id": element.get("id"), "tags": tags})
            element.clear()
    return result


def lane_signature(edge):
    return [{key: value for key, value in lane.attrib.items()
             if key in ("index", "allow", "disallow", "speed", "width")}
            for lane in edge.findall("lane")]


def movement(connection):
    return tuple(connection.get(key) for key in ("from", "to", "fromLane", "toLane"))


def verify_subset(root, source_lanes, source_movements, core):
    edges = {edge.get("id"): edge for edge in root.findall("edge")
             if edge.get("function") != "internal"}
    if not core <= edges.keys():
        raise RuntimeError(f"Core edges lost: {core - edges.keys()}")
    changed = [key for key, edge in edges.items()
               if lane_signature(edge) != source_lanes[key]]
    actual = {movement(c) for c in root.findall("connection")
              if not c.get("from").startswith(":")}
    expected = {c for c in source_movements if c[0] in edges and c[1] in edges}
    if changed or actual != expected:
        raise RuntimeError(f"Subset altered: lanes={changed}, "
                           f"missing turns={expected - actual}, added turns={actual - expected}")
    return {"lane_attributes_changed": 0, "missing_movements": 0,
            "added_movements": 0, "checked_movements": len(expected)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corridor", choices=[*CORRIDORS, "all"], default="mangilik")
    args = parser.parse_args()
    source = BASE / "generated/astana_city.net.xml"
    edges = {}
    source_lanes = {}
    source_movements = set()
    for _, element in ET.iterparse(source, events=("end",)):
        if element.tag == "edge":
            if element.get("function") != "internal":
                edges[element.get("id")] = dict(element.attrib)
                source_lanes[element.get("id")] = lane_signature(element)
            element.clear()
        elif element.tag == "connection":
            if not element.get("from").startswith(":"):
                source_movements.add(movement(element))
            element.clear()
    ways = inventory(BASE / "generated/city_roads.osm.xml")
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {}
    for slug, name in CORRIDORS.items():
        core, kept = select_edges(edges, name)
        report[slug] = {
            "name": name, "status": "requires_manual_review",
            "core_edges": sorted(core), "context_edge_count": len(kept),
            "ways": ways[name],
            "missing_lanes": [w["id"] for w in ways[name] if "lanes" not in w["tags"]],
            "missing_turn_lanes": [w["id"] for w in ways[name]
                                   if not any(k.startswith("turn:lanes") for k in w["tags"])],
        }
        if args.corridor not in (slug, "all"):
            continue
        if not core:
            raise RuntimeError(f"Street not found: {name}")
        output = BASE / "generated/corridors" / slug
        output.mkdir(parents=True, exist_ok=True)
        selection = output / "edges.txt"
        selection.write_text("\n".join(sorted(kept)) + "\n")
        command = [sumolib.checkBinary("netconvert"), "--sumo-net-file", str(source),
                   "--keep-edges.input-file", str(selection),
                   "--output-file", str(output / "network.net.xml"),
                   "--junctions.corner-detail", "10", "--junctions.internal-link-detail", "10",
                   "--output.original-names", "--output.street-names"]
        with (output / "build.log").open("w") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        tree = ET.parse(output / "network.net.xml")
        actual = {e.get("id"): e for e in tree.getroot().findall("edge")}
        checks = verify_subset(tree.getroot(), source_lanes, source_movements, core)
        internal = sum(e.get("function") == "internal" for e in actual.values())
        if not internal:
            raise RuntimeError("Detailed view must contain internal junction links")
        config = ET.Element("configuration")
        ET.SubElement(ET.SubElement(config, "input"), "net-file", value="network.net.xml")
        ET.SubElement(ET.SubElement(config, "time"), "end", value="1")
        ET.ElementTree(config).write(output / "view.sumocfg", encoding="utf-8", xml_declaration=True)
        manifest = {"source_sha256": digest, "command": command,
                    "core_edges": len(core), "internal_edges": internal,
                    "checks": checks,
                    "purpose": "visual_review_only; boundary junctions rebuilt; not ground truth"}
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(f"{slug}: {len(core)} core, {len(kept)} context, {internal} internal", flush=True)
    (BASE / "generated/corridor_audit.json").write_text(
        json.dumps({"source_sha256": digest, "corridors": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
