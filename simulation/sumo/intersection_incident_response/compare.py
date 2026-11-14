"""Compare the same seeded incident with and without a coordinated corridor."""
from __future__ import annotations

import json
from pathlib import Path

from simulation.sumo.intersection_incident_response.run import run_scenario


OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def run_comparison() -> dict[str, object]:
    """Return an auditable A/B result; both runs use the same seed and demand."""
    without_corridor = run_scenario(use_emergency_corridor=False)
    with_corridor = run_scenario(use_emergency_corridor=True)
    baseline_arrival = float(without_corridor["transit_ambulance_arrival_time_seconds"])
    corridor_arrival = float(with_corridor["transit_ambulance_arrival_time_seconds"])
    baseline_safety = without_corridor["safety"]
    corridor_safety = with_corridor["safety"]
    assert isinstance(baseline_safety, dict) and isinstance(corridor_safety, dict)
    result: dict[str, object] = {
        "without_corridor": without_corridor,
        "with_corridor": with_corridor,
        "arrival_time_improvement_seconds": round(baseline_arrival - corridor_arrival, 3),
        "near_miss_reduction": int(baseline_safety["near_misses"]) - int(corridor_safety["near_misses"]),
        "corridor_does_not_increase_near_misses": (
            int(corridor_safety["near_misses"]) <= int(baseline_safety["near_misses"])
        ),
    }
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(run_comparison(), indent=2))
