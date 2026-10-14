"""Integration test for the first deterministic SUMO scenario."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = PROJECT_ROOT / "simulation" / "sumo" / "basic"
sys.path.insert(0, str(SCENARIO_DIR))

from run import run_scenario  # noqa: E402


def test_ambulance_completes_basic_route() -> None:
    result = run_scenario()

    assert result["ambulance_departed"] is True
    assert result["ambulance_arrived"] is True
    assert 20.0 <= float(result["ambulance_arrival_time_seconds"]) < 180.0
