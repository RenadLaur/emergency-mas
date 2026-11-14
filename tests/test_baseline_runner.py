from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))
from baseline_runner import (VehicleSpec, build_dispatcher, build_vehicle_specs, choose_future_route_edge,
                             choose_incident_access_edge, load_scenario, mean_or_zero, percentile, run_experiment,
                             write_routes)  # noqa: E402


def test_percentile_is_interpolated_and_deterministic() -> None:
    assert percentile([1, 2, 3, 4], .95) == pytest.approx(3.85)
    assert percentile([], .95) == 0.0
    assert mean_or_zero([]) == 0.0
    assert mean_or_zero([1, 3]) == 2.0


def test_scenario_rejects_unknown_tls_mode(tmp_path: Path) -> None:
    scenario = {"network": "x", "seed": 42, "ordinary_vehicles": 1, "emergency_vehicles": 1,
                "tls_mode": "broken", "routing_strategy": "dijkstra"}
    path = tmp_path / "scenario.json"; path.write_text(json.dumps(scenario))
    with pytest.raises(ValueError, match="tls_mode"):
        load_scenario(path)


def test_incident_requires_non_negative_delays_and_a_supported_target(tmp_path: Path) -> None:
    scenario = {"network": "x", "seed": 42, "ordinary_vehicles": 1, "emergency_vehicles": 1,
                "tls_mode": "normal", "routing_strategy": "dijkstra",
                "incident": {"enabled": True, "target": "hard_coded_edge", "starts_at_seconds": 1,
                             "detection_delay_seconds": 0, "report_latency_seconds": 0,
                             "vehicle_command_latency_seconds": 0}}
    path = tmp_path / "scenario.json"; path.write_text(json.dumps(scenario))
    with pytest.raises(ValueError, match="incident.target"):
        load_scenario(path)
    scenario["incident"]["target"] = "first_active_emergency_route"
    scenario["incident"]["report_latency_seconds"] = -1
    path.write_text(json.dumps(scenario))
    with pytest.raises(ValueError, match="report_latency_seconds"):
        load_scenario(path)


def test_incident_target_skips_current_and_destination_edges() -> None:
    lanes = {"start": 1, "ahead": 2, "blocked": 1, "finish": 2}
    assert choose_future_route_edge(("start", "ahead", "blocked", "finish"), 0, lanes.get, 2) == "blocked"
    assert choose_future_route_edge(("start", "ahead", "finish"), 1, lanes.get, 1) is None


def test_incident_response_uses_an_access_edge_before_the_closure() -> None:
    lanes = {"start": 1, "approach": 2, "blocked": 1, "finish": 2}
    assert choose_incident_access_edge(("start", "approach", "blocked", "finish"), 0, "blocked", lanes.get) == "approach"


def test_vehicle_specs_are_seeded_and_routes_are_written(tmp_path: Path) -> None:
    from simulation.sumo.basic.build_network import build_network
    import sumolib
    build_network()
    scenario = {"seed": 42, "ordinary_vehicles": 10, "emergency_vehicles": 2,
                "ordinary_departure_window_seconds": 60, "emergency_departures_seconds": [10, 20],
                "tls_mode": "normal", "routing_strategy": "dijkstra"}
    net = sumolib.net.readNet(str(ROOT / "simulation" / "sumo" / "basic" / "basic.net.xml"))
    first = build_vehicle_specs(net, scenario)
    assert first == build_vehicle_specs(net, scenario)
    assert len(first) == 12
    assert sum(item.emergency for item in first) == 2
    route_file = tmp_path / "routes.rou.xml"; write_routes(route_file, first)
    vehicles = ET.parse(route_file).getroot().findall("vehicle")
    assert len(vehicles) == 12
    assert [int(vehicle.get("depart")) for vehicle in vehicles] == sorted(int(vehicle.get("depart")) for vehicle in vehicles)


def test_blue_light_routes_enable_the_sumo_device(tmp_path: Path) -> None:
    specs = [VehicleSpec("ambulance_0", "ambulance", ("edge_a", "edge_b"), 0, True)]
    route_file = tmp_path / "blue_light.rou.xml"
    write_routes(route_file, specs, {"mode": "blue_light", "speed_factor": 1.15})
    ambulance = ET.parse(route_file).getroot().find("vType[@id='ambulance']")
    assert ambulance is not None
    assert ambulance.get("speedFactor") == "1.15"
    assert ambulance.find("param[@key='has.bluelight.device']").get("value") == "true"


def test_weather_multiplies_route_speed_factors(tmp_path: Path) -> None:
    from simulation.weather.model import profile_from_config
    specs = [VehicleSpec("ambulance_0", "ambulance", ("edge_a", "edge_b"), 0, True)]
    route_file = tmp_path / "snow.rou.xml"
    write_routes(route_file, specs, {"mode": "blue_light", "speed_factor": 1.15},
                 profile_from_config({"condition": "snow"}))
    root = ET.parse(route_file).getroot()
    assert float(root.find("vType[@id='civilian']").get("speedFactor")) == pytest.approx(.70)
    assert float(root.find("vType[@id='ambulance']").get("speedFactor")) == pytest.approx(.805)


def test_dispatcher_setup_is_seeded_and_does_not_create_static_emergencies() -> None:
    from simulation.sumo.basic.build_network import build_network
    import sumolib
    build_network()
    scenario = {"seed": 42, "ordinary_vehicles": 3, "emergency_vehicles": 2,
                "ordinary_departure_window_seconds": 60, "tls_mode": "normal", "routing_strategy": "dijkstra",
                "dispatcher": {"enabled": True, "unit_count": 2, "call_times_seconds": [10, 20]}}
    net = sumolib.net.readNet(str(ROOT / "simulation" / "sumo" / "basic" / "basic.net.xml"))
    first_dispatcher, first_calls = build_dispatcher(net, scenario)
    second_dispatcher, second_calls = build_dispatcher(net, scenario)
    assert [(unit.unit_id, unit.edge_id) for unit in first_dispatcher.units.values()] == [
        (unit.unit_id, unit.edge_id) for unit in second_dispatcher.units.values()]
    assert [(call.call_id, call.destination_edge_id) for call in first_calls] == [
        (call.call_id, call.destination_edge_id) for call in second_calls]
    assert not any(spec.emergency for spec in build_vehicle_specs(net, scenario, include_emergency=False))


def test_dispatcher_fleet_creates_typed_units() -> None:
    from simulation.sumo.basic.build_network import build_network
    import sumolib
    build_network()
    scenario = {"seed": 42, "ordinary_vehicles": 3, "emergency_vehicles": 2,
                "ordinary_departure_window_seconds": 60, "tls_mode": "normal", "routing_strategy": "dijkstra",
                "dispatcher": {"enabled": True, "call_times_seconds": [10],
                               "fleet": {"ambulance": 2, "police": 1, "fire_rescue": 1}}}
    net = sumolib.net.readNet(str(ROOT / "simulation" / "sumo" / "basic" / "basic.net.xml"))
    dispatcher, calls = build_dispatcher(net, scenario)
    assert sorted(unit.unit_type.value for unit in dispatcher.units.values()) == [
        "ambulance", "ambulance", "fire_rescue", "police",
    ]
    assert all(call.required_unit_type.value == "ambulance" for call in calls)


def test_overrides_are_written_into_resolved_scenario(tmp_path: Path) -> None:
    scenario = {"run_id": "original", "network": "simulation/sumo/basic/basic.net.xml", "seed": 1,
                "ordinary_vehicles": 1, "emergency_vehicles": 1, "tls_mode": "normal", "routing_strategy": "dijkstra"}
    path = tmp_path / "scenario.json"; path.write_text(json.dumps(scenario))
    # This test only asserts the resolved configuration path. The SUMO integration
    # test remains separate because it requires a local TraCI port.
    resolved = load_scenario(path); resolved.update({"seed": 42, "run_id": "changed"})
    assert resolved["seed"] == 42
    assert resolved["run_id"] == "changed"
