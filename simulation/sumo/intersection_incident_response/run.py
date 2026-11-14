"""Run a signalized-intersection crash report, dispatch and lane-bypass chain."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import sumolib
import traci


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from agents.communication.message_bus import CommunicationBus  # noqa: E402
from agents.dispatcher.dispatcher import EmergencyCall, EmergencyUnit, NearestFreeDispatcher, UnitType  # noqa: E402
from agents.incident.incidents import IncidentManager, RoadClosureIncident  # noqa: E402
from agents.incident.safety import SafetyMonitor, near_miss_payload  # noqa: E402
from agents.incident.triage import IncidentAssessment, ResponsePlanner  # noqa: E402
from agents.traffic_light.priority import EmergencyPriorityController  # noqa: E402
from simulation.sumo.intersection_incident_response.build_network import build_network  # noqa: E402


SCENARIO_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCENARIO_DIR / "output"
TLS_ID = "center"
TRANSIT_AMBULANCE = "transit_ambulance"
APPROACH_EDGE = "west_in"
EXIT_EDGE = "east_out"
CLOSED_LANE = "west_in_0"
EMERGENCY_CORRIDOR_LANE_INDEX = 2
INCIDENT_ID = "intersection_fire_injury"
INCIDENT_START_SECONDS = 15.0
DETECTION_DELAY_SECONDS = 1.0
REPORT_LATENCY_SECONDS = 2.0
VEHICLE_COMMAND_LATENCY_SECONDS = 1.0


def _westbound_signal_indices() -> list[int]:
    return [
        index
        for index, alternatives in enumerate(traci.trafficlight.getControlledLinks(TLS_ID))
        if any(link and link[0].startswith(f"{APPROACH_EDGE}_") for link in alternatives)
    ]


def _response_summary(plan: object) -> dict[str, object]:
    return {
        "incident_class": plan.incident_class.value,
        "police_units": plan.police_units,
        "ambulance_units": plan.ambulance_units,
        "fire_rescue_units": plan.fire_rescue_units,
        "reasons": list(plan.reasons),
    }


def run_scenario(gui: bool = False, use_emergency_corridor: bool = True) -> dict[str, object]:
    """Model one incident from closure through messages to completed response calls."""
    network = build_network()
    OUTPUT_DIR.mkdir(exist_ok=True)
    sumo = sumolib.checkBinary("sumo-gui" if gui else "sumo")
    command = [
        sumo, "-n", str(network), "-r", str(SCENARIO_DIR / "intersection_incident_response.rou.xml"),
        "--seed", "42", "--step-length", "0.1", "--time-to-teleport", "-1",
        "--collision.action", "warn", "--collision.check-junctions", "--no-step-log", "true",
        "--duration-log.disable", "true",
    ]
    if gui:
        command.extend(["--gui-settings-file", str(ROOT / "simulation" / "sumo" / "demo_viewsettings.xml"),
                        "--delay", "250", "--start"])

    incident_manager = IncidentManager([RoadClosureIncident(INCIDENT_ID, (CLOSED_LANE,), INCIDENT_START_SECONDS)])
    communications = CommunicationBus()
    assessment = IncidentAssessment(
        incident_id=INCIDENT_ID,
        estimated_injured=1,
        fire_or_smoke=True,
        blocked_lanes=1,
    )
    dispatcher = NearestFreeDispatcher([
        EmergencyUnit("police_00", "east_in", unit_type=UnitType.POLICE),
        EmergencyUnit("ambulance_00", "north_in", unit_type=UnitType.AMBULANCE),
        EmergencyUnit("fire_rescue_00", "south_in", unit_type=UnitType.FIRE_RESCUE),
    ])
    route_by_unit = {
        "police_00": ("east_in", "west_out"),
        "ambulance_00": ("north_in", "south_out"),
        "fire_rescue_00": ("south_in", "north_out"),
    }
    priority = EmergencyPriorityController(clearance_seconds=3)
    safety = SafetyMonitor(3.0)
    events: list[dict[str, object]] = []
    vehicle_to_unit: dict[str, str] = {}
    incident_started_at: float | None = None
    dispatcher_received_at: float | None = None
    transit_command_received_at: float | None = None
    report_sent = False
    plan_summary: dict[str, object] | None = None
    transit_lane_bypass_applied = False
    transit_arrival_time: float | None = None
    priority_granted = False
    priority_restored = False

    def dispatch_response(unit_type: UnitType, destination_edge: str, time_seconds: float) -> None:
        call_id = f"{INCIDENT_ID}__{unit_type.value}_00"
        dispatcher.create_call(EmergencyCall(call_id, time_seconds, destination_edge, required_unit_type=unit_type))

        def route_provider(unit: EmergencyUnit, _: EmergencyCall) -> tuple[tuple[str, ...], float] | None:
            route = route_by_unit.get(unit.unit_id)
            return (route, float(len(route))) if route else None

        assignment = dispatcher.assign(call_id, route_provider)
        if assignment is None:
            events.append({"time_seconds": time_seconds, "event": "dispatcher_no_unit", "call_id": call_id,
                           "unit_type": unit_type.value})
            return
        vehicle_id = f"{assignment.unit_id}__{assignment.call_id}"
        route_id = f"route__{vehicle_id}"
        traci.route.add(route_id, list(assignment.path))
        traci.vehicle.add(vehicle_id, route_id, typeID=assignment.unit_type.value, depart="now",
                          departLane="best", departSpeed="max")
        vehicle_to_unit[vehicle_id] = assignment.unit_id
        events.append({"time_seconds": time_seconds, "event": "dispatcher_assigned", "call_id": call_id,
                       "unit_id": assignment.unit_id, "unit_type": assignment.unit_type.value,
                       "vehicle_id": vehicle_id})

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

            for incident in incident_manager.activate_due(time):
                incident_started_at = time
                # Civilian access is closed. The transit ambulance remains on
                # the incident corridor until its delayed command arrives.
                traci.lane.setDisallowed(CLOSED_LANE, ["passenger"])
                events.append({"time_seconds": time, "event": "incident_activated", "incident_id": incident.incident_id,
                               "closed_lanes": list(incident.lane_ids)})
            if incident_started_at is not None and not report_sent and time >= incident_started_at + DETECTION_DELAY_SECONDS:
                report_sent = True
                message = communications.send("incident_report", "roadside_sensor", "dispatcher", time,
                                              REPORT_LATENCY_SECONDS, {"incident_id": INCIDENT_ID})
                events.append({"time_seconds": time, "event": "incident_report_sent", "message_id": message.message_id,
                               "will_deliver_at_seconds": message.delivered_at_seconds})

            for message in communications.deliver_due(time):
                events.append({"time_seconds": time, "event": "message_received", "message_id": message.message_id,
                               "kind": message.kind, "sender": message.sender, "recipient": message.recipient})
                if message.kind == "incident_report" and message.recipient == "dispatcher":
                    dispatcher_received_at = time
                    plan = ResponsePlanner().plan(assessment)
                    plan_summary = _response_summary(plan)
                    events.append({"time_seconds": time, "event": "dispatcher_response_plan", **plan_summary})
                    for unit_type, count, destination in (
                        (UnitType.POLICE, plan.police_units, "west_out"),
                        (UnitType.AMBULANCE, plan.ambulance_units, "south_out"),
                        (UnitType.FIRE_RESCUE, plan.fire_rescue_units, "north_out"),
                    ):
                        for _ in range(count):
                            dispatch_response(unit_type, destination, time)
                    if use_emergency_corridor:
                        vehicle_command = communications.send("lane_bypass_command", "dispatcher", TRANSIT_AMBULANCE,
                                                              time, VEHICLE_COMMAND_LATENCY_SECONDS,
                                                              {"closed_lane": CLOSED_LANE,
                                                               "target_lane": EMERGENCY_CORRIDOR_LANE_INDEX})
                        events.append({"time_seconds": time, "event": "dispatcher_lane_bypass_command_sent",
                                       "message_id": vehicle_command.message_id,
                                       "will_deliver_at_seconds": vehicle_command.delivered_at_seconds})
                elif message.kind == "lane_bypass_command" and message.recipient == TRANSIT_AMBULANCE:
                    transit_command_received_at = time
                    if TRANSIT_AMBULANCE in active and traci.vehicle.getRoadID(TRANSIT_AMBULANCE) == APPROACH_EDGE:
                        try:
                            traci.vehicle.changeLane(TRANSIT_AMBULANCE, int(message.payload["target_lane"]), 8.0)
                            transit_lane_bypass_applied = True
                        except traci.TraCIException:
                            transit_lane_bypass_applied = False
                    events.append({"time_seconds": time,
                                   "event": "transit_ambulance_lane_bypass_applied" if transit_lane_bypass_applied
                                   else "transit_ambulance_lane_bypass_unavailable"})

            approaching = False
            passed = TRANSIT_AMBULANCE in traci.simulation.getArrivedIDList()
            if TRANSIT_AMBULANCE in active:
                road_id = traci.vehicle.getRoadID(TRANSIT_AMBULANCE)
                if road_id == APPROACH_EDGE:
                    remaining = traci.lane.getLength(CLOSED_LANE) - traci.vehicle.getLanePosition(TRANSIT_AMBULANCE)
                    approaching = remaining <= 55
                    leader = traci.vehicle.getLeader(TRANSIT_AMBULANCE, 0.0)
                    if leader is not None:
                        leader_id, gap_meters = leader
                        leader_speed = traci.vehicle.getSpeed(leader_id)
                        near_miss = safety.observe(TRANSIT_AMBULANCE, leader_id, gap_meters,
                                                    traci.vehicle.getSpeed(TRANSIT_AMBULANCE), leader_speed)
                        if near_miss:
                            events.append({"time_seconds": time, "event": "near_miss", **near_miss_payload(near_miss)})
                passed = passed or road_id == EXIT_EDGE
            priority_command = priority.step(time, approaching, passed)
            if priority_command:
                if priority_command.action == "set_all_red":
                    traci.trafficlight.setRedYellowGreenState(TLS_ID, all_red)
                    events.append({"time_seconds": time, "event": "priority_requested"})
                elif priority_command.action == "grant_priority":
                    traci.trafficlight.setRedYellowGreenState(TLS_ID, priority_state)
                    priority_granted = True
                    events.append({"time_seconds": time, "event": "priority_granted"})
                else:
                    traci.trafficlight.setProgram(TLS_ID, original_program)
                    priority_restored = True
                    events.append({"time_seconds": time, "event": "priority_restored"})

            for vehicle_id in traci.simulation.getArrivedIDList():
                if vehicle_id == TRANSIT_AMBULANCE:
                    transit_arrival_time = time
                    events.append({"time_seconds": time, "event": "transit_ambulance_arrived"})
                unit_id = vehicle_to_unit.get(vehicle_id)
                if unit_id:
                    completed = dispatcher.complete(unit_id, time)
                    events.append({"time_seconds": time, "event": "response_call_completed", "call_id": completed.call_id,
                                   "unit_id": unit_id, "vehicle_id": vehicle_id})
    finally:
        traci.close()

    completed_calls = sum(call.status.value == "completed" for call in dispatcher.calls.values())
    result: dict[str, object] = {
        "incident_activated": bool(incident_manager.closed_lanes()),
        "incident_to_dispatcher_seconds": round(dispatcher_received_at - incident_started_at, 3)
        if dispatcher_received_at is not None and incident_started_at is not None else None,
        "dispatcher_to_transit_ambulance_seconds": round(transit_command_received_at - dispatcher_received_at, 3)
        if transit_command_received_at is not None and dispatcher_received_at is not None else None,
        "response_plan": plan_summary,
        "response_calls_dispatched": len(dispatcher.calls),
        "response_calls_completed": completed_calls,
        "emergency_bypass_corridor_enabled": use_emergency_corridor,
        "emergency_bypass_corridor_lane": EMERGENCY_CORRIDOR_LANE_INDEX,
        "transit_ambulance_lane_bypass_applied": transit_lane_bypass_applied,
        "transit_ambulance_arrived": transit_arrival_time is not None,
        "transit_ambulance_arrival_time_seconds": transit_arrival_time,
        "priority_granted": priority_granted,
        "priority_restored": priority_restored,
        "safety": safety.summary(),
    }
    (OUTPUT_DIR / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "event_log.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    mode = "corridor" if use_emergency_corridor else "no_corridor"
    (OUTPUT_DIR / f"run_summary_{mode}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (OUTPUT_DIR / f"event_log_{mode}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="show the incident-response chain in SUMO-GUI")
    parser.add_argument("--without-corridor", action="store_true", help="keep the ambulance in ordinary traffic")
    args = parser.parse_args()
    print(json.dumps(run_scenario(gui=args.gui, use_emergency_corridor=not args.without_corridor), indent=2))
