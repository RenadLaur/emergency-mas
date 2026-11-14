"""Close a road, reroute an ambulance, and record the exact decision."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import sumolib
import traci


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from agents.communication.message_bus import CommunicationBus
from agents.incident.incidents import IncidentManager, RoadClosureIncident  # noqa: E402
from agents.routing.routing import route_avoids_closed_lanes  # noqa: E402
from simulation.sumo.incident_rerouting.build_network import build_network  # noqa: E402


SCENARIO_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCENARIO_DIR / "output"
AMBULANCE_ID = "ambulance_0"
CLOSED_LANE = "upper_out_0"
DESTINATION_EDGE = "finish"
INCIDENT_DETECTION_DELAY_SECONDS = 1
REPORT_TO_DISPATCHER_DELAY_SECONDS = 1
DISPATCHER_TO_VEHICLE_DELAY_SECONDS = 1


def run_scenario(gui: bool = False) -> dict[str, float | int | bool]:
    """Create a hard closure and reroute only after messages reach the ambulance."""
    network = build_network()
    sumo = sumolib.checkBinary("sumo-gui" if gui else "sumo")
    command = [sumo, "-n", str(network), "-r", str(SCENARIO_DIR / "incident_rerouting.rou.xml"),
               "--seed", "42", "--time-to-teleport", "-1", "--no-step-log", "true",
               "--duration-log.disable", "true"]
    if gui:
        command.extend(["--delay", "300", "--start"])
    incidents = IncidentManager([RoadClosureIncident("collision_upper_out", (CLOSED_LANE,), 2)])
    communications = CommunicationBus()
    events: list[dict[str, object]] = []
    rerouted = False
    reroute_avoids_closure = False
    arrival_time: float | None = None
    incident_started_at: float | None = None
    dispatcher_received_at: float | None = None
    vehicle_command_received_at: float | None = None
    incident_detected = False
    traci.start(command)
    try:
        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            time = traci.simulation.getTime()
            for incident in incidents.activate_due(time):
                incident_started_at = time
                for lane_id in incident.lane_ids:
                    traci.lane.setDisallowed(lane_id, ["passenger", "emergency"])
                events.append({"time_seconds": time, "event": "incident_activated",
                               "incident_id": incident.incident_id, "closed_lanes": list(incident.lane_ids)})
            if (incident_started_at is not None and not incident_detected
                    and time >= incident_started_at + INCIDENT_DETECTION_DELAY_SECONDS):
                incident_detected = True
                report = communications.send("incident_report", "roadside_sensor", "dispatcher", time,
                                             REPORT_TO_DISPATCHER_DELAY_SECONDS,
                                             {"incident_id": "collision_upper_out", "closed_lanes": [CLOSED_LANE]})
                events.append({"time_seconds": time, "event": "incident_detected",
                               "source": "roadside_sensor", "message_id": report.message_id,
                               "will_deliver_at_seconds": report.delivered_at_seconds})
            for message in communications.deliver_due(time):
                events.append({"time_seconds": time, "event": "message_received", "message_id": message.message_id,
                               "kind": message.kind, "sender": message.sender, "recipient": message.recipient,
                               "sent_at_seconds": message.sent_at_seconds})
                if message.kind == "incident_report" and message.recipient == "dispatcher":
                    dispatcher_received_at = time
                    command_message = communications.send("reroute_command", "dispatcher", AMBULANCE_ID, time,
                                                          DISPATCHER_TO_VEHICLE_DELAY_SECONDS, message.payload)
                    events.append({"time_seconds": time, "event": "dispatcher_reroute_command_sent",
                                   "message_id": command_message.message_id,
                                   "will_deliver_at_seconds": command_message.delivered_at_seconds})
                elif message.kind == "reroute_command" and message.recipient == AMBULANCE_ID:
                    vehicle_command_received_at = time
                    if AMBULANCE_ID not in traci.vehicle.getIDList():
                        continue
                    old_route = tuple(traci.vehicle.getRoute(AMBULANCE_ID))
                    current_edge = traci.vehicle.getRoadID(AMBULANCE_ID)
                    alternative = traci.simulation.findRoute(current_edge, DESTINATION_EDGE, vType="ambulance")
                    if not alternative.edges:
                        raise RuntimeError("Incident made the destination unreachable")
                    new_route = tuple(alternative.edges)
                    traci.vehicle.setRoute(AMBULANCE_ID, list(new_route))
                    rerouted = True
                    reroute_avoids_closure = route_avoids_closed_lanes(new_route, incidents.closed_lanes())
                    events.append({"time_seconds": time, "event": "emergency_rerouted",
                                   "vehicle_id": AMBULANCE_ID, "old_route": list(old_route),
                                   "new_route": list(new_route),
                                   "avoids_closed_lane": reroute_avoids_closure,
                                   "command_message_id": message.message_id})
            if AMBULANCE_ID in traci.simulation.getArrivedIDList():
                arrival_time = time
                events.append({"time_seconds": time, "event": "ambulance_arrived"})
    finally:
        traci.close()
    result: dict[str, float | int | bool] = {
        "incident_activated": bool(incidents.closed_lanes()),
        "ambulance_rerouted": rerouted,
        "reroute_avoids_closure": reroute_avoids_closure,
        "ambulance_arrived": arrival_time is not None,
        "ambulance_arrival_time_seconds": arrival_time if arrival_time is not None else -1.0,
        "incident_to_dispatcher_seconds": (dispatcher_received_at - incident_started_at)
        if dispatcher_received_at is not None and incident_started_at is not None else -1.0,
        "dispatcher_to_vehicle_seconds": (vehicle_command_received_at - dispatcher_received_at)
        if vehicle_command_received_at is not None and dispatcher_received_at is not None else -1.0,
        "incident_to_reroute_seconds": (vehicle_command_received_at - incident_started_at)
        if vehicle_command_received_at is not None and incident_started_at is not None else -1.0,
    }
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "run_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "event_log.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="show rerouting in SUMO-GUI")
    args = parser.parse_args()
    print(json.dumps(run_scenario(gui=args.gui), indent=2))
