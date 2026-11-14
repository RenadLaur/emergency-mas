from __future__ import annotations

from simulation.sumo.dispatcher_collision_reassign.run import run_scenario


def test_physical_collision_unavailable_unit_and_reassignment_form_one_chain() -> None:
    result = run_scenario()

    assert result["physical_collision_detected"] is True
    assert result["first_vehicle_id"] == "ambulance_00__collision_call__attempt1"
    assert result["replacement_vehicle_id"] == "ambulance_01__collision_call__attempt2"
    assert result["call_status"] == "completed"
    assert result["attempts"] == 2
    assert float(result["completed_time_seconds"]) > 0
    safety = result["safety"]
    assert isinstance(safety, dict)
    assert safety["near_misses"] >= 1
