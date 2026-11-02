"""Offline checks for the reproducible Astana pilot import configuration."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path


AREA_FILE = Path(__file__).resolve().parents[1] / "simulation" / "maps" / "astana_pilot" / "area.json"
SUMO_CONFIG_FILE = AREA_FILE.parent / "astana_pilot.sumocfg"


def test_pilot_bbox_is_west_south_east_north() -> None:
    area = json.loads(AREA_FILE.read_text(encoding="utf-8"))
    west, south, east, north = area["bbox_west_south_east_north"]

    assert west < east
    assert south < north
    assert 71.0 < west < 72.0
    assert 51.0 < south < 52.0


def test_sumo_config_loads_network_and_polygon_layer() -> None:
    root = ET.parse(SUMO_CONFIG_FILE).getroot()

    assert root.find("./input/net-file").attrib["value"] == "generated/astana_pilot.net.xml"
    assert (
        root.find("./input/additional-files").attrib["value"]
        == "generated/astana_pilot.poly.xml"
    )
