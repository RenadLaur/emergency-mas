"""Inventory named OSM roads and their derived SUMO edges for manual review."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


BASE = Path(__file__).resolve().parent
OSM = BASE / "generated" / "city_roads.osm.xml"
NETWORK = BASE / "generated" / "astana_city.net.xml"
REPORT = BASE / "generated" / "named_road_inventory.json"

TARGETS = {
    "imanov": {"улица Амангельды Иманова", "улица А. Иманова"},
    "mangilik_el": {"Мәңгілік Ел даңғылы"},
    "sarayshyk": {"Сарайшық көшесі"},
}

OSM_TAGS = (
    "name", "name:ru", "highway", "oneway", "lanes", "lanes:forward",
    "lanes:backward", "turn:lanes", "bridge", "tunnel", "layer", "maxspeed",
)


def osm_inventory() -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {target: [] for target in TARGETS}
    for _, element in ET.iterparse(OSM, events=("end",)):
        if element.tag != "way":
            continue
        tags = {tag.get("k"): tag.get("v") for tag in element.findall("tag")}
        names = {tags.get("name"), tags.get("name:ru")}
        for target, expected_names in TARGETS.items():
            if names & expected_names:
                result[target].append({
                    "osm_way_id": element.get("id"),
                    "tags": {key: tags[key] for key in OSM_TAGS if key in tags},
                    "node_count": len(element.findall("nd")),
                })
        element.clear()
    return result


def network_inventory(wanted_ids: set[str]) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = defaultdict(list)
    for _, element in ET.iterparse(NETWORK, events=("end",)):
        if element.tag != "edge":
            continue
        if element.get("function") == "internal":
            element.clear()
            continue
        origin_ids = {element.get("id", "").split("#")[0]}
        origin_ids.update(
            token
            for param in element.findall("param")
            if param.get("key") == "origId"
            for token in param.get("value", "").split()
        )
        for origin_id in origin_ids & wanted_ids:
            lanes = element.findall("lane")
            result[origin_id].append({
                "sumo_edge_id": element.get("id"),
                "from": element.get("from"),
                "to": element.get("to"),
                "lane_count": len(lanes),
                "speed_mps": float(lanes[0].get("speed")) if lanes else None,
            })
        element.clear()
    return dict(result)


def main() -> None:
    if not OSM.exists() or not NETWORK.exists():
        raise FileNotFoundError("Build the full city network before inventorying named roads")
    osm = osm_inventory()
    ways = {item["osm_way_id"] for values in osm.values() for item in values}
    sumo = network_inventory(ways)
    report = {"purpose": "Inventory only; no geometry is validated or modified.", "targets": {}}
    for target, items in osm.items():
        matched_edges = sum(len(sumo.get(item["osm_way_id"], [])) for item in items)
        report["targets"][target] = {
            "osm_way_count": len(items),
            "sumo_edge_count": matched_edges,
            "ways": [
                {**item, "sumo_edges": sumo.get(item["osm_way_id"], [])}
                for item in items
            ],
        }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        target: {
            "osm_ways": data["osm_way_count"],
            "sumo_edges": data["sumo_edge_count"],
        }
        for target, data in report["targets"].items()
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
