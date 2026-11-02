"""Run and verify a controlled SUMO blue-light rescue-lane experiment."""
from __future__ import annotations

import json
from pathlib import Path

import sumolib
import traci

from build_network import build_network


SCENARIO_DIR = Path(__file__).resolve().parent
ROUTE_FILE = SCENARIO_DIR / "rescue_lane.rou.xml"
OUTPUT_DIR = SCENARIO_DIR / "output"
AMBULANCE_ID = "ambulance_0"


def run_scenario() -> dict[str, float | int | bool]:
    """Return evidence that SUMO formed a rescue lane in a controlled queue."""
    network = build_network()
    sumo = sumolib.checkBinary("sumo")
    formed_by: set[str] = set()
    departed = False
    arrived_at: float | None = None
    traci.start([sumo, "-n", str(network), "-r", str(ROUTE_FILE), "--seed", "42",
                 "--lateral-resolution", "0.4", "--device.bluelight.reactiondist", "100",
                 "--no-step-log", "true", "--duration-log.disable", "true"])
    try:
        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            departed = departed or AMBULANCE_ID in traci.simulation.getDepartedIDList()
            active = set(traci.vehicle.getIDList())
            if AMBULANCE_ID in active:
                for vehicle_id in active - {AMBULANCE_ID}:
                    try:
                        influenced_by = traci.vehicle.getParameter(vehicle_id, "rescueLane")
                    except traci.TraCIException:
                        influenced_by = ""
                    if AMBULANCE_ID in influenced_by.split():
                        formed_by.add(vehicle_id)
            if AMBULANCE_ID in traci.simulation.getArrivedIDList():
                arrived_at = traci.simulation.getTime()
    finally:
        traci.close()
    result: dict[str, float | int | bool] = {
        "ambulance_departed": departed,
        "ambulance_arrived": arrived_at is not None,
        "ambulance_arrival_time_seconds": arrived_at if arrived_at is not None else -1.0,
        "rescue_lane_formations": len(formed_by),
        "rescue_lane_observed": bool(formed_by),
    }
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(run_scenario(), indent=2))
