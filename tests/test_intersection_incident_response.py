from __future__ import annotations

from simulation.sumo.intersection_incident_response.run import run_scenario
from simulation.sumo.intersection_incident_response.compare import run_comparison


def test_intersection_incident_reaches_dispatcher_and_deploys_typed_response() -> None:
    result = run_scenario()

    assert result["incident_activated"] is True
    assert result["incident_to_dispatcher_seconds"] == 3.0
    assert result["dispatcher_to_transit_ambulance_seconds"] == 1.0
    plan = result["response_plan"]
    assert isinstance(plan, dict)
    assert plan["police_units"] == 1
    assert plan["ambulance_units"] == 1
    assert plan["fire_rescue_units"] == 1
    assert result["response_calls_dispatched"] == 3
    assert result["response_calls_completed"] == 3
    assert result["emergency_bypass_corridor_enabled"] is True
    assert result["emergency_bypass_corridor_lane"] == 2
    assert result["transit_ambulance_lane_bypass_applied"] is True
    assert result["transit_ambulance_arrived"] is True
    assert result["priority_granted"] is True
    assert result["priority_restored"] is True
    safety = result["safety"]
    assert isinstance(safety, dict)
    assert safety["near_misses"] >= 0


def test_coordinated_corridor_improves_arrival_and_near_miss_metrics() -> None:
    comparison = run_comparison()

    assert comparison["arrival_time_improvement_seconds"] > 0
    assert comparison["near_miss_reduction"] >= 0
    assert comparison["corridor_does_not_increase_near_misses"] is True
