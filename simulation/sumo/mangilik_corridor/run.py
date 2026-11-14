"""Run the long Mangilik corridor through Syganak and Saraishyk bridges."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import sumolib
import traci


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from agents.driver.civilian_yielding import CivilianYieldingModel, profiles_for  # noqa: E402
from agents.incident.safety import SafetyMonitor, near_miss_payload  # noqa: E402


SCENARIO_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCENARIO_DIR / "output"
NETWORK = (
    ROOT
    / "simulation"
    / "maps"
    / "astana_city"
    / "generated"
    / "bridge_preview"
    / "catalog"
    / "mangilik_corridor"
    / "network.net.xml"
)
ROUTES = SCENARIO_DIR / "mangilik_corridor.rou.xml"
AMBULANCE_ID = "ambulance_0"
EXPECTED_CIVILIANS = 100
WARMUP_SECONDS = 120.0
DETECTION_RANGE_METERS = 75.0
PRIORITY_REQUEST_RANGE_METERS = 110.0
MODES = ("normal", "priority", "yielding", "assisted")
SYGANAK_LOWER_EDGES = {"762778578#5", "688297712#12"}
SARAYSHYK_VICINITY_EDGES = {"735041697#0", "165816979#3"}


@dataclass
class _ActivePriority:
    tls_id: str
    link_indices: tuple[int, ...]
    stage: str
    stage_started_at: float


def _safe_target_lane(vehicle_id: str) -> int | None:
    """Find a free neighbouring lane on the current directed edge."""
    edge_id = traci.vehicle.getRoadID(vehicle_id)
    if not edge_id or edge_id.startswith(":"):
        return None
    current_lane = traci.vehicle.getLaneIndex(vehicle_id)
    position = traci.vehicle.getLanePosition(vehicle_id)
    for target_lane_index in range(traci.edge.getLaneNumber(edge_id)):
        if target_lane_index == current_lane:
            continue
        lane_id = f"{edge_id}_{target_lane_index}"
        if "passenger" not in traci.lane.getAllowed(lane_id) and traci.lane.getAllowed(lane_id):
            continue
        neighbours = traci.lane.getLastStepVehicleIDs(lane_id)
        if all(
            other_id == vehicle_id
            or abs(traci.vehicle.getLanePosition(other_id) - position) >= 15.0
            for other_id in neighbours
        ):
            return target_lane_index
    return None


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def _mean_and_std(values: Iterable[float]) -> dict[str, float | int]:
    observations = list(values)
    if not observations:
        return {"n": 0, "mean": 0.0, "sample_std": 0.0}
    return {
        "n": len(observations),
        "mean": round(statistics.fmean(observations), 3),
        "sample_std": round(statistics.stdev(observations), 3) if len(observations) > 1 else 0.0,
    }


def _demand_route_file(output_dir: Path, seed: int, civilian_count: int) -> Path:
    """Create a deterministic demand variant without changing the canonical route file."""
    if civilian_count < 1:
        raise ValueError("civilian_count must be positive")
    root = ET.parse(ROUTES)
    northbound_count = civilian_count // 2
    southbound_count = civilian_count - northbound_count
    flow_counts = {
        "civilian_northbound": northbound_count,
        "civilian_southbound": southbound_count,
    }
    for flow in root.findall("flow"):
        if flow.attrib.get("id") in flow_counts:
            flow.set("number", str(flow_counts[flow.attrib["id"]]))
    found_flow_ids = {flow.attrib.get("id") for flow in root.findall("flow")}
    missing = set(flow_counts) - found_flow_ids
    if missing:
        raise ValueError(f"Canonical route file has no flows: {sorted(missing)}")
    input_dir = output_dir / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    route_path = input_dir / f"routes_seed_{seed}_civilian_{civilian_count}.rou.xml"
    ET.indent(root)
    root.write(route_path, encoding="utf-8", xml_declaration=True)
    return route_path


def _extend_green_if_needed(
    tls_id: str, distance_meters: float, speed_mps: float, time_seconds: float
) -> tuple[bool, float, float]:
    """Extend, never shorten, the active phase to cover the ambulance ETA."""
    remaining = max(0.0, traci.trafficlight.getNextSwitch(tls_id) - time_seconds)
    required = distance_meters / max(speed_mps, 5.0) + 2.0
    if remaining + 0.1 < required:
        traci.trafficlight.setPhaseDuration(tls_id, required)
        return True, remaining, required
    return False, remaining, required


def inspect_corridor() -> dict[str, object]:
    """Return deterministic geometry evidence used by regression tests."""
    net = sumolib.net.readNet(str(NETWORK))
    route_root = ET.parse(ROUTES).getroot()
    routes: dict[str, dict[str, object]] = {}
    for route in route_root.findall("route"):
        edge_ids = route.attrib["edges"].split()
        routes[route.attrib["id"]] = {
            "edge_count": len(edge_ids),
            "length_meters": round(sum(net.getEdge(edge_id).getLength() for edge_id in edge_ids), 1),
            "contains_syganak_lower_level": bool(set(edge_ids) & SYGANAK_LOWER_EDGES),
            "contains_sarayshyk_vicinity": bool(set(edge_ids) & SARAYSHYK_VICINITY_EDGES),
        }

    z_values: list[float] = []
    for _, element in ET.iterparse(NETWORK, events=("end",)):
        shape = element.get("shape")
        if shape:
            for point in shape.split():
                coordinates = point.split(",")
                if len(coordinates) == 3:
                    z_values.append(float(coordinates[2]))
        element.clear()
    nonzero = [z for z in z_values if abs(z) > 1e-9]
    return {
        "routes": routes,
        "three_dimensional_points": len(z_values),
        "nonzero_height_points": len(nonzero),
        "minimum_height_meters": min(z_values),
        "maximum_height_meters": max(z_values),
    }


def _prepare_gui_views(net: sumolib.net.Net) -> None:
    """Keep remote source edges outside the camera and expose a 3D bridge view."""
    default_view = traci.gui.DEFAULT_VIEW
    syganak_x, syganak_y = net.convertLonLat2XY(71.4365, 51.1220)
    sarayshyk_x, sarayshyk_y = net.convertLonLat2XY(71.4387, 51.1325)
    traci.gui.setBoundary(
        default_view,
        min(syganak_x, sarayshyk_x) - 500,
        syganak_y - 350,
        max(syganak_x, sarayshyk_x) + 500,
        sarayshyk_y + 350,
    )
    view_3d = "Mangilik bridges 3D"
    if view_3d not in traci.gui.getIDList():
        traci.gui.addView(view_3d, "real world", True)
    traci.gui.setBoundary(view_3d, syganak_x - 350, syganak_y - 350, syganak_x + 350, syganak_y + 350)


def run_scenario(
    gui: bool = False,
    mode: str = "assisted",
    *,
    seed: int = 42,
    civilian_count: int = EXPECTED_CIVILIANS,
    output_dir: Path | None = None,
) -> dict[str, object]:
    """Run one reproducible demand/mode combination on the fixed corridor."""
    if mode not in MODES:
        raise ValueError("mode must be normal, priority, yielding or assisted")
    if seed < 0:
        raise ValueError("seed must be non-negative")
    if civilian_count < 1:
        raise ValueError("civilian_count must be positive")
    priority_enabled = mode in {"priority", "assisted"}
    yielding_enabled = mode in {"yielding", "assisted"}
    if not NETWORK.exists():
        raise FileNotFoundError(
            "Mangilik bridge corridor is missing; rebuild profile mangilik_corridor "
            "with simulation/maps/astana_city/build_bridge_catalog.py"
        )
    output_dir = output_dir or OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    routes = _demand_route_file(output_dir, seed, civilian_count)
    net = sumolib.net.readNet(str(NETWORK))
    geometry = inspect_corridor()
    sumo = sumolib.checkBinary("sumo-gui" if gui else "sumo")
    command = [
        sumo,
        "-n",
        str(NETWORK),
        "-r",
        str(routes),
        "--seed",
        str(seed),
        "--step-length",
        "0.2",
        "--time-to-teleport",
        "-1",
        "--collision.action",
        "warn",
        "--collision.check-junctions",
        "--no-step-log",
        "true",
        "--duration-log.disable",
        "true",
    ]
    if gui:
        command.extend([
            "--gui-settings-file",
            str(ROOT / "simulation" / "sumo" / "demo_viewsettings.xml"),
            "--delay",
            "20",
            "--start",
        ])

    safety = SafetyMonitor(3.0)
    yielding = CivilianYieldingModel({}, seed, DETECTION_RANGE_METERS)
    events: list[dict[str, object]] = []
    collision_keys: set[tuple[str, str, str, int]] = set()
    physical_collisions: list[dict[str, object]] = []
    civilians_departed: set[str] = set()
    civilians_arrived = 0
    civilian_departure_times: dict[str, float] = {}
    civilian_travel_times: list[float] = []
    civilian_time_loss: dict[str, float] = {}
    ambulance_arrival: float | None = None
    tracked = False
    active_priority: _ActivePriority | None = None
    priority_requests = 0
    deferred_requests = 0
    priority_windows = 0

    traci.start(command)
    try:
        if gui:
            _prepare_gui_views(net)
        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            time = traci.simulation.getTime()
            active = set(traci.vehicle.getIDList())
            departed = set(traci.simulation.getDepartedIDList())
            arrived = set(traci.simulation.getArrivedIDList())
            civilian_departures = {vehicle_id for vehicle_id in departed if vehicle_id != AMBULANCE_ID}
            civilian_arrivals = {vehicle_id for vehicle_id in arrived if vehicle_id != AMBULANCE_ID}
            civilians_departed.update(civilian_departures)
            civilians_arrived += len(civilian_arrivals)
            for vehicle_id in civilian_departures:
                civilian_departure_times[vehicle_id] = time
            for vehicle_id in civilian_arrivals:
                departure_time = civilian_departure_times.get(vehicle_id)
                if departure_time is not None:
                    civilian_travel_times.append(time - departure_time)
            civilians = active - {AMBULANCE_ID}
            for vehicle_id in civilians:
                civilian_time_loss[vehicle_id] = traci.vehicle.getTimeLoss(vehicle_id)

            for collision in traci.simulation.getCollisions():
                first, second = sorted((collision.collider, collision.victim))
                key = (first, second, collision.lane, round(collision.pos))
                if key in collision_keys:
                    continue
                collision_keys.add(key)
                payload = {
                    "time_seconds": time,
                    "event": "physical_collision",
                    "collider_vehicle_id": collision.collider,
                    "victim_vehicle_id": collision.victim,
                    "collision_type": collision.type,
                    "lane_id": collision.lane,
                    "position_meters": round(collision.pos, 3),
                    "relative_speed_mps": round(
                        abs(collision.colliderSpeed - collision.victimSpeed), 3
                    ),
                    "involves_ambulance": AMBULANCE_ID in {collision.collider, collision.victim},
                }
                physical_collisions.append(payload)
                events.append(payload)

            if gui and not tracked and time >= WARMUP_SECONDS and AMBULANCE_ID in active:
                traci.gui.trackVehicle("Mangilik bridges 3D", AMBULANCE_ID)
                tracked = True

            if AMBULANCE_ID in active:
                road_id = traci.vehicle.getRoadID(AMBULANCE_ID)
                if road_id in SYGANAK_LOWER_EDGES:
                    events.append({"time_seconds": time, "event": "ambulance_entered_syganak_underpass"})
                if road_id in SARAYSHYK_VICINITY_EDGES:
                    events.append({"time_seconds": time, "event": "ambulance_reached_sarayshyk_interchange"})

                if (
                    yielding_enabled
                    and time >= WARMUP_SECONDS
                    and traci.vehicle.getSpeed(AMBULANCE_ID) > 1.0
                    and not road_id.startswith(":")
                ):
                    ambulance_position = traci.vehicle.getLanePosition(AMBULANCE_ID)
                    ambulance_lane_index = traci.vehicle.getLaneIndex(AMBULANCE_ID)
                    for vehicle_id in sorted(civilians):
                        if traci.vehicle.getRoadID(vehicle_id) != road_id:
                            continue
                        if traci.vehicle.getLaneIndex(vehicle_id) != ambulance_lane_index:
                            continue
                        distance = traci.vehicle.getLanePosition(vehicle_id) - ambulance_position
                        if not 0 < distance <= DETECTION_RANGE_METERS:
                            continue
                        if vehicle_id not in yielding.profiles:
                            yielding.profiles.update(profiles_for([vehicle_id], seed))
                        events.extend(
                            yielding.observe(
                                vehicle_id,
                                AMBULANCE_ID,
                                time,
                                distance,
                                _safe_target_lane(vehicle_id) is not None,
                            )
                        )

                next_tls = [item for item in traci.vehicle.getNextTLS(AMBULANCE_ID) if item[1] >= 0]
                if priority_enabled and active_priority is not None:
                    still_ahead = any(
                        item[0] == active_priority.tls_id
                        for item in next_tls
                    )
                    if not still_ahead:
                        if active_priority.stage == "priority":
                            events.append({
                                "time_seconds": time,
                                "event": "priority_completed",
                                "tls_id": active_priority.tls_id,
                            })
                        else:
                            events.append({
                                "time_seconds": time,
                                "event": "priority_deferred_expired"
                                if active_priority.stage == "deferred"
                                else "priority_not_needed_completed",
                                "tls_id": active_priority.tls_id,
                            })
                        active_priority = None
                    elif active_priority.stage == "deferred":
                        current_state = traci.trafficlight.getRedYellowGreenState(
                            active_priority.tls_id
                        )
                        became_green = all(
                            current_state[index] in "gG" for index in active_priority.link_indices
                        )
                        if became_green:
                            distance_to_tls = min(
                                item[2]
                                for item in next_tls
                                if item[0] == active_priority.tls_id
                            )
                            extended, remaining, required = _extend_green_if_needed(
                                active_priority.tls_id,
                                distance_to_tls,
                                traci.vehicle.getSpeed(AMBULANCE_ID),
                                time,
                            )
                            active_priority.stage = "priority" if extended else "observing"
                            active_priority.stage_started_at = time
                            if extended:
                                priority_windows += 1
                            events.append({
                                "time_seconds": time,
                                "event": "priority_granted" if extended else "priority_not_needed",
                                "tls_id": active_priority.tls_id,
                                "link_indices": list(active_priority.link_indices),
                                "transition": "deferred_until_green",
                                "remaining_green_seconds": round(remaining, 2),
                                "required_green_seconds": round(required, 2),
                            })

                if priority_enabled and active_priority is None and next_tls:
                    tls_id, link_index, distance_to_tls, _ = min(next_tls, key=lambda item: item[2])
                    if distance_to_tls <= PRIORITY_REQUEST_RANGE_METERS:
                        link_indices = tuple(sorted({
                            item[1] for item in next_tls if item[0] == tls_id
                        }))
                        original_state = traci.trafficlight.getRedYellowGreenState(tls_id)
                        already_green = all(original_state[index] in "gG" for index in link_indices)
                        priority_requests += 1
                        if already_green:
                            extended, remaining, required = _extend_green_if_needed(
                                tls_id,
                                distance_to_tls,
                                traci.vehicle.getSpeed(AMBULANCE_ID),
                                time,
                            )
                            stage = "priority" if extended else "observing"
                            if extended:
                                priority_windows += 1
                        else:
                            remaining = None
                            required = None
                            stage = "deferred"
                            deferred_requests += 1
                        active_priority = _ActivePriority(
                            tls_id,
                            link_indices,
                            stage,
                            time,
                        )
                        events.append({
                            "time_seconds": time,
                            "event": "priority_requested",
                            "tls_id": tls_id,
                            "link_indices": list(link_indices),
                            "distance_meters": round(distance_to_tls, 2),
                            "transition": (
                                "green_extended"
                                if already_green and extended
                                else "green_already_sufficient"
                                if already_green
                                else "deferred_until_green"
                            ),
                        })
                        if already_green:
                            events.append({
                                "time_seconds": time,
                                "event": "priority_granted" if extended else "priority_not_needed",
                                "tls_id": tls_id,
                                "link_indices": list(link_indices),
                                "remaining_green_seconds": round(remaining, 2),
                                "required_green_seconds": round(required, 2),
                            })

                leader = traci.vehicle.getLeader(AMBULANCE_ID, 0.0)
                if leader is not None:
                    leader_id, gap = leader
                    event = safety.observe(
                        AMBULANCE_ID,
                        leader_id,
                        gap,
                        traci.vehicle.getSpeed(AMBULANCE_ID),
                        traci.vehicle.getSpeed(leader_id),
                    )
                    if event:
                        events.append({"time_seconds": time, "event": "near_miss", **near_miss_payload(event)})

            if yielding_enabled:
                for action in yielding.due_actions(time):
                    applied = False
                    if action.civilian_id in active:
                        target_lane = _safe_target_lane(action.civilian_id)
                        if target_lane is not None:
                            try:
                                traci.vehicle.changeLane(action.civilian_id, target_lane, 6.0)
                                applied = True
                            except traci.TraCIException:
                                applied = False
                    yielding.record_action(applied)
                    events.append({
                        "time_seconds": time,
                        "event": "yield_applied" if applied else "yield_failed",
                        "vehicle_id": action.civilian_id,
                        "emergency_id": action.emergency_id,
                    })
            if AMBULANCE_ID in arrived:
                ambulance_arrival = time
                events.append({"time_seconds": time, "event": "ambulance_arrived"})
    finally:
        traci.close()

    # Collapse per-step location events to one timestamp per bridge.
    unique_events: list[dict[str, object]] = []
    seen: set[str] = set()
    for event in events:
        name = str(event["event"])
        if name.startswith("ambulance_") and name in seen:
            continue
        seen.add(name)
        unique_events.append(event)

    safety_summary = safety.summary()
    safety_summary.update({
        "physical_collisions": len(physical_collisions),
        "emergency_vehicle_collisions": sum(
            bool(collision["involves_ambulance"]) for collision in physical_collisions
        ),
    })
    result: dict[str, object] = {
        "seed": seed,
        "mode": mode,
        "warmup_seconds": WARMUP_SECONDS,
        "synthetic_demand": True,
        "civilian_vehicles_expected": civilian_count,
        "civilian_vehicles_departed": len(civilians_departed),
        "civilian_vehicles_arrived": civilians_arrived,
        "ambulance_arrived": ambulance_arrival is not None,
        "ambulance_arrival_time_seconds": ambulance_arrival,
        "ambulance_response_time_seconds": (
            round(ambulance_arrival - WARMUP_SECONDS, 3) if ambulance_arrival is not None else None
        ),
        "traffic_light_priority": {
            "requests": priority_requests,
            "deferred_requests": deferred_requests,
            "windows_granted": priority_windows,
            "program_resets": 0,
        },
        "yielding": yielding.counts,
        "ordinary_transport": {
            "mean_travel_time_seconds": round(statistics.fmean(civilian_travel_times), 3),
            "p95_travel_time_seconds": round(_p95(civilian_travel_times), 3),
            "mean_time_loss_seconds": round(statistics.fmean(civilian_time_loss.values()), 3),
            "p95_time_loss_seconds": round(_p95(list(civilian_time_loss.values())), 3),
        },
        "geometry": geometry,
        "safety": safety_summary,
    }
    (output_dir / f"run_summary_{mode}.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    (output_dir / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (output_dir / f"event_log_{mode}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in unique_events), encoding="utf-8"
    )
    (output_dir / f"civilian_profiles_{mode}.json").write_text(
        json.dumps(yielding.profile_rows(), indent=2), encoding="utf-8"
    )
    return result


def _effect_against_normal(
    normal: dict[str, object], candidate: dict[str, object]
) -> dict[str, float]:
    normal_transport = normal["ordinary_transport"]
    candidate_transport = candidate["ordinary_transport"]
    normal_safety = normal["safety"]
    candidate_safety = candidate["safety"]
    return {
        "ambulance_response_time_delta_seconds": round(
            float(candidate["ambulance_response_time_seconds"])
            - float(normal["ambulance_response_time_seconds"]),
            3,
        ),
        "civilian_mean_travel_time_delta_seconds": round(
            float(candidate_transport["mean_travel_time_seconds"])
            - float(normal_transport["mean_travel_time_seconds"]),
            3,
        ),
        "civilian_mean_time_loss_delta_seconds": round(
            float(candidate_transport["mean_time_loss_seconds"])
            - float(normal_transport["mean_time_loss_seconds"]),
            3,
        ),
        "near_miss_delta": float(candidate_safety["near_misses"])
        - float(normal_safety["near_misses"]),
        "physical_collision_delta": float(candidate_safety["physical_collisions"])
        - float(normal_safety["physical_collisions"]),
    }


def _write_comparison(
    results: dict[str, dict[str, object]], output_dir: Path = OUTPUT_DIR
) -> dict[str, object]:
    normal = results["normal"]
    normal_time = float(normal["ambulance_response_time_seconds"])
    assisted_time = float(results["assisted"]["ambulance_response_time_seconds"])
    comparison = {
        "seed": normal["seed"],
        "controlled_factors": {
            "same_network": True,
            "same_routes": True,
            "same_civilian_demand": True,
            "same_weather": "dry",
        },
        **results,
        "effects_against_normal": {
            mode: _effect_against_normal(normal, result)
            for mode, result in results.items()
            if mode != "normal"
        },
        "ambulance_time_saved_seconds": round(normal_time - assisted_time, 3),
        "ambulance_time_saved_percent": round((normal_time - assisted_time) / normal_time * 100, 3),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    return comparison


def assemble_comparison(
    *, seed: int = 42, civilian_count: int = EXPECTED_CIVILIANS, output_dir: Path = OUTPUT_DIR
) -> dict[str, object]:
    """Write a comparison from four compatible completed mode summaries."""
    results: dict[str, dict[str, object]] = {}
    for mode in MODES:
        summary_path = output_dir / f"run_summary_{mode}.json"
        if not summary_path.exists():
            raise FileNotFoundError(f"Missing completed mode summary: {summary_path}")
        result = json.loads(summary_path.read_text(encoding="utf-8"))
        if (
            result.get("mode") != mode
            or result.get("seed") != seed
            or result.get("civilian_vehicles_expected") != civilian_count
        ):
            raise ValueError(f"Incompatible mode summary: {summary_path}")
        results[mode] = result
    return _write_comparison(results, output_dir)


def _completed_mode_summary(
    output_dir: Path, mode: str, seed: int, civilian_count: int
) -> dict[str, object] | None:
    summary_path = output_dir / f"run_summary_{mode}.json"
    if not summary_path.exists():
        return None
    try:
        result = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if (
        result.get("mode") != mode
        or result.get("seed") != seed
        or result.get("civilian_vehicles_expected") != civilian_count
        or not result.get("ambulance_arrived")
        or "physical_collisions" not in result.get("safety", {})
    ):
        return None
    return result


def run_comparison(
    *,
    seed: int = 42,
    civilian_count: int = EXPECTED_CIVILIANS,
    output_dir: Path = OUTPUT_DIR,
    resume: bool = False,
    progress: bool = False,
) -> dict[str, object]:
    """Run a controlled ablation where one assistance component changes at a time."""
    results: dict[str, dict[str, object]] = {}
    for mode in MODES:
        result = (
            _completed_mode_summary(output_dir, mode, seed, civilian_count)
            if resume
            else None
        )
        if progress:
            action = "reuse" if result is not None else "run"
            print(
                f"[matrix] {action}: demand={civilian_count} seed={seed} mode={mode}",
                file=sys.stderr,
                flush=True,
            )
        if result is None:
            result = run_scenario(
                mode=mode,
                seed=seed,
                civilian_count=civilian_count,
                output_dir=output_dir,
            )
        results[mode] = result
    return _write_comparison(results, output_dir)


def _summary_metric(result: dict[str, object], field: str) -> float:
    if field == "ambulance_response_time_seconds":
        response_time = result[field]
        if response_time is None:
            raise ValueError("Cannot aggregate a run where the ambulance did not arrive")
        return float(response_time)
    if field == "civilian_mean_travel_time_seconds":
        return float(result["ordinary_transport"]["mean_travel_time_seconds"])
    if field == "civilian_mean_time_loss_seconds":
        return float(result["ordinary_transport"]["mean_time_loss_seconds"])
    if field == "near_misses":
        return float(result["safety"]["near_misses"])
    if field == "physical_collisions":
        return float(result["safety"]["physical_collisions"])
    raise ValueError(f"Unknown aggregate metric: {field}")


def _aggregate_mode_results(mode_results: list[dict[str, object]]) -> dict[str, object]:
    metric_fields = (
        "ambulance_response_time_seconds",
        "civilian_mean_travel_time_seconds",
        "civilian_mean_time_loss_seconds",
        "near_misses",
        "physical_collisions",
    )
    return {
        "runs": len(mode_results),
        "ambulance_success_rate": round(
            sum(bool(result["ambulance_arrived"]) for result in mode_results) / len(mode_results), 3
        ),
        "metrics": {
            field: _mean_and_std(_summary_metric(result, field) for result in mode_results)
            for field in metric_fields
        },
    }


def run_experiment_matrix(
    seeds: Iterable[int], civilian_counts: Iterable[int], *, output_dir: Path | None = None
) -> dict[str, object]:
    """Run paired mode comparisons across seeds and traffic-demand levels."""
    normalized_seeds = tuple(sorted(set(seeds)))
    normalized_counts = tuple(sorted(set(civilian_counts)))
    if not normalized_seeds or not normalized_counts:
        raise ValueError("At least one seed and civilian_count are required")
    if any(seed < 0 for seed in normalized_seeds) or any(count < 1 for count in normalized_counts):
        raise ValueError("Seeds must be non-negative and civilian counts must be positive")
    if output_dir is None:
        seeds_label = "-".join(str(seed) for seed in normalized_seeds)
        demand_label = "-".join(str(count) for count in normalized_counts)
        output_dir = OUTPUT_DIR / "batches" / f"seeds_{seeds_label}__demand_{demand_label}"
    output_dir.mkdir(parents=True, exist_ok=True)

    cells: list[dict[str, object]] = []
    aggregates: dict[str, dict[str, object]] = {}
    for civilian_count in normalized_counts:
        comparisons: list[dict[str, object]] = []
        for seed in normalized_seeds:
            cell_dir = output_dir / f"civilian_{civilian_count}" / f"seed_{seed}"
            comparison = run_comparison(
                seed=seed,
                civilian_count=civilian_count,
                output_dir=cell_dir,
                resume=True,
                progress=True,
            )
            comparisons.append(comparison)
            cells.append({
                "seed": seed,
                "civilian_count": civilian_count,
                "comparison_path": str(cell_dir / "comparison.json"),
            })
        aggregates[str(civilian_count)] = {
            "seeds": list(normalized_seeds),
            "modes": {
                mode: _aggregate_mode_results([comparison[mode] for comparison in comparisons])
                for mode in MODES
            },
            "effects_against_normal": {
                mode: {
                    metric: _mean_and_std(
                        float(comparison["effects_against_normal"][mode][metric])
                        for comparison in comparisons
                    )
                    for metric in next(iter(comparisons))["effects_against_normal"][mode]
                }
                for mode in MODES
                if mode != "normal"
            },
        }
    matrix = {
        "network": str(NETWORK),
        "route_template": str(ROUTES),
        "seeds": list(normalized_seeds),
        "civilian_counts": list(normalized_counts),
        "modes": list(MODES),
        "cells": cells,
        "aggregates_by_civilian_count": aggregates,
    }
    (output_dir / "batch_summary.json").write_text(json.dumps(matrix, indent=2), encoding="utf-8")
    return matrix


def _parse_csv_integers(value: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected comma-separated integers") from error
    if not values:
        raise argparse.ArgumentTypeError("Expected at least one integer")
    return values


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="open 2D overview and 3D tracked ambulance view")
    parser.add_argument(
        "--mode", choices=MODES, default="assisted"
    )
    parser.add_argument("--seed", type=int, default=42, help="SUMO and driver-profile seed")
    parser.add_argument(
        "--civilian-count", type=int, default=EXPECTED_CIVILIANS,
        help="total number of civilian vehicles across both directions",
    )
    parser.add_argument("--compare", action="store_true", help="run all four modes headlessly")
    parser.add_argument(
        "--assemble-comparison",
        action="store_true",
        help="write comparison.json from four completed mode summaries",
    )
    parser.add_argument(
        "--batch-seeds", type=_parse_csv_integers,
        help="comma-separated seeds for a paired experiment matrix",
    )
    parser.add_argument(
        "--batch-civilian-counts", type=_parse_csv_integers,
        help="comma-separated demand levels for a paired experiment matrix",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        help="directory for one run, comparison, or batch artefacts",
    )
    args = parser.parse_args()
    batch_requested = args.batch_seeds is not None or args.batch_civilian_counts is not None
    if batch_requested:
        if args.batch_seeds is None or args.batch_civilian_counts is None:
            parser.error("--batch-seeds and --batch-civilian-counts must be used together")
        if args.gui or args.compare or args.assemble_comparison:
            parser.error("batch mode cannot be combined with --gui, --compare or --assemble-comparison")
        print(json.dumps(
            run_experiment_matrix(
                args.batch_seeds, args.batch_civilian_counts, output_dir=args.output_dir
            ),
            indent=2,
        ))
    elif args.compare:
        if args.assemble_comparison:
            parser.error("--compare cannot be combined with --assemble-comparison")
        if args.gui:
            parser.error("--compare cannot be combined with --gui")
        print(json.dumps(
            run_comparison(
                seed=args.seed, civilian_count=args.civilian_count, output_dir=args.output_dir or OUTPUT_DIR
            ),
            indent=2,
        ))
    elif args.assemble_comparison:
        print(json.dumps(
            assemble_comparison(
                seed=args.seed,
                civilian_count=args.civilian_count,
                output_dir=args.output_dir or OUTPUT_DIR,
            ),
        ))
    else:
        print(json.dumps(
            run_scenario(
                gui=args.gui,
                mode=args.mode,
                seed=args.seed,
                civilian_count=args.civilian_count,
                output_dir=args.output_dir or OUTPUT_DIR,
            ),
            indent=2,
        ))
