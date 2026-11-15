from __future__ import annotations

from typing import Any

import numpy as np

from ai.env import (
    DISPATCHER_AGENT,
    ROUTING_AGENT,
    DecisionOutcome,
    DecisionState,
    DecisionTransition,
    EmergencyLogisticsParallelEnv,
)
from experiments.rl_policy_matrix import run_matrix, select_actions


def observations() -> dict[str, dict[str, np.ndarray]]:
    return {
        DISPATCHER_AGENT: {
            "action_mask": np.array([1, 0, 1, 1], dtype=np.int8),
            "call": np.zeros(5, dtype=np.float32),
            "units": np.array([
                [1.0, 0.7, 0.0, 0.0, 1.0],
                [0.0, 0.1, 0.0, 0.0, 1.0],
                [1.0, 0.2, 0.0, 0.0, 1.0],
                [1.0, 0.5, 0.0, 0.0, 1.0],
            ], dtype=np.float32),
        },
        ROUTING_AGENT: {
            "action_mask": np.ones(3, dtype=np.int8),
            "call": np.zeros(5, dtype=np.float32),
            "driver": np.zeros(4, dtype=np.float32),
            "routes": np.array([
                [0.8, 0.0, 0.0, 0.0, 0.0, 0.0],
                [0.3, 0.0, 0.0, 0.0, 0.0, 0.0],
                [0.5, 0.0, 0.0, 0.0, 0.0, 0.0],
            ], dtype=np.float32),
        },
    }


def test_named_baselines_choose_nearest_unit_and_expected_route() -> None:
    rng = np.random.default_rng(1)

    nearest = select_actions("nearest", observations(), rng=rng)
    astar = select_actions("astar", observations(), rng=rng)

    assert nearest == {DISPATCHER_AGENT: 2, ROUTING_AGENT: 1}
    assert astar == {DISPATCHER_AGENT: 2, ROUTING_AGENT: 1}


class MatrixBackend:
    max_units = 4
    max_routes = 3

    def __init__(self) -> None:
        self.seed = 0
        self.call = 0

    def state(self) -> DecisionState:
        return DecisionState(
            call=np.full(5, 0.5, dtype=np.float32),
            units=np.array([
                [1.0, 0.4, 0.0, 0.0, 1.0],
                [1.0, 0.2, 0.0, 0.0, 1.0],
                [1.0, 0.8, 0.0, 0.0, 1.0],
                [1.0, 0.6, 0.0, 0.0, 1.0],
            ], dtype=np.float32),
            routes=np.full((3, 6), 0.2, dtype=np.float32),
            driver=np.zeros(4, dtype=np.float32),
            unit_mask=np.ones(4, dtype=np.int8),
            route_mask=np.ones(3, dtype=np.int8),
        )

    def reset(self, seed: int | None) -> DecisionState:
        self.seed = int(seed or 0)
        self.call = 0
        return self.state()

    def evaluate(self, unit_action: int, route_action: int) -> DecisionTransition:
        call_index = self.call
        self.call += 1
        terminated = self.call == 2
        details: dict[str, Any] = {
            "call_id": f"call_{call_index}",
            "destination_edge_id": f"edge_{self.seed}_{call_index}",
            "selected_unit_id": f"ambulance_{unit_action}",
            "selected_route_id": ("dijkstra", "astar", "traffic_aware")[route_action],
            "minimum_ttc_seconds": 4.0,
            "near_misses": 0,
            "physical_collision": False,
            "route_edges": 5,
        }
        return DecisionTransition(
            DecisionOutcome(100.0 + route_action, 0.0, 2.0, True, details),
            None if terminated else self.state(),
            terminated,
        )

    def close(self) -> None:
        pass


def test_matrix_verifies_identical_seeded_call_destinations() -> None:
    def factory() -> EmergencyLogisticsParallelEnv:
        return EmergencyLogisticsParallelEnv(
            max_units=4,
            max_routes=3,
            episode_calls=2,
            backend=MatrixBackend(),
        )

    result = run_matrix(
        ["nearest", "dijkstra", "traffic_aware"],
        [7, 8],
        environment_factory=factory,
    )

    assert result["protocol"]["same_calls_verified"] is True
    assert len(result["runs"]) == 6
    for seed in (7, 8):
        signatures = {
            tuple(call["destination_edge_id"] for call in run["calls"])
            for run in result["runs"]
            if run["summary"]["seed"] == seed
        }
        assert len(signatures) == 1
