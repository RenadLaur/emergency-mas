from __future__ import annotations

from simulation.sumo.intelligent_intersection.run import run_scenario


def test_intersection_experiment_grants_priority_and_completes_ambulance_route() -> None:
    result = run_scenario()

    assert result["priority_granted"] is True
    assert result["priority_restored"] is True
    assert result["ambulance_arrived"] is True
    assert result["civilian_vehicles_observed"] >= 30
    assert result["civilians_in_emergency_detection_range"] >= 1
    yielding = result["yielding"]
    assert isinstance(yielding, dict)
    assert yielding["yield_detections"] >= 1
    assert yielding["yield_actions_applied"] >= 1
