"""Run the first real-network pilot at Mangilik El and Uly Dala."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import sumolib
import traci


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from agents.incident.safety import SafetyMonitor, near_miss_payload  # noqa: E402
from agents.traffic_light.priority import EmergencyPriorityController  # noqa: E402


SCENARIO_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCENARIO_DIR / "output"
NETWORK = ROOT / "simulation" / "maps" / "astana_city" / "generated" / "corridors" / "mangilik" / "network.net.xml"
ROUTES = SCENARIO_DIR / "mangilik_uly_dala.rou.xml"
TLS_ID = "GS_cluster_1773372178_1773372179_1773372182_1773430682"
AMBULANCE_ID = "ambulance_0"
APPROACH_EDGE = "1531638144#6"
EXIT_EDGE = "522977372#0"
EXPECTED_CIVILIANS = 100


def _ambulance_signal_indices() -> list[int]:
    """Resolve only straight Mangilik links used by the ambulance route."""
    indices: list[int] = []
    for index, alternatives in enumerate(traci.trafficlight.getControlledLinks(TLS_ID)):
        if any(link and link[0].startswith(f"{APPROACH_EDGE}_")
               and link[1].startswith(f"{EXIT_EDGE}_") for link in alternatives):
            indices.append(index)
    return indices


def run_scenario(gui: bool = False, enable_priority: bool = True) -> dict[str, object]:
    """Run 100 synthetic civilians and one priority ambulance on real geometry."""
    if not NETWORK.exists():
        raise FileNotFoundError(
            "Mangilik corridor network is missing; run "
            "python simulation/maps/astana_city/corridors.py --corridor mangilik"
        )
    OUTPUT_DIR.mkdir(exist_ok=True)
    sumo = sumolib.checkBinary("sumo-gui" if gui else "sumo")
    command = [
        sumo, "-n", str(NETWORK), "-r", str(ROUTES), "--seed", "42",
        "--step-length", "0.1", "--time-to-teleport", "-1",
        "--collision.action", "warn", "--collision.check-junctions",
        "--no-step-log", "true", "--duration-log.disable", "true",
    ]
    if gui:
        command.extend(["--gui-settings-file", str(ROOT / "simulation" / "sumo" / "demo_viewsettings.xml"),
                        "--delay", "150", "--start"])

    priority = EmergencyPriorityController(clearance_seconds=3)
    safety = SafetyMonitor(3.0)
    events: list[dict[str, object]] = []
    civilian_ids: set[str] = set()
    civilian_arrivals = 0
    maximum_wait: dict[str, float] = {}
    ambulance_departed = False
    ambulance_arrival_time: float | None = None
    priority_granted = False
    priority_restored = False

    traci.start(command)
    try:
        original_program = traci.trafficlight.getProgram(TLS_ID)
        signal_indices = _ambulance_signal_indices() if enable_priority else []
        if enable_priority and not signal_indices:
            raise RuntimeError("No straight Mangilik TLS links found for ambulance route")
        all_red = "r" * len(traci.trafficlight.getRedYellowGreenState(TLS_ID))
        priority_state = "".join("G" if index in signal_indices else "r" for index in range(len(all_red)))

        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            time = traci.simulation.getTime()
            active = set(traci.vehicle.getIDList())
            departed = set(traci.simulation.getDepartedIDList())
            arrived = set(traci.simulation.getArrivedIDList())
            civilian_ids.update(vehicle_id for vehicle_id in departed if vehicle_id != AMBULANCE_ID)
            civilian_arrivals += sum(vehicle_id != AMBULANCE_ID for vehicle_id in arrived)
            ambulance_departed = ambulance_departed or AMBULANCE_ID in departed
            for vehicle_id in active - {AMBULANCE_ID}:
                maximum_wait[vehicle_id] = max(maximum_wait.get(vehicle_id, 0.0),
                                               traci.vehicle.getWaitingTime(vehicle_id))

            approaching = False
            passed = AMBULANCE_ID in arrived
            if AMBULANCE_ID in active:
                road_id = traci.vehicle.getRoadID(AMBULANCE_ID)
                if road_id == APPROACH_EDGE:
                    lane_id = traci.vehicle.getLaneID(AMBULANCE_ID)
                    remaining = traci.lane.getLength(lane_id) - traci.vehicle.getLanePosition(AMBULANCE_ID)
                    approaching = remaining <= 80
                    leader = traci.vehicle.getLeader(AMBULANCE_ID, 0.0)
                    if leader is not None:
                        leader_id, gap_meters = leader
                        event = safety.observe(AMBULANCE_ID, leader_id, gap_meters,
                                               traci.vehicle.getSpeed(AMBULANCE_ID),
                                               traci.vehicle.getSpeed(leader_id))
                        if event:
                            events.append({"time_seconds": time, "event": "near_miss", **near_miss_payload(event)})
                passed = passed or road_id == EXIT_EDGE

            priority_command = priority.step(time, approaching, passed) if enable_priority else None
            if priority_command:
                if priority_command.action == "set_all_red":
                    traci.trafficlight.setRedYellowGreenState(TLS_ID, all_red)
                    events.append({"time_seconds": time, "event": "priority_requested"})
                elif priority_command.action == "grant_priority":
                    traci.trafficlight.setRedYellowGreenState(TLS_ID, priority_state)
                    priority_granted = True
                    events.append({"time_seconds": time, "event": "priority_granted",
                                   "signal_indices": signal_indices})
                else:
                    traci.trafficlight.setProgram(TLS_ID, original_program)
                    priority_restored = True
                    events.append({"time_seconds": time, "event": "priority_restored"})

            if AMBULANCE_ID in arrived:
                ambulance_arrival_time = time
                events.append({"time_seconds": time, "event": "ambulance_arrived"})
    finally:
        traci.close()

    result: dict[str, object] = {
        "seed": 42,
        "junction_id": "cluster_1773372178_1773372179_1773372182_1773430682",
        "tls_id": TLS_ID,
        "synthetic_demand": True,
        "priority_enabled": enable_priority,
        "civilian_vehicles_expected": EXPECTED_CIVILIANS,
        "civilian_vehicles_departed": len(civilian_ids),
        "civilian_vehicles_arrived": civilian_arrivals,
        "ordinary_transport_max_wait_seconds": round(sum(maximum_wait.values()), 3),
        "ambulance_departed": ambulance_departed,
        "ambulance_arrived": ambulance_arrival_time is not None,
        "ambulance_arrival_time_seconds": ambulance_arrival_time,
        "priority_granted": priority_granted,
        "priority_restored": priority_restored,
        "safety": safety.summary(),
    }
    mode = "priority" if enable_priority else "baseline"
    (OUTPUT_DIR / f"run_summary_{mode}.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    (OUTPUT_DIR / f"event_log_{mode}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="show the Mangilik/Uly Dala pilot in SUMO-GUI")
    parser.add_argument(
        "--disable-priority",
        action="store_true",
        help="run the same demand with the original SUMO traffic-light program",
    )
    args = parser.parse_args()
    print(json.dumps(run_scenario(gui=args.gui, enable_priority=not args.disable_priority), indent=2))
