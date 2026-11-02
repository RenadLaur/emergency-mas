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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sumolib
import traci


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.dispatcher.dispatcher import (CallStatus, EmergencyCall, EmergencyUnit,
                                          NearestFreeDispatcher)
from agents.driver.civilian_yielding import CivilianYieldingModel, profiles_for
VALID_TLS_MODES = {"normal", "off"}
VALID_ROUTING_STRATEGIES = {"dijkstra", "traffic_aware"}
VALID_EMERGENCY_TACTICS = {"standard", "blue_light"}


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
        if int(dispatcher.get("unit_count", scenario["emergency_vehicles"])) < 1:
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
    unit_count = int(config.get("unit_count", scenario["emergency_vehicles"]))
    units = [EmergencyUnit(f"unit_{index:02d}", rng.choice(pool).getID()) for index in range(unit_count)]
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
                 emergency_tactics: dict[str, Any] | None = None) -> None:
    routes = ET.Element("routes")
    ET.SubElement(routes, "vType", id="civilian", vClass="passenger", accel="2.0", decel="4.5",
                  sigma="0.5", length="5.0", maxSpeed="13.89")
    tactics = emergency_tactics or {"mode": "standard"}
    ambulance = ET.SubElement(
        routes, "vType", id="ambulance", vClass="emergency", guiShape="emergency", color="1,1,1",
        accel="2.6", decel="5.0", sigma="0.2", length="6.0", maxSpeed="16.67",
        speedFactor=str(tactics.get("speed_factor", 1.0)),
    )
    if tactics.get("mode") == "blue_light":
        ET.SubElement(ambulance, "param", key="has.bluelight.device", value="true")
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
    yielding = (CivilianYieldingModel(profiles_for(civilian_ids, int(scenario["seed"])), int(scenario["seed"]),
                                      float(yielding_config["detection_range_meters"]))
                if yielding_config.get("enabled") else None)
    route_file = result_dir / "routes.rou.xml"
    tactics_config = scenario.get("emergency_tactics", {"mode": "standard"})
    blue_light = tactics_config.get("mode") == "blue_light"
    write_routes(route_file, specs, tactics_config)
    tripinfo_file = result_dir / "tripinfo.xml"
    command = [sumo, "-n", str(network), "-r", str(route_file), "--seed", str(scenario["seed"]),
               "--time-to-teleport", "-1", "--no-step-log", "true", "--duration-log.disable", "true",
               "--tripinfo-output", str(tripinfo_file)]
    if scenario["tls_mode"] == "off":
        command.append("--tls.all-off")
    if blue_light:
        command.extend(["--lateral-resolution", str(tactics_config["lateral_resolution_meters"]),
                        "--device.bluelight.reactiondist", str(tactics_config["reaction_distance_meters"])])
    events: list[dict[str, Any]] = []
    reroutes = 0
    next_reroute = int(scenario.get("reroute_period_seconds", 30))
    dynamic_specs: list[VehicleSpec] = []
    mission_by_vehicle: dict[str, tuple[str, str]] = {}
    active_vehicle_by_unit: dict[str, str] = {}
    next_call_index = 0
    forced_unavailability_done = False
    first_assignment_loss_at: tuple[str, float] | None = None
    rescue_lane_observations: set[tuple[str, str]] = set()
    rescue_lane_scan_period = float(tactics_config.get("observation_period_seconds", 5))
    next_rescue_lane_scan = 0.0

    def dispatch(assignment, time_seconds: float) -> str:
        """Add a dispatched emergency vehicle only after the dispatcher chooses it."""
        vehicle_id = f"{assignment.unit_id}__{assignment.call_id}__attempt{assignment.attempt}"
        route_id = f"route__{vehicle_id}"
        traci.route.add(route_id, list(assignment.path))
        traci.vehicle.add(vehicle_id, route_id, typeID="ambulance", depart="now",
                          departLane="best", departSpeed="max")
        dynamic_specs.append(VehicleSpec(vehicle_id, "ambulance", assignment.path,
                                         int(time_seconds), True))
        mission_by_vehicle[vehicle_id] = (assignment.unit_id, assignment.call_id)
        active_vehicle_by_unit[assignment.unit_id] = vehicle_id
        events.append({"time_seconds": time_seconds, "event": "dispatcher_assigned",
                       "call_id": assignment.call_id, "unit_id": assignment.unit_id,
                       "vehicle_id": vehicle_id, "attempt": assignment.attempt,
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
        while traci.simulation.getMinExpectedNumber() > 0 or next_call_index < len(scheduled_calls):
            traci.simulationStep()
            time = traci.simulation.getTime()
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
                               "destination_edge_id": call.destination_edge_id})
                assign_call(call, time)
                next_call_index += 1
            active = set(traci.vehicle.getIDList())
            active_emergency_ids = set(emergency_ids) | set(mission_by_vehicle)
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
    for spec in [*specs, *dynamic_specs]:
        info = tripinfos.get(spec.vehicle_id, {})
        rows.append({"vehicle_id": spec.vehicle_id, "vehicle_type": spec.vehicle_type,
                     "emergency": spec.emergency, "depart_planned": spec.depart,
                     # SUMO emits tripinfo also for a TraCI-removed vehicle. It is
                     # an attempted mission, not a completed arrival.
                     "arrived": bool(info) and not info.get("vaporized"), "depart": info.get("depart", ""),
                     "arrival": info.get("arrival", ""), "duration_seconds": info.get("duration", ""),
                     "time_loss_seconds": info.get("timeLoss", ""), "waiting_seconds": info.get("waitingTime", ""),
                     "route_length_meters": info.get("routeLength", ""), "termination_reason": info.get("vaporized", ""),
                     "route_edges": len(spec.edges),
                     **(yielding.profile_rows().get(spec.vehicle_id, {}) if yielding else {})})
    write_csv(result_dir / "vehicles.csv", rows, list(rows[0]))
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
    metrics = {
        "total_vehicles": len(rows), "ordinary_vehicles": len(civilian), "emergency_vehicles": len(emergency_rows),
        "emergency_calls": len(calls),
        "completed_vehicles": sum(bool(row["arrived"]) for row in rows),
        "successful_calls": sum(bool(row["success"]) for row in calls),
        "emergency_mean_response_seconds": statistics.fmean(response) if response else 0.0,
        "emergency_p95_response_seconds": percentile(response, .95),
        "emergency_max_response_seconds": max(response, default=0.0),
        "civilian_mean_time_loss_seconds": statistics.fmean(numeric(civilian, "time_loss_seconds")) if civilian else 0.0,
        "civilian_mean_waiting_seconds": statistics.fmean(numeric(civilian, "waiting_seconds")) if civilian else 0.0,
        "emergency_mean_route_length_meters": statistics.fmean(numeric(emergency_rows, "route_length_meters")) if response else 0.0,
        "reroutes": reroutes, "collisions": 0, "near_misses": 0,
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
                "emergency_tactics": tactics_config,
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
