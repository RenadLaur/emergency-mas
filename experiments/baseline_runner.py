"""Run deterministic SUMO baselines and persist experiment artefacts.

It supports static emergency routes and a minimal nearest-free dispatcher with
automatic reassignment. Weather, collision modelling and RL remain outside it.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import statistics
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sumolib
import traci


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.dispatcher.dispatcher import (CallStatus, EmergencyCall, EmergencyUnit,
                                          NearestFreeDispatcher, UnitType)
from agents.driver.civilian_yielding import CivilianYieldingModel, profiles_for
from agents.driver.emergency_state import EmergencyDriverModel
from agents.communication.message_bus import CommunicationBus
from agents.incident.incidents import IncidentManager, RoadClosureIncident
from agents.incident.safety import SafetyMonitor, near_miss_payload
from agents.incident.triage import IncidentAssessment, ResponsePlanner
from agents.routing.routing import route_avoids_closed_lanes
from simulation.weather.model import WeatherProfile, profile_from_config
VALID_TLS_MODES = {"normal", "off"}
VALID_ROUTING_STRATEGIES = {"dijkstra", "traffic_aware"}
VALID_EMERGENCY_TACTICS = {"standard", "blue_light"}
INCIDENT_ROUTE_TARGET = "first_active_emergency_route"
RESPONSE_UNIT_FIELDS: tuple[tuple[UnitType, str], ...] = (
    (UnitType.POLICE, "police_units"),
    (UnitType.AMBULANCE, "ambulance_units"),
    (UnitType.FIRE_RESCUE, "fire_rescue_units"),
)


@dataclass(frozen=True)
class VehicleSpec:
    vehicle_id: str
    vehicle_type: str
    edges: tuple[str, ...]
    depart: int
    emergency: bool


def validate_scenario(scenario: dict[str, Any]) -> None:
    required = {"network", "seed", "ordinary_vehicles", "emergency_vehicles", "tls_mode", "routing_strategy"}
    missing = required - scenario.keys()
    if missing:
        raise ValueError(f"Scenario has no fields: {sorted(missing)}")
    if scenario["tls_mode"] not in VALID_TLS_MODES:
        raise ValueError(f"Unknown tls_mode: {scenario['tls_mode']}")
    if scenario["routing_strategy"] not in VALID_ROUTING_STRATEGIES:
        raise ValueError(f"Unknown routing_strategy: {scenario['routing_strategy']}")
    if int(scenario["ordinary_vehicles"]) < 1 or int(scenario["emergency_vehicles"]) < 1:
        raise ValueError("Scenario needs at least one ordinary and emergency vehicle")
    if "experiment_end_seconds" in scenario and float(scenario["experiment_end_seconds"]) <= 0:
        raise ValueError("experiment_end_seconds must be positive")
    weather = scenario.get("weather", {"condition": "dry"})
    if not isinstance(weather, dict):
        raise ValueError("weather must be an object")
    profile_from_config(weather)
    emergency_driver = scenario.get("emergency_driver", {"enabled": False})
    if not isinstance(emergency_driver, dict) or not isinstance(emergency_driver.get("enabled", False), bool):
        raise ValueError("emergency_driver must be an object with boolean enabled")
    safety = scenario.get("safety", {"enabled": False})
    if not isinstance(safety, dict) or not isinstance(safety.get("enabled", False), bool):
        raise ValueError("safety must be an object with boolean enabled")
    if safety.get("enabled"):
        if float(safety.get("ttc_threshold_seconds", 2.0)) <= 0:
            raise ValueError("safety.ttc_threshold_seconds must be positive")
        if float(safety.get("observation_period_seconds", 1.0)) <= 0:
            raise ValueError("safety.observation_period_seconds must be positive")
    yielding = scenario.get("civilian_yielding", {"enabled": False})
    if not isinstance(yielding, dict) or not isinstance(yielding.get("enabled", False), bool):
        raise ValueError("civilian_yielding must be an object with boolean enabled")
    if yielding.get("enabled") and float(yielding.get("detection_range_meters", 0)) <= 0:
        raise ValueError("civilian_yielding requires positive detection_range_meters")
    dispatcher = scenario.get("dispatcher", {"enabled": False})
    if not isinstance(dispatcher, dict) or not isinstance(dispatcher.get("enabled", False), bool):
        raise ValueError("dispatcher must be an object with boolean enabled")
    if dispatcher.get("enabled"):
        call_times = dispatcher.get("call_times_seconds")
        if not isinstance(call_times, list) or not call_times:
            raise ValueError("dispatcher requires a non-empty call_times_seconds list")
        fleet = dispatcher.get("fleet")
        if fleet is not None and not isinstance(fleet, dict):
            raise ValueError("dispatcher.fleet must be an object")
        if fleet is not None:
            unknown_types = set(fleet) - {unit_type.value for unit_type in UnitType}
            if unknown_types:
                raise ValueError(f"dispatcher.fleet has unknown unit types: {sorted(unknown_types)}")
            for unit_type, count in fleet.items():
                if not isinstance(count, int) or count < 0:
                    raise ValueError(f"dispatcher.fleet.{unit_type} must be a non-negative integer")
            if sum(fleet.values()) < 1:
                raise ValueError("dispatcher.fleet requires at least one unit")
        if fleet is None and int(dispatcher.get("unit_count", scenario["emergency_vehicles"])) < 1:
            raise ValueError("dispatcher requires at least one unit")
    tactics = scenario.get("emergency_tactics", {"mode": "standard"})
    if not isinstance(tactics, dict) or tactics.get("mode", "standard") not in VALID_EMERGENCY_TACTICS:
        raise ValueError(f"emergency_tactics.mode must be one of {sorted(VALID_EMERGENCY_TACTICS)}")
    if tactics.get("mode") == "blue_light":
        if not 0 < float(tactics.get("lateral_resolution_meters", 0)) <= 1:
            raise ValueError("blue_light requires lateral_resolution_meters in (0, 1]")
        if float(tactics.get("reaction_distance_meters", 0)) <= 0:
            raise ValueError("blue_light requires positive reaction_distance_meters")
        if not 1 <= float(tactics.get("speed_factor", 1)) <= 1.5:
            raise ValueError("blue_light speed_factor must be in [1, 1.5]")
    incident = scenario.get("incident", {"enabled": False})
    if not isinstance(incident, dict) or not isinstance(incident.get("enabled", False), bool):
        raise ValueError("incident must be an object with boolean enabled")
    if incident.get("enabled"):
        required_incident_fields = {"starts_at_seconds", "detection_delay_seconds", "report_latency_seconds",
                                    "vehicle_command_latency_seconds"}
        missing_incident_fields = required_incident_fields - incident.keys()
        if missing_incident_fields:
            raise ValueError(f"incident has no fields: {sorted(missing_incident_fields)}")
        if incident.get("target", INCIDENT_ROUTE_TARGET) != INCIDENT_ROUTE_TARGET:
            raise ValueError(f"incident.target must be {INCIDENT_ROUTE_TARGET}")
        if int(incident.get("route_edge_offset", 2)) < 1:
            raise ValueError("incident.route_edge_offset must be at least 1")
        for field in required_incident_fields:
            if float(incident[field]) < 0:
                raise ValueError(f"incident.{field} must not be negative")
        assessment = incident.get("assessment", {})
        if not isinstance(assessment, dict):
            raise ValueError("incident.assessment must be an object")
        if int(assessment.get("estimated_injured", 0)) < 0 or int(assessment.get("blocked_lanes", 0)) < 0:
            raise ValueError("incident assessment counts must not be negative")


def choose_future_route_edge(route_edges: tuple[str, ...], current_route_index: int,
                             lane_count_by_edge: Callable[[str], int], offset: int) -> str | None:
    """Pick a driveable edge still ahead of a vehicle, never its destination.

    The closure is tied to an active emergency route so the scenario can prove
    rerouting on any imported map without hard-coding unstable OSM edge IDs.
    """
    first_candidate = max(0, current_route_index + offset)
    for edge_id in route_edges[first_candidate:-1]:
        if not edge_id.startswith(":") and lane_count_by_edge(edge_id) > 0:
            return edge_id
    return None


def choose_incident_access_edge(route_edges: tuple[str, ...], current_route_index: int,
                                target_edge: str, lane_count_by_edge: Callable[[str], int]) -> str | None:
    """Return a driveable staging edge before a closed incident edge.

    A responder must reach the incident perimeter, not route into every closed
    lane. The selected edge remains auditable in the event log.
    """
    for index in range(len(route_edges) - 1, max(-1, current_route_index - 1), -1):
        if route_edges[index] != target_edge:
            continue
        for edge_id in reversed(route_edges[max(0, current_route_index):index]):
            if not edge_id.startswith(":") and lane_count_by_edge(edge_id) > 0:
                return edge_id
        return None
    return None


def load_scenario(path: Path) -> dict[str, Any]:
    scenario = json.loads(path.read_text(encoding="utf-8"))
    validate_scenario(scenario)
    return scenario


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def mean_or_zero(values: list[float]) -> float:
    """Return a defined aggregate for time-limited runs with no arrivals yet."""
    return statistics.fmean(values) if values else 0.0


def passenger_component(net: sumolib.net.Net) -> list[sumolib.net.edge.Edge]:
    """Return a connected pool of passenger edges, avoiding isolated service roads."""
    candidates = [edge for edge in net.getEdges() if not edge.isSpecial() and edge.allows("passenger")
                  and edge.getLength() >= 50]
    if len(candidates) < 2:
        raise ValueError("Network has too few passenger edges")
    # Start from the first candidate and retain the reachable directed component.
    seen: set[str] = set()
    pending = [candidates[0]]
    while pending:
        edge = pending.pop()
        if edge.getID() in seen:
            continue
        seen.add(edge.getID())
        pending.extend(next_edge for next_edge in edge.getOutgoing()
                       if not next_edge.isSpecial() and next_edge.allows("passenger"))
    component = [edge for edge in candidates if edge.getID() in seen]
    return component if len(component) >= 2 else candidates


def choose_path(net: sumolib.net.Net, pool: list[sumolib.net.edge.Edge], rng: random.Random,
                strategy: str) -> tuple[str, ...]:
    for _ in range(2_000):
        origin, destination = rng.sample(pool, 2)
        if strategy == "dijkstra":
            path, _ = net.getShortestPath(origin, destination, vClass="passenger")
        else:
            path, _ = net.getFastestPath(origin, destination, vClass="passenger")
        if path and len(path) >= 2:
            return tuple(edge.getID() for edge in path)
    raise RuntimeError("Could not find a passenger route in the selected network")


def build_vehicle_specs(net: sumolib.net.Net, scenario: dict[str, Any],
                        include_emergency: bool = True) -> list[VehicleSpec]:
    rng = random.Random(int(scenario["seed"]))
    pool = passenger_component(net)
    period = max(1, int(scenario.get("ordinary_departure_window_seconds", 600)) // int(scenario["ordinary_vehicles"]))
    specs: list[VehicleSpec] = []
    for index in range(int(scenario["ordinary_vehicles"])):
        specs.append(VehicleSpec(f"civilian_{index:03d}", "civilian",
                                 choose_path(net, pool, rng, scenario["routing_strategy"]),
                                 index * period, False))
    if include_emergency:
        emergency_departures = scenario.get("emergency_departures_seconds", [60, 240])
        for index in range(int(scenario["emergency_vehicles"])):
            specs.append(VehicleSpec(f"emergency_{index:02d}", "ambulance",
                                     choose_path(net, pool, rng, scenario["routing_strategy"]),
                                     int(emergency_departures[index % len(emergency_departures)]), True))
    return specs


def build_dispatcher(net: sumolib.net.Net, scenario: dict[str, Any]) -> tuple[NearestFreeDispatcher, list[EmergencyCall]]:
    """Create deterministic depots and incident destinations for live dispatching."""
    config = scenario["dispatcher"]
    rng = random.Random(f"dispatcher:{scenario['seed']}")
    pool = passenger_component(net)
    fleet = config.get("fleet")
    if fleet is None:
        unit_count = int(config.get("unit_count", scenario["emergency_vehicles"]))
        units = [EmergencyUnit(f"unit_{index:02d}", rng.choice(pool).getID()) for index in range(unit_count)]
    else:
        units = [
            EmergencyUnit(f"{unit_type.value}_{index:02d}", rng.choice(pool).getID(), unit_type=unit_type)
            for unit_type in UnitType
            for index in range(int(fleet.get(unit_type.value, 0)))
        ]
    calls = [EmergencyCall(f"call_{index:02d}", float(call_time), rng.choice(pool).getID())
             for index, call_time in enumerate(config["call_times_seconds"])]
    return NearestFreeDispatcher(units), calls


def dispatcher_route_provider(net: sumolib.net.Net, strategy: str):
    def provider(unit: EmergencyUnit, call: EmergencyCall) -> tuple[tuple[str, ...], float] | None:
        origin, destination = net.getEdge(unit.edge_id), net.getEdge(call.destination_edge_id)
        if strategy == "dijkstra":
            path, cost = net.getShortestPath(origin, destination, vClass="emergency")
        else:
            path, cost = net.getFastestPath(origin, destination, vClass="emergency")
        if not path or len(path) < 2:
            return None
        return tuple(edge.getID() for edge in path), float(cost)
    return provider


def write_routes(path: Path, specs: list[VehicleSpec],
                 emergency_tactics: dict[str, Any] | None = None,
                 weather: WeatherProfile | None = None) -> None:
    routes = ET.Element("routes")
    weather = weather or profile_from_config(None)
    ET.SubElement(routes, "vType", id="civilian", vClass="passenger", accel="2.0", decel="4.5",
                  sigma="0.5", length="5.0", maxSpeed="13.89", speedFactor=str(weather.speed_factor))
    tactics = emergency_tactics or {"mode": "standard"}
    emergency_speed_factor = float(tactics.get("speed_factor", 1.0)) * weather.speed_factor
    ambulance = ET.SubElement(
        routes, "vType", id="ambulance", vClass="emergency", guiShape="emergency", color="1,1,1",
        accel="2.6", decel="5.0", sigma="0.2", length="6.0", maxSpeed="16.67",
        speedFactor=str(emergency_speed_factor),
    )
    if tactics.get("mode") == "blue_light":
        ET.SubElement(ambulance, "param", key="has.bluelight.device", value="true")
    ET.SubElement(routes, "vType", id="police", vClass="emergency", guiShape="police", color="0,0.35,1",
                  accel="2.7", decel="5.2", sigma="0.2", length="5.0", maxSpeed="17.22",
                  speedFactor=str(emergency_speed_factor))
    ET.SubElement(routes, "vType", id="fire_rescue", vClass="emergency", guiShape="firebrigade", color="1,0.15,0",
                  accel="1.8", decel="4.5", sigma="0.2", length="8.0", maxSpeed="13.89",
                  speedFactor=str(emergency_speed_factor))
    # SUMO ignores out-of-order departures. Emergency vehicles may depart before
    # the last civilian, so XML order must not follow the construction order.
    for spec in sorted(specs, key=lambda item: (item.depart, item.vehicle_id)):
        vehicle = ET.SubElement(routes, "vehicle", id=spec.vehicle_id, type=spec.vehicle_type,
                                depart=str(spec.depart), departLane="best", departSpeed="max")
        ET.SubElement(vehicle, "route", edges=" ".join(spec.edges))
    ET.indent(routes)
    ET.ElementTree(routes).write(path, encoding="utf-8", xml_declaration=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def apply_yield_action(vehicle_id: str, slowdown_factor: float, duration_seconds: float) -> bool:
    """Issue a conservative slowdown and, where possible, move to the outer lane."""
    try:
        road_id = traci.vehicle.getRoadID(vehicle_id)
        lanes = traci.edge.getLaneNumber(road_id) if not road_id.startswith(":") else 0
        lane_index = traci.vehicle.getLaneIndex(vehicle_id)
        if lanes > 1 and lane_index > 0:
            traci.vehicle.changeLane(vehicle_id, 0, duration_seconds)
        traci.vehicle.slowDown(vehicle_id, max(1.0, traci.vehicle.getSpeed(vehicle_id) * slowdown_factor), duration_seconds)
        return True
    except traci.TraCIException:
        return False


def run_experiment(scenario_path: Path, output_root: Path | None = None,
                   overrides: dict[str, Any] | None = None) -> Path:
    scenario = load_scenario(scenario_path)
    scenario.update(overrides or {})
    validate_scenario(scenario)
    network = (ROOT / scenario["network"]).resolve()
    if not network.exists():
        raise FileNotFoundError(network)
    sumo = sumolib.checkBinary("sumo")
    run_id = scenario.get("run_id") or f"baseline-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    result_dir = (output_root or ROOT / "experiments" / "results") / run_id
    result_dir.mkdir(parents=True, exist_ok=False)
    resolved_scenario = result_dir / "scenario.json"
    resolved_scenario.write_text(json.dumps(scenario, indent=2), encoding="utf-8")
    net = sumolib.net.readNet(str(network))
    dispatcher_config = scenario.get("dispatcher", {"enabled": False})
    weather_profile = profile_from_config(scenario.get("weather"))
    emergency_driver_config = scenario.get("emergency_driver", {"enabled": False})
    emergency_driver_enabled = emergency_driver_config.get("enabled", False)
    safety_config = scenario.get("safety", {"enabled": False})
    safety_enabled = safety_config.get("enabled", False)
    dispatcher_enabled = dispatcher_config.get("enabled", False)
    specs = build_vehicle_specs(net, scenario, include_emergency=not dispatcher_enabled)
    civilian_ids = [spec.vehicle_id for spec in specs if not spec.emergency]
    emergency_ids = [spec.vehicle_id for spec in specs if spec.emergency]
    dispatcher: NearestFreeDispatcher | None = None
    scheduled_calls: list[EmergencyCall] = []
    route_provider = None
    if dispatcher_enabled:
        dispatcher, scheduled_calls = build_dispatcher(net, scenario)
        scheduled_calls.sort(key=lambda call: (call.created_time_seconds, call.call_id))
        route_provider = dispatcher_route_provider(net, scenario["routing_strategy"])
    yielding_config = scenario.get("civilian_yielding", {"enabled": False})
    yielding_detection_range = (min(float(yielding_config["detection_range_meters"]), weather_profile.visibility_meters)
                                if yielding_config.get("enabled") else None)
    yielding = (CivilianYieldingModel(profiles_for(civilian_ids, int(scenario["seed"])), int(scenario["seed"]),
                                      float(yielding_detection_range))
                if yielding_config.get("enabled") else None)
    route_file = result_dir / "routes.rou.xml"
    tactics_config = scenario.get("emergency_tactics", {"mode": "standard"})
    blue_light = tactics_config.get("mode") == "blue_light"
    write_routes(route_file, specs, tactics_config, weather_profile)
    tripinfo_file = result_dir / "tripinfo.xml"
    command = [sumo, "-n", str(network), "-r", str(route_file), "--seed", str(scenario["seed"]),
               "--time-to-teleport", "-1", "--no-step-log", "true", "--duration-log.disable", "true",
               "--tripinfo-output", str(tripinfo_file)]
    if scenario["tls_mode"] == "off":
        command.append("--tls.all-off")
    if blue_light:
        command.extend(["--lateral-resolution", str(tactics_config["lateral_resolution_meters"]),
                        "--device.bluelight.reactiondist", str(tactics_config["reaction_distance_meters"])])
    if weather_profile.surface_friction < 1:
        command.extend(["--device.friction.probability", "1"])
    if safety_enabled:
        command.extend(["--collision.action", "warn", "--collision.check-junctions"])
    events: list[dict[str, Any]] = []
    incident_config = scenario.get("incident", {"enabled": False})
    incident_enabled = bool(incident_config.get("enabled", False))
    communications = CommunicationBus()
    incident_manager: IncidentManager | None = None
    incident_assessment: IncidentAssessment | None = None
    incident_scene_access_edge: str | None = None
    incident_detection_due_at: float | None = None
    incident_report_sent = False
    incident_reroutes = 0
    incident_message_deliveries = 0
    reroutes = 0
    next_reroute = int(scenario.get("reroute_period_seconds", 30))
    dynamic_specs: list[VehicleSpec] = []
    mission_by_vehicle: dict[str, tuple[str, str]] = {}
    active_vehicle_by_unit: dict[str, str] = {}
    destination_by_vehicle = {spec.vehicle_id: spec.edges[-1] for spec in specs if spec.emergency}
    next_call_index = 0
    forced_unavailability_done = False
    first_assignment_loss_at: tuple[str, float] | None = None
    rescue_lane_observations: set[tuple[str, str]] = set()
    rescue_lane_scan_period = float(tactics_config.get("observation_period_seconds", 5))
    next_rescue_lane_scan = 0.0
    experiment_end_seconds = scenario.get("experiment_end_seconds")
    weather_applied_edges: set[str] = set()
    driver_model = (EmergencyDriverModel(int(scenario["seed"]), weather_profile.reaction_time_multiplier)
                    if emergency_driver_enabled else None)
    driver_id_by_vehicle = {spec.vehicle_id: spec.vehicle_id for spec in specs if spec.emergency}
    driver_last_update_at: dict[str, float] = {}
    next_driver_state_log = 0.0
    emergency_base_speed_factor = weather_profile.speed_factor * float(tactics_config.get("speed_factor", 1.0))
    safety_monitor = (SafetyMonitor(float(safety_config.get("ttc_threshold_seconds", 2.0)))
                      if safety_enabled else None)
    next_safety_observation = 0.0
    collision_pairs_seen: set[tuple[str, ...]] = set()
    collision_count = 0
    emergency_collision_count = 0

    def dispatch(assignment, time_seconds: float) -> str:
        """Add a dispatched emergency vehicle only after the dispatcher chooses it."""
        vehicle_id = f"{assignment.unit_id}__{assignment.call_id}__attempt{assignment.attempt}"
        route_id = f"route__{vehicle_id}"
        traci.route.add(route_id, list(assignment.path))
        traci.vehicle.add(vehicle_id, route_id, typeID=assignment.unit_type.value, depart="now",
                          departLane="best", departSpeed="max")
        dynamic_specs.append(VehicleSpec(vehicle_id, assignment.unit_type.value, assignment.path,
                                         int(time_seconds), True))
        destination_by_vehicle[vehicle_id] = assignment.path[-1]
        mission_by_vehicle[vehicle_id] = (assignment.unit_id, assignment.call_id)
        driver_id_by_vehicle[vehicle_id] = assignment.unit_id
        active_vehicle_by_unit[assignment.unit_id] = vehicle_id
        events.append({"time_seconds": time_seconds, "event": "dispatcher_assigned",
                       "call_id": assignment.call_id, "unit_id": assignment.unit_id,
                       "vehicle_id": vehicle_id, "attempt": assignment.attempt,
                       "unit_type": assignment.unit_type.value,
                       "estimated_seconds": assignment.estimated_seconds})
        return vehicle_id

    def assign_call(call: EmergencyCall, time_seconds: float) -> str | None:
        assert dispatcher is not None and route_provider is not None
        assignment = dispatcher.assign(call.call_id, route_provider)
        if assignment is None:
            events.append({"time_seconds": time_seconds, "event": "dispatcher_no_unit",
                           "call_id": call.call_id})
            return None
        vehicle_id = dispatch(assignment, time_seconds)
        nonlocal first_assignment_loss_at
        loss_after = dispatcher_config.get("forced_first_assignment_unavailable_after_seconds")
        if assignment.attempt == 1 and loss_after is not None and first_assignment_loss_at is None:
            first_assignment_loss_at = (vehicle_id, time_seconds + float(loss_after))
        return vehicle_id

    traci.start(command)
    try:
        events.append({"time_seconds": 0.0, "event": "weather_configured",
                       "condition": weather_profile.condition.value,
                       "surface_friction": weather_profile.surface_friction,
                       "speed_factor": weather_profile.speed_factor,
                       "visibility_meters": weather_profile.visibility_meters,
                       "reaction_time_multiplier": weather_profile.reaction_time_multiplier,
                       "yielding_detection_range_meters": yielding_detection_range if yielding else None})
        while traci.simulation.getMinExpectedNumber() > 0 or next_call_index < len(scheduled_calls):
            traci.simulationStep()
            time = traci.simulation.getTime()
            if experiment_end_seconds is not None and time >= float(experiment_end_seconds):
                events.append({"time_seconds": time, "event": "experiment_time_limit_reached",
                               "experiment_end_seconds": float(experiment_end_seconds)})
                break
            for vehicle_id in traci.simulation.getDepartedIDList():
                events.append({"time_seconds": time, "event": "departed", "vehicle_id": vehicle_id})
            for vehicle_id in traci.simulation.getArrivedIDList():
                events.append({"time_seconds": time, "event": "arrived", "vehicle_id": vehicle_id})
                if dispatcher and vehicle_id in mission_by_vehicle:
                    unit_id, call_id = mission_by_vehicle.pop(vehicle_id)
                    active_vehicle_by_unit.pop(unit_id, None)
                    call = dispatcher.complete(unit_id, time)
                    events.append({"time_seconds": time, "event": "call_completed", "call_id": call_id,
                                   "unit_id": unit_id, "vehicle_id": vehicle_id})
                    # A newly available unit may now serve a call that was waiting for capacity.
                    for pending_call in dispatcher.calls.values():
                        if pending_call.status is CallStatus.PENDING:
                            assign_call(pending_call, time)
            while next_call_index < len(scheduled_calls) and time >= scheduled_calls[next_call_index].created_time_seconds:
                call = scheduled_calls[next_call_index]
                assert dispatcher is not None
                dispatcher.create_call(call)
                events.append({"time_seconds": time, "event": "call_created", "call_id": call.call_id,
                               "destination_edge_id": call.destination_edge_id,
                               "required_unit_type": call.required_unit_type.value})
                assign_call(call, time)
                next_call_index += 1
            active = set(traci.vehicle.getIDList())
            if weather_profile.surface_friction < 1:
                for vehicle_id in active:
                    edge_id = traci.vehicle.getRoadID(vehicle_id)
                    if edge_id.startswith(":") or edge_id in weather_applied_edges:
                        continue
                    traci.edge.setFriction(edge_id, weather_profile.surface_friction)
                    weather_applied_edges.add(edge_id)
            active_emergency_ids = set(emergency_ids) | set(mission_by_vehicle)
            if driver_model is not None:
                for vehicle_id in sorted(active & active_emergency_ids):
                    driver_id = driver_id_by_vehicle[vehicle_id]
                    if vehicle_id not in driver_last_update_at:
                        profile, state = driver_model.register(driver_id)
                        driver_last_update_at[vehicle_id] = time
                        events.append({"time_seconds": time, "event": "emergency_driver_registered",
                                       "vehicle_id": vehicle_id, "driver_id": driver_id,
                                       "profile": asdict(profile), "state": asdict(state)})
                    else:
                        state = driver_model.advance(
                            driver_id, time - driver_last_update_at[vehicle_id], traci.vehicle.getSpeed(vehicle_id),
                            traci.vehicle.getAllowedSpeed(vehicle_id),
                        )
                        driver_last_update_at[vehicle_id] = time
                        traci.vehicle.setSpeedFactor(
                            vehicle_id, emergency_base_speed_factor * state.speed_factor,
                        )
                if time >= next_driver_state_log:
                    for vehicle_id in sorted(active & active_emergency_ids):
                        driver_id = driver_id_by_vehicle[vehicle_id]
                        events.append({"time_seconds": time, "event": "emergency_driver_state",
                                       "vehicle_id": vehicle_id, "driver_id": driver_id,
                                       "state": asdict(driver_model.states[driver_id])})
                    next_driver_state_log = time + 30.0
            if safety_monitor is not None:
                if time >= next_safety_observation:
                    for vehicle_id in sorted(active):
                        leader = traci.vehicle.getLeader(vehicle_id, 0.0)
                        if leader is None:
                            safety_monitor.observe(vehicle_id, None, None, traci.vehicle.getSpeed(vehicle_id), 0.0)
                            continue
                        leader_id, gap_meters = leader
                        if leader_id not in active:
                            safety_monitor.observe(vehicle_id, None, None, traci.vehicle.getSpeed(vehicle_id), 0.0)
                            continue
                        event = safety_monitor.observe(
                            vehicle_id, leader_id, gap_meters, traci.vehicle.getSpeed(vehicle_id),
                            traci.vehicle.getSpeed(leader_id),
                        )
                        if event is not None:
                            events.append({"time_seconds": time, "event": "near_miss",
                                           **near_miss_payload(event)})
                    next_safety_observation = time + float(safety_config.get("observation_period_seconds", 1.0))
                colliding = tuple(sorted(traci.simulation.getCollidingVehiclesIDList()))
                if colliding and colliding not in collision_pairs_seen:
                    collision_pairs_seen.add(colliding)
                    collision_count += 1
                    emergency_in_collision = sorted(set(colliding) & active_emergency_ids)
                    emergency_collision_count += len(emergency_in_collision)
                    events.append({"time_seconds": time, "event": "physical_collision",
                                   "vehicle_ids": list(colliding),
                                   "emergency_vehicle_ids": emergency_in_collision})
                    for vehicle_id in emergency_in_collision:
                        if vehicle_id in mission_by_vehicle:
                            unit_id, call_id = mission_by_vehicle.pop(vehicle_id)
                            active_vehicle_by_unit.pop(unit_id, None)
                            traci.vehicle.remove(vehicle_id)
                            affected_call = dispatcher.mark_unavailable(unit_id) if dispatcher else None
                            events.append({"time_seconds": time, "event": "emergency_vehicle_unavailable",
                                           "vehicle_id": vehicle_id, "unit_id": unit_id, "call_id": call_id,
                                           "reason": "physical_collision"})
                            if affected_call:
                                assign_call(affected_call, time)
                        else:
                            traci.vehicle.remove(vehicle_id)
                            events.append({"time_seconds": time, "event": "emergency_vehicle_unavailable",
                                           "vehicle_id": vehicle_id, "reason": "physical_collision"})
                    active = set(traci.vehicle.getIDList())
                    active_emergency_ids = set(emergency_ids) | set(mission_by_vehicle)
            if (incident_enabled and incident_manager is None
                    and time >= float(incident_config["starts_at_seconds"])):
                # Select an edge ahead of an actual mission only when one exists.
                # This prevents a synthetic closure from silently affecting an
                # unrelated street in a city-scale experiment.
                for vehicle_id in sorted(active & active_emergency_ids):
                    route_edges = tuple(traci.vehicle.getRoute(vehicle_id))
                    target_edge = choose_future_route_edge(
                        route_edges,
                        traci.vehicle.getRouteIndex(vehicle_id),
                        traci.edge.getLaneNumber,
                        int(incident_config.get("route_edge_offset", 2)),
                    )
                    if target_edge is None:
                        continue
                    access_edge = choose_incident_access_edge(
                        route_edges, traci.vehicle.getRouteIndex(vehicle_id), target_edge, traci.edge.getLaneNumber,
                    )
                    if access_edge is None:
                        continue
                    lane_ids = tuple(f"{target_edge}_{index}"
                                     for index in range(traci.edge.getLaneNumber(target_edge)))
                    incident = RoadClosureIncident("scenario_collision", lane_ids, time)
                    incident_manager = IncidentManager([incident])
                    assessment_data = incident_config.get("assessment", {})
                    incident_assessment = IncidentAssessment(
                        incident_id=incident.incident_id,
                        estimated_injured=int(assessment_data.get("estimated_injured", 0)),
                        impact_speed_mps=assessment_data.get("impact_speed_mps"),
                        people_trapped=bool(assessment_data.get("people_trapped", False)),
                        fire_or_smoke=bool(assessment_data.get("fire_or_smoke", False)),
                        hazardous_material=bool(assessment_data.get("hazardous_material", False)),
                        blocked_lanes=len(lane_ids),
                    )
                    incident_scene_access_edge = access_edge
                    events.append({"time_seconds": time, "event": "incident_target_selected",
                                   "incident_id": incident.incident_id, "vehicle_id": vehicle_id,
                                   "target_edge_id": target_edge, "scene_access_edge_id": access_edge,
                                   "closed_lanes": list(lane_ids)})
                    break
            if incident_manager is not None:
                for incident in incident_manager.activate_due(time):
                    for lane_id in incident.lane_ids:
                        traci.lane.setDisallowed(lane_id, ["passenger", "emergency"])
                    incident_detection_due_at = time + float(incident_config["detection_delay_seconds"])
                    events.append({"time_seconds": time, "event": "incident_activated",
                                   "incident_id": incident.incident_id,
                                   "closed_lanes": list(incident.lane_ids),
                                   "detection_due_at_seconds": incident_detection_due_at})
                if (incident_detection_due_at is not None and not incident_report_sent
                        and time >= incident_detection_due_at):
                    report = communications.send(
                        "incident_report", "incident_agent", "dispatcher", time,
                        float(incident_config["report_latency_seconds"]),
                        {"incident_id": "scenario_collision",
                         "closed_lanes": sorted(incident_manager.closed_lanes())},
                    )
                    incident_report_sent = True
                    events.append({"time_seconds": time, "event": "incident_detected",
                                   "message_id": report.message_id,
                                   "will_deliver_at_seconds": report.delivered_at_seconds})
                for message in communications.deliver_due(time):
                    incident_message_deliveries += 1
                    events.append({"time_seconds": time, "event": "message_received",
                                   "message_id": message.message_id, "kind": message.kind,
                                   "sender": message.sender, "recipient": message.recipient,
                                   "sent_at_seconds": message.sent_at_seconds})
                    if message.kind == "incident_report" and message.recipient == "dispatcher":
                        assert incident_assessment is not None
                        plan = ResponsePlanner().plan(incident_assessment)
                        events.append({"time_seconds": time, "event": "dispatcher_response_plan",
                                       "incident_id": plan.incident_id,
                                       "incident_class": plan.incident_class.value,
                                       "police_units": plan.police_units,
                                       "ambulance_units": plan.ambulance_units,
                                       "fire_rescue_units": plan.fire_rescue_units,
                                       "reasons": list(plan.reasons)})
                        if dispatcher is None or incident_scene_access_edge is None:
                            events.append({"time_seconds": time, "event": "response_plan_not_dispatched",
                                           "incident_id": plan.incident_id,
                                           "reason": "dispatcher_disabled_or_scene_access_unavailable"})
                        else:
                            for unit_type, field_name in RESPONSE_UNIT_FIELDS:
                                for index in range(getattr(plan, field_name)):
                                    call = EmergencyCall(
                                        call_id=f"{plan.incident_id}__{unit_type.value}_{index:02d}",
                                        created_time_seconds=time,
                                        destination_edge_id=incident_scene_access_edge,
                                        required_unit_type=unit_type,
                                    )
                                    dispatcher.create_call(call)
                                    events.append({"time_seconds": time, "event": "response_call_created",
                                                   "incident_id": plan.incident_id, "call_id": call.call_id,
                                                   "required_unit_type": unit_type.value,
                                                   "scene_access_edge_id": incident_scene_access_edge})
                                    assign_call(call, time)
                        closed_lanes = set(message.payload["closed_lanes"])
                        for vehicle_id in sorted(active & active_emergency_ids):
                            current_route = tuple(traci.vehicle.getRoute(vehicle_id))
                            if route_avoids_closed_lanes(current_route, closed_lanes):
                                continue
                            command_message = communications.send(
                                "reroute_command", "dispatcher", vehicle_id, time,
                                float(incident_config["vehicle_command_latency_seconds"]), message.payload,
                            )
                            events.append({"time_seconds": time, "event": "dispatcher_reroute_command_sent",
                                           "vehicle_id": vehicle_id, "message_id": command_message.message_id,
                                           "will_deliver_at_seconds": command_message.delivered_at_seconds})
                    elif message.kind == "reroute_command" and message.recipient in active:
                        destination_edge = destination_by_vehicle.get(message.recipient)
                        if destination_edge is None:
                            continue
                        current_edge = traci.vehicle.getRoadID(message.recipient)
                        alternative = traci.simulation.findRoute(
                            current_edge, destination_edge, vType=traci.vehicle.getTypeID(message.recipient),
                        )
                        new_route = tuple(alternative.edges)
                        closed_lanes = set(message.payload["closed_lanes"])
                        if not new_route or not route_avoids_closed_lanes(new_route, closed_lanes):
                            events.append({"time_seconds": time, "event": "reroute_unavailable",
                                           "vehicle_id": message.recipient,
                                           "incident_id": message.payload["incident_id"]})
                            continue
                        old_route = tuple(traci.vehicle.getRoute(message.recipient))
                        traci.vehicle.setRoute(message.recipient, list(new_route))
                        incident_reroutes += 1
                        events.append({"time_seconds": time, "event": "emergency_rerouted",
                                       "vehicle_id": message.recipient, "old_route": list(old_route),
                                       "new_route": list(new_route), "incident_id": message.payload["incident_id"],
                                       "avoids_closed_lane": True, "command_message_id": message.message_id})
            if blue_light:
                for vehicle_id in traci.simulation.getDepartedIDList():
                    if vehicle_id in active_emergency_ids:
                        events.append({"time_seconds": time, "event": "emergency_tactics_active",
                                       "vehicle_id": vehicle_id, "mode": "blue_light"})
            if (dispatcher and first_assignment_loss_at and not forced_unavailability_done and
                    time >= first_assignment_loss_at[1]):
                vehicle_id = first_assignment_loss_at[0]
                if vehicle_id in active and vehicle_id in mission_by_vehicle:
                    unit_id, call_id = mission_by_vehicle.pop(vehicle_id)
                    traci.vehicle.remove(vehicle_id)
                    active_vehicle_by_unit.pop(unit_id, None)
                    affected_call = dispatcher.mark_unavailable(unit_id)
                    events.append({"time_seconds": time, "event": "unit_unavailable", "unit_id": unit_id,
                                   "call_id": call_id, "vehicle_id": vehicle_id,
                                   "reason": "scenario_forced_unavailability"})
                    if affected_call:
                        assign_call(affected_call, time)
                forced_unavailability_done = True
            if yielding:
                civilian_active = active & set(civilian_ids)
                active_emergency_ids = set(emergency_ids) | set(mission_by_vehicle)
                for emergency_id in active & active_emergency_ids:
                    emergency_position = traci.vehicle.getPosition(emergency_id)
                    for civilian_id in civilian_active:
                        civilian_position = traci.vehicle.getPosition(civilian_id)
                        distance = ((emergency_position[0] - civilian_position[0]) ** 2 +
                                    (emergency_position[1] - civilian_position[1]) ** 2) ** .5
                        road_id = traci.vehicle.getRoadID(civilian_id)
                        possible = not road_id.startswith(":") and traci.edge.getLaneNumber(road_id) > 1
                        events.extend(yielding.observe(civilian_id, emergency_id, time, distance, possible))
                for action in yielding.due_actions(time):
                    applied = action.civilian_id in active and apply_yield_action(
                        action.civilian_id, float(yielding_config.get("slowdown_factor", .45)),
                        float(yielding_config.get("slowdown_duration_seconds", 4)))
                    yielding.record_action(applied)
                    events.append({"time_seconds": time, "event": "yield_action_applied" if applied else "yield_action_failed",
                                   "vehicle_id": action.civilian_id, "emergency_id": action.emergency_id})
            if blue_light and time >= next_rescue_lane_scan:
                # SUMO already exposes only the affected vehicles via this
                # parameter. Sampling avoids hundreds of TraCI requests every
                # single tick on the city-scale network.
                for civilian_id in active & set(civilian_ids):
                    try:
                        rescue_lane = traci.vehicle.getParameter(civilian_id, "rescueLane")
                    except traci.TraCIException:
                        rescue_lane = ""
                    for emergency_id in set(rescue_lane.split()) & active_emergency_ids:
                        pair = (civilian_id, emergency_id)
                        if pair not in rescue_lane_observations:
                            rescue_lane_observations.add(pair)
                            events.append({"time_seconds": time, "event": "rescue_lane_forming",
                                           "vehicle_id": civilian_id, "emergency_id": emergency_id})
                next_rescue_lane_scan = time + rescue_lane_scan_period
            if scenario["routing_strategy"] == "traffic_aware" and time >= next_reroute:
                for emergency_id in active & active_emergency_ids:
                    traci.vehicle.rerouteTraveltime(emergency_id, currentTravelTimes=True)
                    reroutes += 1
                    events.append({"time_seconds": time, "event": "rerouted", "vehicle_id": emergency_id})
                next_reroute += int(scenario.get("reroute_period_seconds", 30))
    finally:
        traci.close()
    tripinfos = {item.get("id"): item.attrib for item in ET.parse(tripinfo_file).getroot().findall("tripinfo")}
    rows: list[dict[str, Any]] = []
    driver_rows = driver_model.rows() if driver_model is not None else {}
    driver_defaults = {field: "" for field in next(iter(driver_rows.values()), {})}
    for spec in [*specs, *dynamic_specs]:
        info = tripinfos.get(spec.vehicle_id, {})
        driver_row = driver_rows.get(driver_id_by_vehicle.get(spec.vehicle_id, ""), {})
        rows.append({"vehicle_id": spec.vehicle_id, "vehicle_type": spec.vehicle_type,
                     "emergency": spec.emergency, "depart_planned": spec.depart,
                     # SUMO emits tripinfo also for a TraCI-removed vehicle. It is
                     # an attempted mission, not a completed arrival.
                     "arrived": bool(info) and not info.get("vaporized"), "depart": info.get("depart", ""),
                     "arrival": info.get("arrival", ""), "duration_seconds": info.get("duration", ""),
                     "time_loss_seconds": info.get("timeLoss", ""), "waiting_seconds": info.get("waitingTime", ""),
                     "route_length_meters": info.get("routeLength", ""), "termination_reason": info.get("vaporized", ""),
                     "route_edges": len(spec.edges),
                     **driver_defaults, **driver_row,
                     **(yielding.profile_rows().get(spec.vehicle_id, {}) if yielding else {})})
    write_csv(result_dir / "vehicles.csv", rows, list(rows[0]))
    if driver_rows:
        driver_report_rows = [{"driver_id": driver_id, **row} for driver_id, row in sorted(driver_rows.items())]
        write_csv(result_dir / "drivers.csv", driver_report_rows, list(driver_report_rows[0]))
    emergency_rows = [row for row in rows if row["emergency"]]
    if dispatcher:
        calls = []
        for call in dispatcher.calls.values():
            latest = call.assignments[-1] if call.assignments else None
            vehicle_id = (f"{latest.unit_id}__{latest.call_id}__attempt{latest.attempt}"
                          if latest else "")
            dispatch_time = next((spec.depart for spec in dynamic_specs if spec.vehicle_id == vehicle_id), "")
            completed = call.status is CallStatus.COMPLETED
            calls.append({"call_id": call.call_id, "vehicle_id": vehicle_id,
                          "assigned_unit_id": call.assigned_unit_id or (latest.unit_id if latest else ""),
                          "status": call.status.value, "attempts": len(call.assignments),
                          "created_time_seconds": call.created_time_seconds,
                          "dispatch_time_seconds": dispatch_time,
                          "arrival_time_seconds": call.completed_time_seconds if completed else "",
                          "response_seconds": (call.completed_time_seconds - call.created_time_seconds)
                          if completed and call.completed_time_seconds is not None else "",
                          "success": completed})
    else:
        calls = [{"call_id": f"call_{index:02d}", "vehicle_id": row["vehicle_id"],
                  "assigned_unit_id": row["vehicle_id"], "status": "completed" if row["arrived"] else "failed",
                  "attempts": 1, "created_time_seconds": row["depart_planned"],
                  "dispatch_time_seconds": row["depart_planned"], "arrival_time_seconds": row["arrival"],
                  "response_seconds": row["duration_seconds"], "success": row["arrived"]}
                 for index, row in enumerate(emergency_rows)]
    write_csv(result_dir / "calls.csv", calls, list(calls[0]))
    (result_dir / "event_log.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    numeric = lambda rows_, field: [float(row[field]) for row in rows_ if row[field] not in ("", None)]
    response = numeric(calls, "response_seconds")
    civilian = [row for row in rows if not row["emergency"]]
    civilian_time_loss = numeric(civilian, "time_loss_seconds")
    civilian_waiting = numeric(civilian, "waiting_seconds")
    emergency_route_lengths = numeric(emergency_rows, "route_length_meters")
    final_driver_states = list(driver_model.states.values()) if driver_model is not None else []
    metrics = {
        "total_vehicles": len(rows), "ordinary_vehicles": len(civilian), "emergency_vehicles": len(emergency_rows),
        "emergency_calls": len(calls),
        "completed_vehicles": sum(bool(row["arrived"]) for row in rows),
        "successful_calls": sum(bool(row["success"]) for row in calls),
        "emergency_mean_response_seconds": mean_or_zero(response),
        "emergency_p95_response_seconds": percentile(response, .95),
        "emergency_max_response_seconds": max(response, default=0.0),
        "civilian_mean_time_loss_seconds": mean_or_zero(civilian_time_loss),
        "civilian_mean_waiting_seconds": mean_or_zero(civilian_waiting),
        "emergency_mean_route_length_meters": mean_or_zero(emergency_route_lengths),
        "reroutes": reroutes, "incident_reroutes": incident_reroutes,
        "incident_message_deliveries": incident_message_deliveries,
        "incident_activated": bool(incident_manager and incident_manager.closed_lanes()),
        "weather_condition": weather_profile.condition.value,
        "weather_friction_edges_applied": len(weather_applied_edges),
        "emergency_driver_count": len(final_driver_states),
        "emergency_mean_final_stress": mean_or_zero([state.stress for state in final_driver_states]),
        "emergency_max_final_stress": max((state.stress for state in final_driver_states), default=0.0),
        "emergency_mean_final_fatigue": mean_or_zero([state.fatigue for state in final_driver_states]),
        "emergency_mean_final_reaction_seconds": mean_or_zero([state.reaction_seconds for state in final_driver_states]),
        "collisions": collision_count,
        "emergency_collisions": emergency_collision_count,
        **(safety_monitor.summary() if safety_monitor else {"near_misses": 0, "minimum_ttc_seconds": None}),
        "rescue_lane_formations": len(rescue_lane_observations),
        **(yielding.counts if yielding else {"yield_detections": 0, "yield_attempts": 0, "yield_declined": 0,
                                             "yield_impossible": 0, "yield_actions_applied": 0, "yield_actions_failed": 0}),
    }
    (result_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    metadata = {"run_id": run_id, "created_at_utc": datetime.now(UTC).isoformat(), "network": str(network),
                "network_sha256": sha256(network), "scenario_sha256": sha256(resolved_scenario),
                "sumo_version": subprocess.run([sumo, "--version"], capture_output=True, text=True, check=True).stdout.splitlines()[0],
                "tls_mode": scenario["tls_mode"], "routing_strategy": scenario["routing_strategy"],
                "seed": scenario["seed"], "civilian_yielding": yielding_config, "dispatcher": dispatcher_config,
                "weather": {"condition": weather_profile.condition.value,
                            "surface_friction": weather_profile.surface_friction,
                            "speed_factor": weather_profile.speed_factor,
                            "visibility_meters": weather_profile.visibility_meters,
                            "reaction_time_multiplier": weather_profile.reaction_time_multiplier,
                            "yielding_detection_range_meters": yielding_detection_range if yielding else None},
                "incident": incident_config,
                "emergency_tactics": tactics_config,
                "emergency_driver": emergency_driver_config,
                "safety": safety_config,
                "limitations": ["No weather or collision model yet",
                                "Yielding is a seeded behavioural approximation, not calibrated driver psychology"]}
    (result_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (result_dir / "summary.txt").write_text(
        f"{run_id}: {metrics['completed_vehicles']}/{metrics['total_vehicles']} vehicles, "
        f"{metrics['successful_calls']}/{len(calls)} calls, P95 response {metrics['emergency_p95_response_seconds']:.2f}s\n",
        encoding="utf-8")
    return result_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--tls-mode", choices=sorted(VALID_TLS_MODES))
    parser.add_argument("--routing-strategy", choices=sorted(VALID_ROUTING_STRATEGIES))
    args = parser.parse_args()
    overrides = {key: value for key, value in {"seed": args.seed, "run_id": args.run_id,
                 "tls_mode": args.tls_mode, "routing_strategy": args.routing_strategy}.items()
                 if value is not None}
    print(run_experiment(args.scenario, args.output_root, overrides))


if __name__ == "__main__":
    main()
