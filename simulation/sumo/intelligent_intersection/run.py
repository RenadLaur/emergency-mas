"""Run a reproducible two-lane intersection experiment with an ambulance."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import sumolib
import traci


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from agents.driver.civilian_yielding import CivilianYieldingModel, profiles_for  # noqa: E402
from agents.incident.safety import SafetyMonitor, near_miss_payload  # noqa: E402
from agents.traffic_light.priority import EmergencyPriorityController  # noqa: E402
from simulation.sumo.intelligent_intersection.build_network import build_network  # noqa: E402


SCENARIO_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCENARIO_DIR / "output"
SEED = 42
TLS_ID = "center"
AMBULANCE_ID = "ambulance_0"
APPROACH_EDGE = "west_in"
EXIT_EDGE = "east_out"
DETECTION_RANGE_METERS = 75.0


def _westbound_signal_indices() -> list[int]:
    return [
        index
        for index, alternatives in enumerate(traci.trafficlight.getControlledLinks(TLS_ID))
        if any(link and link[0].startswith(f"{APPROACH_EDGE}_") for link in alternatives)
    ]


def _safe_target_lane(vehicle_id: str) -> int | None:
    """Return a neighbouring lane with a conservative physical clearance."""
    current = traci.vehicle.getLaneIndex(vehicle_id)
    position = traci.vehicle.getLanePosition(vehicle_id)
    for target in range(traci.edge.getLaneNumber(APPROACH_EDGE)):
        if target == current:
            continue
        target_lane = f"{APPROACH_EDGE}_{target}"
        neighbours = traci.lane.getLastStepVehicleIDs(target_lane)
        if all(
            other_id == vehicle_id
            or abs(traci.vehicle.getLanePosition(other_id) - position) >= 12.0
            for other_id in neighbours
        ):
            return target
    return None


def run_scenario(gui: bool = False) -> dict[str, object]:
    """Exercise yielding and traffic-light priority with seeded ordinary drivers."""
    network = build_network()
    OUTPUT_DIR.mkdir(exist_ok=True)
    sumo = sumolib.checkBinary("sumo-gui" if gui else "sumo")
    command = [
        sumo, "-n", str(network), "-r", str(SCENARIO_DIR / "intelligent_intersection.rou.xml"),
        "--seed", str(SEED), "--step-length", "0.1", "--time-to-teleport", "-1",
        "--collision.action", "warn", "--collision.check-junctions", "--no-step-log", "true",
        "--duration-log.disable", "true",
    ]
    if gui:
        command.extend(["--gui-settings-file", str(ROOT / "simulation" / "sumo" / "demo_viewsettings.xml"),
                        "--delay", "250", "--start"])

    priority = EmergencyPriorityController(clearance_seconds=3)
    yielding = CivilianYieldingModel({}, SEED, DETECTION_RANGE_METERS)
    safety = SafetyMonitor(3.0)
    events: list[dict[str, object]] = []
    observed_civilians: set[str] = set()
    maximum_wait: dict[str, float] = {}
    priority_granted = False
    priority_restored = False
    ambulance_arrival_time: float | None = None

    traci.start(command)
    try:
        original_program = traci.trafficlight.getProgram(TLS_ID)
        indices = _westbound_signal_indices()
        if not indices:
            raise RuntimeError("No west-to-east signal link was generated")
        all_red = "r" * len(traci.trafficlight.getRedYellowGreenState(TLS_ID))
        priority_state = "".join("G" if index in indices else "r" for index in range(len(all_red)))

        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            time = traci.simulation.getTime()
            active = set(traci.vehicle.getIDList())
            civilians = {vehicle_id for vehicle_id in active if vehicle_id != AMBULANCE_ID}
            for vehicle_id in civilians:
                maximum_wait[vehicle_id] = max(maximum_wait.get(vehicle_id, 0.0), traci.vehicle.getWaitingTime(vehicle_id))

            approaching = False
            passed = AMBULANCE_ID in traci.simulation.getArrivedIDList()
            if AMBULANCE_ID in active:
                road_id = traci.vehicle.getRoadID(AMBULANCE_ID)
                if road_id == APPROACH_EDGE:
                    remaining = traci.lane.getLength(f"{APPROACH_EDGE}_0") - traci.vehicle.getLanePosition(AMBULANCE_ID)
                    approaching = remaining <= 55
                    ambulance_position = traci.vehicle.getLanePosition(AMBULANCE_ID)
                    for vehicle_id in sorted(civilians):
                        if traci.vehicle.getRoadID(vehicle_id) != APPROACH_EDGE:
                            continue
                        distance = traci.vehicle.getLanePosition(vehicle_id) - ambulance_position
                        if not 0 < distance <= DETECTION_RANGE_METERS:
                            continue
                        if vehicle_id not in yielding.profiles:
                            yielding.profiles.update(profiles_for([vehicle_id], SEED))
                        observed_civilians.add(vehicle_id)
                        events.extend(yielding.observe(vehicle_id, AMBULANCE_ID, time, distance,
                                                       _safe_target_lane(vehicle_id) is not None))
                    leader = traci.vehicle.getLeader(AMBULANCE_ID, 0.0)
                    if leader is not None:
                        leader_id, gap_meters = leader
                        near_miss = safety.observe(AMBULANCE_ID, leader_id, gap_meters,
                                                    traci.vehicle.getSpeed(AMBULANCE_ID),
                                                    traci.vehicle.getSpeed(leader_id))
                        if near_miss is not None:
                            events.append({"time_seconds": time, "event": "near_miss", **near_miss_payload(near_miss)})
                passed = passed or road_id == EXIT_EDGE

            command_to_apply = priority.step(time, approaching, passed)
            if command_to_apply:
                if command_to_apply.action == "set_all_red":
                    traci.trafficlight.setRedYellowGreenState(TLS_ID, all_red)
                    events.append({"time_seconds": time, "event": "priority_requested", "all_red_state": all_red})
                elif command_to_apply.action == "grant_priority":
                    traci.trafficlight.setRedYellowGreenState(TLS_ID, priority_state)
                    priority_granted = True
                    events.append({"time_seconds": time, "event": "priority_granted", "signal_state": priority_state})
                else:
                    traci.trafficlight.setProgram(TLS_ID, original_program)
                    priority_restored = True
                    events.append({"time_seconds": time, "event": "priority_restored", "program_id": original_program})

            for action in yielding.due_actions(time):
                applied = False
                if action.civilian_id in active and traci.vehicle.getRoadID(action.civilian_id) == APPROACH_EDGE:
                    target_lane = _safe_target_lane(action.civilian_id)
                    if target_lane is not None:
                        try:
                            traci.vehicle.changeLane(action.civilian_id, target_lane, 6.0)
                            traci.vehicle.slowDown(action.civilian_id, 7.0, 4.0)
                            applied = True
                        except traci.TraCIException:
                            applied = False
                yielding.record_action(applied)
                events.append({"time_seconds": time, "event": "yield_applied" if applied else "yield_failed",
                               "vehicle_id": action.civilian_id, "emergency_id": action.emergency_id})

            if AMBULANCE_ID in traci.simulation.getArrivedIDList():
                ambulance_arrival_time = time
                events.append({"time_seconds": time, "event": "ambulance_arrived"})
    finally:
        traci.close()

    completed_civilians = len(maximum_wait)
    result: dict[str, object] = {
        "seed": SEED,
        "priority_granted": priority_granted,
        "priority_restored": priority_restored,
        "ambulance_arrived": ambulance_arrival_time is not None,
        "ambulance_arrival_time_seconds": ambulance_arrival_time,
        "civilian_vehicles_observed": completed_civilians,
        "civilians_in_emergency_detection_range": len(observed_civilians),
        "yielding": yielding.counts,
        "ordinary_transport_max_wait_seconds": round(sum(maximum_wait.values()), 3),
        "safety": safety.summary(),
    }
    (OUTPUT_DIR / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "event_log.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    (OUTPUT_DIR / "civilian_profiles.json").write_text(json.dumps(yielding.profile_rows(), indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="show the intersection experiment in SUMO-GUI")
    args = parser.parse_args()
    print(json.dumps(run_scenario(gui=args.gui), indent=2))
