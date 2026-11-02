from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
from simulation.sumo.rescue_lane.build_network import build_network


def test_rescue_lane_has_two_lanes_and_bluelight_vehicle() -> None:
    network = build_network()
    import sumolib
    net = sumolib.net.readNet(str(network))
    assert net.getEdge("approach").getLaneNumber() == 2
    route_root = ET.parse(ROOT / "simulation" / "sumo" / "rescue_lane" / "rescue_lane.rou.xml").getroot()
    ambulance = route_root.find("vType[@id='ambulance']")
    assert ambulance is not None
    assert ambulance.get("vClass") == "emergency"
    assert ambulance.find("param[@key='has.bluelight.device']") is not None
