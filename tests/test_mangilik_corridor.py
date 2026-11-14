from __future__ import annotations

import inspect
import xml.etree.ElementTree as ET
from pathlib import Path

import simulation.sumo.mangilik_corridor.run as corridor
import simulation.sumo.mangilik_corridor.report as corridor_report
from simulation.sumo.mangilik_corridor.run import inspect_corridor, run_scenario


def test_mangilik_routes_are_long_and_cross_both_grade_separated_areas() -> None:
    geometry = inspect_corridor()

    assert geometry["nonzero_height_points"] > 0
    assert geometry["minimum_height_meters"] == -6.5
    assert geometry["maximum_height_meters"] == 6.5
    for route in geometry["routes"].values():
        assert route["edge_count"] > 50
        assert route["length_meters"] > 4_000
        assert route["contains_syganak_lower_level"] is True
        assert route["contains_sarayshyk_vicinity"] is True


def test_mangilik_corridor_runs_100_civilians_and_ambulance() -> None:
    result = run_scenario()

    assert result["civilian_vehicles_expected"] == 100
    assert result["civilian_vehicles_departed"] == 100
    assert result["civilian_vehicles_arrived"] == 100
    assert result["ambulance_arrived"] is True


def test_priority_call_path_cannot_set_a_fixed_phase_duration() -> None:
    """The controller extends a phase safely and never resets its programme."""
    source = inspect.getsource(corridor.run_scenario)

    assert ".setPhaseDuration(" not in source
    assert ".setProgram(" not in source


def test_green_guard_only_extends_and_never_shortens(monkeypatch) -> None:
    changes: list[tuple[str, float]] = []
    monkeypatch.setattr(corridor.traci.trafficlight, "getNextSwitch", lambda _: 120.0)
    monkeypatch.setattr(
        corridor.traci.trafficlight,
        "setPhaseDuration",
        lambda tls_id, duration: changes.append((tls_id, duration)),
    )

    extended, remaining, required = corridor._extend_green_if_needed("tls_0", 40.0, 10.0, 100.0)

    assert extended is False
    assert remaining == 20.0
    assert required == 6.0
    assert changes == []


def test_green_guard_extends_a_short_phase_to_cover_eta(monkeypatch) -> None:
    changes: list[tuple[str, float]] = []
    monkeypatch.setattr(corridor.traci.trafficlight, "getNextSwitch", lambda _: 103.0)
    monkeypatch.setattr(
        corridor.traci.trafficlight,
        "setPhaseDuration",
        lambda tls_id, duration: changes.append((tls_id, duration)),
    )

    extended, remaining, required = corridor._extend_green_if_needed("tls_0", 40.0, 10.0, 100.0)

    assert extended is True
    assert remaining == 3.0
    assert required == 6.0
    assert changes == [("tls_0", 6.0)]


def test_demand_route_variant_splits_an_odd_count_across_directions(tmp_path: Path) -> None:
    route_file = corridor._demand_route_file(tmp_path, seed=7, civilian_count=101)
    flows = {
        flow.attrib["id"]: int(flow.attrib["number"])
        for flow in ET.parse(route_file).getroot().findall("flow")
    }

    assert flows == {"civilian_northbound": 50, "civilian_southbound": 51}
    assert route_file.parent == tmp_path / "inputs"


def test_batch_matrix_aggregates_paired_results_without_running_sumo(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[int, int, Path]] = []

    def mode_result(seed: int, mode: str, response_time: float) -> dict[str, object]:
        return {
            "seed": seed,
            "mode": mode,
            "civilian_vehicles_expected": 150,
            "ambulance_arrived": True,
            "ambulance_response_time_seconds": response_time,
            "ordinary_transport": {
                "mean_travel_time_seconds": 100.0 + response_time,
                "mean_time_loss_seconds": 20.0 + response_time,
            },
            "safety": {"near_misses": 2, "physical_collisions": 0},
        }

    def fake_comparison(
        *, seed: int, civilian_count: int, output_dir: Path,
        resume: bool = False, progress: bool = False,
    ) -> dict[str, object]:
        assert resume is True
        assert progress is True
        calls.append((seed, civilian_count, output_dir))
        normal = mode_result(seed, "normal", 12.0 + seed)
        assisted = mode_result(seed, "assisted", 10.0 + seed)
        priority = mode_result(seed, "priority", 11.0 + seed)
        yielding = mode_result(seed, "yielding", 12.0 + seed)
        return {
            "normal": normal,
            "priority": priority,
            "yielding": yielding,
            "assisted": assisted,
            "effects_against_normal": {
                "priority": {"ambulance_response_time_delta_seconds": -1.0},
                "yielding": {"ambulance_response_time_delta_seconds": 0.0},
                "assisted": {"ambulance_response_time_delta_seconds": -2.0},
            },
        }

    monkeypatch.setattr(corridor, "run_comparison", fake_comparison)
    matrix = corridor.run_experiment_matrix([8, 7], [150], output_dir=tmp_path)

    assert [(seed, count) for seed, count, _ in calls] == [(7, 150), (8, 150)]
    aggregate = matrix["aggregates_by_civilian_count"]["150"]
    assert aggregate["modes"]["assisted"]["metrics"]["ambulance_response_time_seconds"] == {
        "n": 2,
        "mean": 17.5,
        "sample_std": 0.707,
    }
    assert aggregate["effects_against_normal"]["assisted"]["ambulance_response_time_delta_seconds"] == {
        "n": 2,
        "mean": -2.0,
        "sample_std": 0.0,
    }
    assert (tmp_path / "batch_summary.json").exists()


def test_report_tables_flatten_matrix_metrics_and_effects() -> None:
    matrix = {
        "seeds": [42, 43],
        "civilian_counts": [100],
        "modes": ["normal", "priority"],
        "aggregates_by_civilian_count": {
            "100": {
                "modes": {
                    "normal": {
                        "runs": 2,
                        "ambulance_success_rate": 1.0,
                        "metrics": {
                            "ambulance_response_time_seconds": {
                                "mean": 20.0,
                                "sample_std": 1.0,
                            },
                            "physical_collisions": {"mean": 0.0, "sample_std": 0.0},
                        },
                    },
                    "priority": {
                        "runs": 2,
                        "ambulance_success_rate": 1.0,
                        "metrics": {
                            "ambulance_response_time_seconds": {
                                "mean": 18.0,
                                "sample_std": 0.5,
                            },
                            "physical_collisions": {"mean": 0.0, "sample_std": 0.0},
                        },
                    },
                },
                "effects_against_normal": {
                    "priority": {
                        "ambulance_response_time_delta_seconds": {
                            "mean": -2.0,
                            "sample_std": 0.5,
                        }
                    }
                },
            }
        },
    }

    rows = corridor_report.summary_rows(matrix)
    effects = corridor_report.effect_rows(matrix)

    assert rows[1]["mode"] == "priority"
    assert rows[1]["ambulance_response_time_seconds_mean"] == 18.0
    assert rows[1]["physical_collisions_sample_std"] == 0.0
    assert effects == [
        {
            "civilian_count": 100,
            "mode": "priority",
            "ambulance_response_time_delta_seconds_mean": -2.0,
            "ambulance_response_time_delta_seconds_sample_std": 0.5,
        }
    ]
