import xml.etree.ElementTree as ET

import pytest

from simulation.maps.astana_city.corridors import (
    CORRIDORS, inventory, select_edges, verify_subset,
)


def test_twenty_distinct_corridors():
    assert len(CORRIDORS) == len(set(CORRIDORS.values())) == 20


def test_keep_all_approaches_but_not_unrelated_roads():
    edges = {
        "main": {"name": "Main", "from": "a", "to": "b"},
        "incoming": {"from": "c", "to": "a"},
        "outgoing": {"from": "b", "to": "d"},
        "far": {"from": "d", "to": "e"},
    }
    assert select_edges(edges, "Main") == ({"main"}, {"main", "incoming", "outgoing"})
    assert select_edges(edges, "Unknown") == (set(), set())


def test_inventory_includes_residential_parts(tmp_path):
    path = tmp_path / "map.xml"
    path.write_text('<osm><way id="42"><tag k="name" v="Роза Бағланова көшесі"/>'
                    '<tag k="highway" v="residential"/></way></osm>')
    assert inventory(path)[CORRIDORS["baglanova"]][0]["id"] == "42"


def test_subset_rejects_lost_movement_and_changed_permissions():
    root = ET.fromstring('<net><edge id="a"><lane index="0" allow="bus"/></edge>'
                         '<edge id="b"><lane index="0"/></edge></net>')
    lanes = {"a": [{"index": "0", "allow": "bus"}], "b": [{"index": "0"}]}
    turns = {("a", "b", "0", "0")}
    with pytest.raises(RuntimeError, match="missing turns"):
        verify_subset(root, lanes, turns, {"a"})
    ET.SubElement(root, "connection", {"from": "a", "to": "b", "fromLane": "0", "toLane": "0"})
    assert verify_subset(root, lanes, turns, {"a"})["checked_movements"] == 1
    root.find("edge/lane").set("allow", "passenger")
    with pytest.raises(RuntimeError, match="lanes="):
        verify_subset(root, lanes, turns, {"a"})
