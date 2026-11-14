from __future__ import annotations

from simulation.sumo.collision_response.run import run_scenario


def test_controlled_scenario_records_a_physical_collision() -> None:
    result = run_scenario()
    assert result["physical_collision_detected"] is True
    assert set(result["colliding_vehicles"]) == {"stalled_vehicle", "unsafe_vehicle"}
    assert float(result["collision_time_seconds"]) > 0
    response_plan = result["response_plan"]
    assert isinstance(response_plan, dict)
    assert response_plan["police_units"] == 1
    # Impact speed alone must not invent an injury or a fire.
    assert response_plan["ambulance_units"] == 0
    assert response_plan["fire_rescue_units"] == 0
    safety = result["safety"]
    assert isinstance(safety, dict)
    assert safety["near_misses"] >= 1
    assert float(safety["minimum_ttc_seconds"]) <= 3
