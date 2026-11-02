"""Show a safe all-red-to-green traffic-light priority sequence in SUMO."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import sumolib
import traci


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from agents.traffic_light.priority import EmergencyPriorityController  # noqa: E402
from simulation.sumo.basic.build_network import build_network  # noqa: E402


SCENARIO_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCENARIO_DIR / "output"
TLS_ID = "center"
AMBULANCE_ID = "ambulance_0"
APPROACH_EDGE = "west_in"
EXIT_EDGE = "east_out"


def westbound_signal_indices() -> list[int]:
    """Resolve signal links from SUMO rather than relying on a hard-coded index."""
    indices: list[int] = []
    for index, alternatives in enumerate(traci.trafficlight.getControlledLinks(TLS_ID)):
        if any(link and link[0].startswith(f"{APPROACH_EDGE}_") for link in alternatives):
            indices.append(index)
    if not indices:
        raise RuntimeError(f"No TLS link from {APPROACH_EDGE} was found")
    return indices


def run_scenario(gui: bool = False) -> dict[str, float | int | bool]:
    """Give ambulance_0 a safe signal priority and return an auditable summary."""
    network = build_network()
    sumo = sumolib.checkBinary("sumo-gui" if gui else "sumo")
    route_file = ROOT / "simulation" / "sumo" / "basic" / "basic.rou.xml"
    command = [sumo, "-n", str(network), "-r", str(route_file), "--seed", "42",
               "--no-step-log", "true", "--duration-log.disable", "true"]
    if gui:
        command.extend(["--delay", "250", "--start"])
    controller = EmergencyPriorityController(clearance_seconds=3)
    events: list[dict[str, object]] = []
    priority_granted = False
    restored = False
    arrival_time: float | None = None
    traci.start(command)
    try:
        original_program = traci.trafficlight.getProgram(TLS_ID)
        signal_indices = westbound_signal_indices()
        all_red = "r" * len(traci.trafficlight.getRedYellowGreenState(TLS_ID))
        priority_state = "".join("G" if index in signal_indices else "r" for index in range(len(all_red)))
        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            time = traci.simulation.getTime()
            active = set(traci.vehicle.getIDList())
            approaching = False
            passed = AMBULANCE_ID in traci.simulation.getArrivedIDList()
            if AMBULANCE_ID in active:
                road_id = traci.vehicle.getRoadID(AMBULANCE_ID)
                if road_id == APPROACH_EDGE:
                    remaining = traci.lane.getLength(f"{APPROACH_EDGE}_0") - traci.vehicle.getLanePosition(AMBULANCE_ID)
                    # At 40m in this controlled network the normal programme
                    # has already switched the west-east movement to red.
                    approaching = remaining <= 40
                passed = passed or road_id == EXIT_EDGE
            command_to_apply = controller.step(time, approaching, passed)
            if command_to_apply:
                if command_to_apply.action == "set_all_red":
                    before = traci.trafficlight.getRedYellowGreenState(TLS_ID)
                    traci.trafficlight.setRedYellowGreenState(TLS_ID, all_red)
                    events.append({"time_seconds": time, "event": "priority_requested",
                                   "previous_signal_state": before, "all_red_state": all_red})
                elif command_to_apply.action == "grant_priority":
                    traci.trafficlight.setRedYellowGreenState(TLS_ID, priority_state)
                    priority_granted = True
                    events.append({"time_seconds": time, "event": "priority_granted",
                                   "signal_state": priority_state})
                else:
                    traci.trafficlight.setProgram(TLS_ID, original_program)
                    restored = True
                    events.append({"time_seconds": time, "event": "priority_restored",
                                   "program_id": original_program})
            if AMBULANCE_ID in traci.simulation.getArrivedIDList():
                arrival_time = time
                events.append({"time_seconds": time, "event": "ambulance_arrived"})
    finally:
        traci.close()
    result: dict[str, float | int | bool] = {
        "priority_granted": priority_granted,
        "priority_restored": restored,
        "ambulance_arrived": arrival_time is not None,
        "ambulance_arrival_time_seconds": arrival_time if arrival_time is not None else -1.0,
        "clearance_seconds": 3,
    }
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "event_log.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="show the sequence in SUMO-GUI")
    args = parser.parse_args()
    print(json.dumps(run_scenario(gui=args.gui), indent=2))
