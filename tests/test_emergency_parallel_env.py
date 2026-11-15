from __future__ import annotations

import numpy as np
import pytest
from pettingzoo.test import parallel_api_test

from ai.env import (
    DISPATCHER_AGENT,
    ROUTING_AGENT,
    DecisionOutcome,
    DecisionState,
    DecisionTransition,
    EmergencyLogisticsParallelEnv,
    RewardWeights,
    parallel_env,
    sumo_parallel_env,
)


def first_valid_actions(observations: dict[str, dict[str, np.ndarray]]) -> dict[str, int]:
    return {
        DISPATCHER_AGENT: int(np.flatnonzero(observations[DISPATCHER_AGENT]["action_mask"])[0]),
        ROUTING_AGENT: int(np.flatnonzero(observations[ROUTING_AGENT]["action_mask"])[0]),
    }


def test_environment_passes_pettingzoo_parallel_api() -> None:
    parallel_api_test(parallel_env(episode_calls=4), num_cycles=20)


def test_seed_reproduces_observations_rewards_and_global_state() -> None:
    first = parallel_env(episode_calls=3)
    second = parallel_env(episode_calls=3)
    first_observations, _ = first.reset(seed=42)
    second_observations, _ = second.reset(seed=42)

    for agent in first.possible_agents:
        for key in first_observations[agent]:
            np.testing.assert_array_equal(first_observations[agent][key], second_observations[agent][key])
    np.testing.assert_array_equal(first.state(), second.state())

    actions = first_valid_actions(first_observations)
    first_transition = first.step(actions)
    second_transition = second.step(actions)
    assert first_transition[1:] == second_transition[1:]
    for agent in first.agents:
        for key in first_transition[0][agent]:
            np.testing.assert_array_equal(first_transition[0][agent][key], second_transition[0][agent][key])


def test_agents_receive_the_same_decomposed_cooperative_reward() -> None:
    environment = parallel_env(episode_calls=2)
    observations, _ = environment.reset(seed=7)
    _, rewards, terminations, truncations, infos = environment.step(first_valid_actions(observations))

    assert rewards[DISPATCHER_AGENT] == pytest.approx(rewards[ROUTING_AGENT])
    assert infos[DISPATCHER_AGENT]["reward_terms"] == infos[ROUTING_AGENT]["reward_terms"]
    assert set(infos[DISPATCHER_AGENT]["reward_terms"]) == {
        "response_time", "safety_risk", "civilian_delay", "success"
    }
    assert not any(terminations.values())
    assert not any(truncations.values())


def test_invalid_masked_action_receives_a_shared_penalty() -> None:
    environment = parallel_env(episode_calls=2, reward_weights=RewardWeights(invalid_action=9.0))
    observations, _ = environment.reset(seed=1)
    invalid_unit = int(np.flatnonzero(observations[DISPATCHER_AGENT]["action_mask"] == 0)[0])

    actions = {DISPATCHER_AGENT: invalid_unit,
               ROUTING_AGENT: first_valid_actions(observations)[ROUTING_AGENT]}
    _, rewards, _, _, infos = environment.step(actions)

    assert rewards == {DISPATCHER_AGENT: -9.0, ROUTING_AGENT: -9.0}
    assert infos[DISPATCHER_AGENT]["invalid_action"] is True


def test_episode_terminates_after_configured_number_of_calls() -> None:
    environment = parallel_env(episode_calls=2)
    observations, _ = environment.reset(seed=11)
    observations, _, terminations, _, _ = environment.step(first_valid_actions(observations))
    assert environment.agents
    observations, _, terminations, truncations, _ = environment.step(first_valid_actions(observations))

    assert set(observations) == set(environment.possible_agents)
    assert all(terminations.values())
    assert not any(truncations.values())
    assert environment.agents == []


def run_sumo_action(unit: int, route: int) -> dict[str, object]:
    environment = sumo_parallel_env()
    observations, _ = environment.reset(seed=42)
    assert environment.observation_space(DISPATCHER_AGENT).contains(observations[DISPATCHER_AGENT])
    assert environment.observation_space(ROUTING_AGENT).contains(observations[ROUTING_AGENT])
    _, _, terminations, _, infos = environment.step({DISPATCHER_AGENT: unit, ROUTING_AGENT: route})
    assert all(terminations.values())
    environment.close()
    return infos[DISPATCHER_AGENT]


def test_sumo_backend_applies_unit_and_route_actions_reproducibly() -> None:
    near = run_sumo_action(unit=0, route=2)
    far = run_sumo_action(unit=3, route=2)
    replay = run_sumo_action(unit=0, route=2)

    assert near["backend"] == "sumo"
    assert near["selected_unit_id"] == "ambulance_0"
    assert near["selected_route_id"] == "lower"
    assert near["route_edges"][0] == "depot_0_in"
    assert near["success"] is True
    assert far["response_seconds"] > near["response_seconds"]
    for metric in ("response_seconds", "safety_risk", "civilian_delay_seconds",
                   "minimum_ttc_seconds", "physical_collision"):
        assert replay[metric] == pytest.approx(near[metric])


class TwoCallBackend:
    max_units = 2
    max_routes = 2

    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def state(value: float) -> DecisionState:
        return DecisionState(
            call=np.full(5, value, dtype=np.float32),
            units=np.full((2, 5), value, dtype=np.float32),
            routes=np.full((2, 6), value, dtype=np.float32),
            driver=np.full(4, value, dtype=np.float32),
            unit_mask=np.ones(2, dtype=np.int8),
            route_mask=np.ones(2, dtype=np.int8),
        )

    def reset(self, seed: int | None) -> DecisionState:
        self.calls = 0
        return self.state(0.1)

    def evaluate(self, unit_action: int, route_action: int) -> DecisionTransition:
        self.calls += 1
        terminated = self.calls == 2
        return DecisionTransition(
            DecisionOutcome(60.0, 0.1, 5.0, True, {"backend": "two_call"}),
            None if terminated else self.state(0.8),
            terminated,
        )

    def close(self) -> None:
        pass


def test_simulator_backend_can_return_multiple_decision_states() -> None:
    environment = EmergencyLogisticsParallelEnv(
        max_units=2,
        max_routes=2,
        episode_calls=2,
        backend=TwoCallBackend(),
    )
    observations, _ = environment.reset(seed=3)

    observations, _, terminations, _, _ = environment.step(first_valid_actions(observations))

    assert not any(terminations.values())
    assert observations[DISPATCHER_AGENT]["call"][0] == pytest.approx(0.8)
    _, _, terminations, _, _ = environment.step(first_valid_actions(observations))
    assert all(terminations.values())
