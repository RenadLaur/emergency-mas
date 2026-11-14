"""Locate level-separated road and parking candidates near Imanov street.

The script is an inventory aid.  A nearby OSM object is a candidate for manual
review, not proof that it is the bridge intended by a user or that its lanes
are correct.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path


BASE = Path(__file__).resolve().parent
OSM = BASE / "generated" / "city_roads.osm.xml"
OUT = BASE / "generated" / "imanov_grade_separation_candidates.json"
IMANOV_NAMES = {"улица Амангельды Иманова", "улица А. Иманова", "Амангелді Иманов көшесі"}
RADIUS_METERS = 220
LOCAL_ROAD_RADIUS_METERS = 140
TAGS = ("name", "name:ru", "highway", "amenity", "bridge", "tunnel", "layer", "lanes", "oneway", "parking")
VEHICLE_HIGHWAYS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service"}


def tags_of(element: ET.Element) -> dict[str, str]:
    return {tag.get("k"): tag.get("v") for tag in element.findall("tag")}


def way_record(element: ET.Element, tags: dict[str, str]) -> dict[str, object]:
    return {
        "osm_way_id": element.get("id"),
        "node_refs": [node.get("ref") for node in element.findall("nd")],
        "tags": {key: tags[key] for key in TAGS if key in tags},
    }


def distance_meters(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Fast local equirectangular distance; sufficient only for candidate search."""
    lat1, lon1 = first
    lat2, lon2 = second
    latitude = math.radians((lat1 + lat2) / 2)
    north = math.radians(lat2 - lat1) * 6_371_000
    east = math.radians(lon2 - lon1) * 6_371_000 * math.cos(latitude)
    return math.hypot(north, east)


def main() -> None:
    if not OSM.exists():
        raise FileNotFoundError(f"Build the city OSM snapshot first: {OSM}")

    imanova = []
    for _, element in ET.iterparse(OSM, events=("end",)):
        if element.tag != "way":
            continue
        tags = tags_of(element)
        if {tags.get("name"), tags.get("name:ru")} & IMANOV_NAMES:
            imanova.append(way_record(element, tags))
        element.clear()
    imanova_nodes = {node for way in imanova for node in way["node_refs"]}
    if not imanova_nodes:
        raise ValueError("No Imanov road segments found in OSM snapshot")

    candidates = []
    for _, element in ET.iterparse(OSM, events=("end",)):
        if element.tag != "way":
            continue
        tags = tags_of(element)
        vehicle_level = tags.get("highway") and (tags.get("bridge") == "yes" or tags.get("tunnel") == "yes")
        parking = tags.get("amenity") == "parking" or tags.get("parking") in {"surface", "underground", "multi-storey"}
        if vehicle_level or parking:
            candidates.append(way_record(element, tags))
        element.clear()

    required_nodes = imanova_nodes | {node for way in candidates for node in way["node_refs"]}
    coordinates: dict[str, tuple[float, float]] = {}
    for _, element in ET.iterparse(OSM, events=("end",)):
        if element.tag == "node" and element.get("id") in required_nodes:
            coordinates[element.get("id")] = (float(element.get("lat")), float(element.get("lon")))
        if element.tag == "node":
            element.clear()

    imanova_points = [coordinates[node] for node in imanova_nodes if node in coordinates]
    nearby = []
    for candidate in candidates:
        points = [coordinates[node] for node in candidate["node_refs"] if node in coordinates]
        if not points:
            continue
        nearest = min(distance_meters(point, reference) for point in points for reference in imanova_points)
        if nearest <= RADIUS_METERS:
            center = (
                sum(point[1] for point in points) / len(points),
                sum(point[0] for point in points) / len(points),
            )
            nearby.append({
                "osm_way_id": candidate["osm_way_id"],
                "nearest_to_imanov_m": round(nearest, 1),
                "center": [round(center[0], 7), round(center[1], 7)],
                "tags": candidate["tags"],
            })

    nearby.sort(key=lambda item: (item["nearest_to_imanov_m"], item["osm_way_id"]))

    # The user reports a lower road.  Search the immediate neighbourhood of the
    # two mapped bridge carriageways, but keep these as candidates until they
    # are manually bound to a level profile.
    bridge_ids = {"1537667048", "846023745"}
    bridge_nodes = {
        node for way in candidates if way["osm_way_id"] in bridge_ids
        for node in way["node_refs"] if node in coordinates
    }
    bridge_points = [coordinates[node] for node in bridge_nodes]
    if not bridge_points:
        raise ValueError("Expected Belasar bridge carriageways were not resolved")
    latitude_margin = LOCAL_ROAD_RADIUS_METERS / 111_000
    longitude_margin = LOCAL_ROAD_RADIUS_METERS / (111_000 * math.cos(math.radians(51.1584)))
    min_lat = min(point[0] for point in bridge_points) - latitude_margin
    max_lat = max(point[0] for point in bridge_points) + latitude_margin
    min_lon = min(point[1] for point in bridge_points) - longitude_margin
    max_lon = max(point[1] for point in bridge_points) + longitude_margin
    local_nodes: dict[str, tuple[float, float]] = {}
    for _, element in ET.iterparse(OSM, events=("end",)):
        if element.tag == "node":
            lat, lon = float(element.get("lat")), float(element.get("lon"))
            if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                local_nodes[element.get("id")] = (lat, lon)
            element.clear()
    nearby_roads = []
    for _, element in ET.iterparse(OSM, events=("end",)):
        if element.tag != "way":
            continue
        tags = tags_of(element)
        refs = [node.get("ref") for node in element.findall("nd")]
        if tags.get("highway") in VEHICLE_HIGHWAYS and element.get("id") not in bridge_ids:
            points = [local_nodes[node] for node in refs if node in local_nodes]
            if points:
                nearest = min(distance_meters(point, reference) for point in points for reference in bridge_points)
                if nearest <= LOCAL_ROAD_RADIUS_METERS:
                    nearby_roads.append({
                        "osm_way_id": element.get("id"),
                        "nearest_to_bridge_m": round(nearest, 1),
                        "tags": {key: tags[key] for key in TAGS if key in tags},
                    })
        element.clear()
    nearby_roads.sort(key=lambda item: (item["nearest_to_bridge_m"], item["osm_way_id"]))
    report = {
        "purpose": "Candidate search only; no automatic lane or height correction.",
        "imanov_way_count": len(imanova),
        "search_radius_m": RADIUS_METERS,
        "candidates": nearby,
        "local_vehicle_road_radius_m": LOCAL_ROAD_RADIUS_METERS,
        "nearby_vehicle_road_candidates": nearby_roads,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "imanov_ways": len(imanova),
        "nearby_candidates": len(nearby),
        "nearby_vehicle_roads": len(nearby_roads),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
