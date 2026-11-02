import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from simulation.maps.astana_city.review_interchange import EXPECTED, check_lanes


def test_reviewed_overrides_prevent_single_lane_regression():
    path = Path(__file__).resolve().parents[1] / "simulation/maps/astana_left_bank/osm_overrides.json"
    overrides = {o["osm_way_id"]: o for o in json.loads(path.read_text())["way_overrides"]}
    for way, count in EXPECTED.items():
        assert overrides[way]["tags"]["lanes"] == str(count)
    # These short connectors were not widened indiscriminately.
    assert not {"44127578", "44127582", "1381198543", "1381198545"} & overrides.keys()


def test_checker_rejects_lane_regression():
    root = ET.Element("net")
    for way, count in EXPECTED.items():
        edge = ET.SubElement(root, "edge", id=way)
        for lane in range(count):
            ET.SubElement(edge, "lane", index=str(lane))
    assert len(check_lanes(root)) == len(EXPECTED)
    edge = root.find("edge")
    edge.remove(edge.find("lane"))
    with pytest.raises(ValueError, match="Wrong lanes"):
        check_lanes(root)
