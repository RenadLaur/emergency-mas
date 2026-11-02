from pathlib import Path
import xml.etree.ElementTree as ET


def test_main_entrypoint_uses_full_city_not_corridor():
    root = ET.parse(Path(__file__).resolve().parents[1] / "astana.sumocfg").getroot()
    assert root.find("input/net-file").get("value") == "simulation/maps/astana_city/generated/astana_city.net.xml"
    assert root.find("input/route-files").get("value") == "simulation/maps/astana_city/generated/coverage.rou.xml"
    assert root.find("processing/time-to-teleport").get("value") == "-1"
    assert root.find("random_number/seed").get("value") == "42"
    assert root.find("gui_only/start") is None  # Let the user inspect before Play.
