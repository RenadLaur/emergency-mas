"""Generate one physical crash and hand its observable consequences to triage."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import sumolib
import traci


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from agents.incident.triage import IncidentAssessment, ResponsePlanner  # noqa: E402
from agents.incident.safety import SafetyMonitor  # noqa: E402
from simulation.sumo.collision_response.build_network import build_network  # noqa: E402


SCENARIO_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCENARIO_DIR / "output"
UNSAFE_VEHICLE = "unsafe_vehicle"
COLLISION_SPEED_MPS = 25.0


def run_scenario(gui: bool = False) -> dict[str, object]:
    """Run a deterministic crash; consequences remain scenario inputs, not diagnoses."""
    network = build_network()
    OUTPUT_DIR.mkdir(exist_ok=True)
    sumo = sumolib.checkBinary("sumo-gui" if gui else "sumo")
    command = [
        sumo, "-n", str(network), "-r", str(SCENARIO_DIR / "collision_response.rou.xml"),
        "--seed", "42", "--step-length", "0.1", "--collision.action", "warn",
        "--collision.stoptime", "60", "--collision.mingap-factor", "0",
        "--collision-output", str(OUTPUT_DIR / "collisions.xml"), "--time-to-teleport", "-1",
        "--no-step-log", "true", "--duration-log.disable", "true",
    ]
    if gui:
        command.extend([
            "--gui-settings-file", str(ROOT / "simulation" / "sumo" / "demo_viewsettings.xml"),
            "--delay", "100", "--start",
        ])

    collision_time: float | None = None
    collision_vehicles: list[str] = []
    safety = SafetyMonitor(ttc_threshold_seconds=3.0)
    traci.start(command)
    try:
        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            if UNSAFE_VEHICLE in traci.vehicle.getIDList() and collision_time is None:
                # Deliberate scenario fault: needed solely for a reproducible
                # physical collision test, never enabled in normal traffic.
                traci.vehicle.setSpeedMode(UNSAFE_VEHICLE, 0)
                traci.vehicle.setSpeed(UNSAFE_VEHICLE, COLLISION_SPEED_MPS)
                leader = traci.vehicle.getLeader(UNSAFE_VEHICLE, 0.0)
                if leader is None:
                    safety.observe(UNSAFE_VEHICLE, None, None, traci.vehicle.getSpeed(UNSAFE_VEHICLE), 0.0)
                else:
                    leader_id, gap_meters = leader
                    if leader_id in traci.vehicle.getIDList():
                        safety.observe(UNSAFE_VEHICLE, leader_id, gap_meters,
                                       traci.vehicle.getSpeed(UNSAFE_VEHICLE), traci.vehicle.getSpeed(leader_id))
            colliding = traci.simulation.getCollidingVehiclesIDList()
            if colliding:
                collision_time = traci.simulation.getTime()
                collision_vehicles = sorted(colliding)
                break
    finally:
        traci.close()

    assessment = IncidentAssessment(
        incident_id="rear_end_controlled",
        impact_speed_mps=COLLISION_SPEED_MPS,
        blocked_lanes=1 if collision_time is not None else 0,
    )
    plan = ResponsePlanner().plan(assessment)
    result: dict[str, object] = {
        "physical_collision_detected": collision_time is not None,
        "collision_time_seconds": collision_time if collision_time is not None else -1.0,
        "colliding_vehicles": collision_vehicles,
        "impact_speed_mps": COLLISION_SPEED_MPS,
        "safety": safety.summary(),
        "response_plan": {
            "incident_class": plan.incident_class,
            "police_units": plan.police_units,
            "ambulance_units": plan.ambulance_units,
            "fire_rescue_units": plan.fire_rescue_units,
            "reasons": plan.reasons,
        },
    }
    (OUTPUT_DIR / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="show the controlled crash in SUMO-GUI")
    args = parser.parse_args()
    print(json.dumps(run_scenario(gui=args.gui), indent=2))
