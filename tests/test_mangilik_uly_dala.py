from __future__ import annotations

from simulation.sumo.mangilik_uly_dala.run import run_scenario


def test_mangilik_uly_dala_runs_100_civilians_and_priority_ambulance() -> None:
    result = run_scenario()

    assert result["synthetic_demand"] is True
    assert result["civilian_vehicles_expected"] == 100
    assert result["civilian_vehicles_departed"] == 100
    assert result["civilian_vehicles_arrived"] == 100
    assert result["ambulance_departed"] is True
    assert result["ambulance_arrived"] is True
    assert result["priority_granted"] is True
    assert result["priority_restored"] is True


def test_mangilik_priority_is_compared_with_the_same_baseline_demand() -> None:
    baseline = run_scenario(enable_priority=False)
    priority = run_scenario(enable_priority=True)

    assert baseline["priority_enabled"] is False
    assert baseline["priority_granted"] is False
    assert baseline["priority_restored"] is False
    assert priority["priority_enabled"] is True
    assert baseline["civilian_vehicles_departed"] == priority["civilian_vehicles_departed"] == 100
    assert baseline["civilian_vehicles_arrived"] == priority["civilian_vehicles_arrived"] == 100
    assert priority["ambulance_arrival_time_seconds"] < baseline["ambulance_arrival_time_seconds"]
