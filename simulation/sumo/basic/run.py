"""Run the basic scenario through TraCI and save a compact outcome summary."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import traci

from build_network import build_network


SCENARIO_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCENARIO_DIR / "basic.sumocfg"
OUTPUT_DIR = SCENARIO_DIR / "output"
AMBULANCE_ID = "ambulance_0"


def run_scenario() -> dict[str, float | int | bool]:
    """Run until the scenario ends and return observable emergency-vehicle events."""
    build_network()
    sumo = shutil.which("sumo")
    if sumo is None:
        raise RuntimeError("sumo is unavailable; activate the emergency-mas environment")

    departed = False
    arrival_time: float | None = None
    steps = 0
    traci.start([sumo, "-c", str(CONFIG_FILE), "--no-step-log", "true", "--duration-log.disable", "true"])
    try:
        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            steps += 1
            departed = departed or AMBULANCE_ID in traci.simulation.getDepartedIDList()
            if AMBULANCE_ID in traci.simulation.getArrivedIDList():
                arrival_time = traci.simulation.getTime()
    finally:
        traci.close()

    result: dict[str, float | int | bool] = {
        "ambulance_departed": departed,
        "ambulance_arrived": arrival_time is not None,
        "ambulance_arrival_time_seconds": arrival_time if arrival_time is not None else -1.0,
        "simulation_steps": steps,
    }
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(run_scenario(), indent=2))
