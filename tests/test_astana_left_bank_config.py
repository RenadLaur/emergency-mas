"""Offline tests for the full left-bank import definition."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path


MAP_DIR = (
    Path(__file__).resolve().parents[1] / "simulation" / "maps" / "astana_left_bank"
)


def test_left_bank_uses_explicit_osm_relations() -> None:
    area = json.loads((MAP_DIR / "area.json").read_text(encoding="utf-8"))
    relation_ids = {item["relation_id"] for item in area["osm_relations"]}

    assert relation_ids == {20593940, 3479876}
    assert area["reference_bounds_west_south_east_north"] == [
        71.217973,
        50.857608,
        71.608270,
        51.189307,
    ]


def test_left_bank_sumo_config_references_generated_network() -> None:
    root = ET.parse(MAP_DIR / "astana_left_bank.sumocfg").getroot()

    assert (
        root.find("./input/net-file").attrib["value"]
        == "generated/astana_left_bank.net.xml"
    )


def test_osm_overrides_are_unique_and_reviewed() -> None:
    payload = json.loads((MAP_DIR / "osm_overrides.json").read_text(encoding="utf-8"))
    overrides = payload["way_overrides"]
    way_ids = [item["osm_way_id"] for item in overrides]

    assert len(way_ids) == len(set(way_ids))
    assert all(item["status"] == "visually_verified" for item in overrides)
    assert all(int(item["tags"]["lanes"]) > 0 for item in overrides)
    for item in overrides:
        tags = item["tags"]
        if "lanes:forward" in tags and "lanes:backward" in tags:
            assert int(tags["lanes"]) == int(tags["lanes:forward"]) + int(tags["lanes:backward"])
