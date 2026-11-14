"""Prove physical emergency-vehicle collision recovery and reassignment."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import sumolib
import traci


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from agents.dispatcher.dispatcher import EmergencyCall, EmergencyUnit, NearestFreeDispatcher  # noqa: E402
from agents.incident.safety import SafetyMonitor  # noqa: E402
from simulation.sumo.collision_response.build_network import build_network  # noqa: E402


SCENARIO_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCENARIO_DIR / "output"
CALL_ID = "collision_call"
STALLED_VEHICLE = "stalled_vehicle"
COLLISION_SPEED_MPS = 25.0


def run_scenario(gui: bool = False, gui_delay_ms: int = 500) -> dict[str, object]:
    """Run first ambulance -> collision -> unavailable -> second ambulance."""
    network = build_network()
    OUTPUT_DIR.mkdir(exist_ok=True)
    sumo = sumolib.checkBinary("sumo-gui" if gui else "sumo")
    command = [
        sumo, "-n", str(network), "-r", str(SCENARIO_DIR / "dispatcher_collision_reassign.rou.xml"),
        "--seed", "42", "--step-length", "0.1", "--collision.action", "warn",
        "--collision.stoptime", "60", "--collision.mingap-factor", "0", "--collision.check-junctions",
        "--time-to-teleport", "-1", "--no-step-log", "true", "--duration-log.disable", "true",
    ]
    if gui:
        command.extend(["--gui-settings-file", str(ROOT / "simulation" / "sumo" / "demo_viewsettings.xml"),
                        "--delay", str(gui_delay_ms), "--start"])

    units = [EmergencyUnit("ambulance_00", "approach"), EmergencyUnit("ambulance_01", "approach")]
    dispatcher = NearestFreeDispatcher(units)
    call = EmergencyCall(CALL_ID, 0.0, "exit")
    dispatcher.create_call(call)
    safety = SafetyMonitor(3.0)
    events: list[dict[str, object]] = []
    vehicle_by_unit: dict[str, str] = {}
    collision_seen = False

    def route_provider(unit: EmergencyUnit, _: EmergencyCall) -> tuple[tuple[str, ...], float]:
        # Deterministic tie-break is intentionally avoided: unit 00 receives
        # the first mission so the physical loss and replacement are visible.
        return ("approach", "exit"), 1.0 if unit.unit_id == "ambulance_00" else 2.0

    def dispatch(time_seconds: float) -> str:
        assignment = dispatcher.assign(CALL_ID, route_provider)
        if assignment is None:
            raise RuntimeError("Expected an available ambulance for reassignment")
        vehicle_id = f"{assignment.unit_id}__{assignment.call_id}__attempt{assignment.attempt}"
        route_id = f"route__{vehicle_id}"
        traci.route.add(route_id, list(assignment.path))
        traci.vehicle.add(vehicle_id, route_id, typeID="ambulance", depart="now", departLane="best", departSpeed="max")
        vehicle_by_unit[assignment.unit_id] = vehicle_id
        events.append({"time_seconds": time_seconds, "event": "dispatcher_assigned", "unit_id": assignment.unit_id,
                       "vehicle_id": vehicle_id, "attempt": assignment.attempt})
        return vehicle_id

    traci.start(command)
    try:
        first_vehicle = dispatch(0.0)
        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            time = traci.simulation.getTime()
            active = set(traci.vehicle.getIDList())
            if first_vehicle in active and not collision_seen:
                traci.vehicle.setSpeedMode(first_vehicle, 0)
                traci.vehicle.setSpeed(first_vehicle, COLLISION_SPEED_MPS)
                leader = traci.vehicle.getLeader(first_vehicle, 0.0)
                if leader is not None:
                    leader_id, gap_meters = leader
                    if leader_id in active:
                        safety.observe(first_vehicle, leader_id, gap_meters,
                                       traci.vehicle.getSpeed(first_vehicle), traci.vehicle.getSpeed(leader_id))
            colliding = set(traci.simulation.getCollidingVehiclesIDList())
            if not collision_seen and {first_vehicle, STALLED_VEHICLE} <= colliding:
                collision_seen = True
                events.append({"time_seconds": time, "event": "physical_collision",
                               "vehicle_ids": sorted(colliding)})
                traci.vehicle.remove(first_vehicle)
                traci.vehicle.remove(STALLED_VEHICLE)
                affected = dispatcher.mark_unavailable("ambulance_00")
                events.append({"time_seconds": time, "event": "emergency_vehicle_unavailable",
                               "unit_id": "ambulance_00", "vehicle_id": first_vehicle,
                               "reason": "physical_collision"})
                if affected is None:
                    raise RuntimeError("Collision did not affect the assigned call")
                dispatch(time)
            for vehicle_id in traci.simulation.getArrivedIDList():
                if vehicle_id == vehicle_by_unit.get("ambulance_01"):
                    completed = dispatcher.complete("ambulance_01", time)
                    events.append({"time_seconds": time, "event": "call_completed", "call_id": completed.call_id,
                                   "unit_id": "ambulance_01", "vehicle_id": vehicle_id})
                    break
            if dispatcher.calls[CALL_ID].completed_time_seconds is not None:
                break
    finally:
        traci.close()

    result: dict[str, object] = {
        "physical_collision_detected": collision_seen,
        "first_vehicle_id": vehicle_by_unit.get("ambulance_00", ""),
        "replacement_vehicle_id": vehicle_by_unit.get("ambulance_01", ""),
        "call_status": dispatcher.calls[CALL_ID].status.value,
        "attempts": len(dispatcher.calls[CALL_ID].assignments),
        "completed_time_seconds": dispatcher.calls[CALL_ID].completed_time_seconds,
        "safety": safety.summary(),
        "events": events,
    }
    (OUTPUT_DIR / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="show the controlled recovery in SUMO-GUI")
    parser.add_argument("--delay-ms", type=int, default=500, help="SUMO-GUI delay between steps")
    args = parser.parse_args()
    print(json.dumps(run_scenario(gui=args.gui, gui_delay_ms=args.delay_ms), indent=2))
