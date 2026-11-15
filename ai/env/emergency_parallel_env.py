"""PettingZoo Parallel environment for cooperative emergency logistics decisions.

The environment validates the MARL interface, action masking, seeded episodes
and reward design.  It can use either its fast synthetic dynamics or a slower
simulator backend through the same decision seam.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from gymnasium import spaces
from pettingzoo import ParallelEnv


DISPATCHER_AGENT = "dispatcher"
ROUTING_AGENT = "routing"


@dataclass(frozen=True)
class RewardWeights:
    """Weights for a cooperative time/safety/traffic objective."""

    response_time: float = 1.0
    safety_risk: float = 3.0
    civilian_delay: float = 0.5
    success: float = 1.0
    invalid_action: float = 5.0

    def __post_init__(self) -> None:
        for field_name, value in self.__dict__.items():
            if value < 0:
                raise ValueError(f"reward weight {field_name} must not be negative")


@dataclass(frozen=True)
class DecisionState:
    """One normalized joint dispatcher/routing decision state."""

    call: np.ndarray
    units: np.ndarray
    routes: np.ndarray
    driver: np.ndarray
    unit_mask: np.ndarray
    route_mask: np.ndarray


@dataclass(frozen=True)
class DecisionOutcome:
    """Measured result of one valid joint action."""

    response_seconds: float
    safety_risk: float
    civilian_delay_seconds: float
    success: bool
    details: dict[str, Any]


@dataclass(frozen=True)
class DecisionTransition:
    """One simulator transition and the next decision, if the episode continues."""

    outcome: DecisionOutcome
    next_state: DecisionState | None
    terminated: bool


class DecisionBackend(Protocol):
    """Adapter seam between PettingZoo decisions and a simulator."""

    max_units: int
    max_routes: int

    def reset(self, seed: int | None) -> DecisionState:
        """Start an episode and return its first normalized decision state."""

    def evaluate(self, unit_action: int, route_action: int) -> DecisionTransition:
        """Apply one valid joint action and return its simulator transition."""

    def close(self) -> None:
        """Release simulator resources."""


class EmergencyLogisticsParallelEnv(ParallelEnv[str, dict[str, np.ndarray], int]):
    """Cooperative two-agent environment with deterministic seeded episodes.

    The dispatcher chooses one candidate emergency unit while the routing agent
    chooses one candidate route. Both receive the same decomposed reward.
    Observations and the centralized ``state()`` are normalized to ``[0, 1]``.
    """

    metadata = {
        "name": "emergency_logistics_parallel_v0",
        "render_modes": ["ansi"],
        "is_parallelizable": True,
    }

    def __init__(
        self,
        *,
        max_units: int = 4,
        max_routes: int = 3,
        episode_calls: int = 8,
        max_cycles: int | None = None,
        reward_weights: RewardWeights | None = None,
        render_mode: str | None = None,
        backend: DecisionBackend | None = None,
    ) -> None:
        if max_units < 2:
            raise ValueError("max_units must be at least 2")
        if max_routes < 2:
            raise ValueError("max_routes must be at least 2")
        if episode_calls < 1:
            raise ValueError("episode_calls must be positive")
        if max_cycles is not None and max_cycles < 1:
            raise ValueError("max_cycles must be positive")
        if render_mode not in {None, "ansi"}:
            raise ValueError("render_mode must be None or 'ansi'")
        if backend is not None and (backend.max_units != max_units or backend.max_routes != max_routes):
            raise ValueError("backend dimensions must match max_units and max_routes")

        self.max_units = max_units
        self.max_routes = max_routes
        self.episode_calls = episode_calls
        self.max_cycles = max_cycles or episode_calls
        self.reward_weights = reward_weights or RewardWeights()
        self.render_mode = render_mode
        self.backend = backend
        self.possible_agents = [DISPATCHER_AGENT, ROUTING_AGENT]
        self.agents: list[str] = []

        call_space = spaces.Box(0.0, 1.0, shape=(5,), dtype=np.float32)
        self.observation_spaces = {
            DISPATCHER_AGENT: spaces.Dict({
                "action_mask": spaces.MultiBinary(max_units),
                "call": call_space,
                "units": spaces.Box(0.0, 1.0, shape=(max_units, 5), dtype=np.float32),
            }),
            ROUTING_AGENT: spaces.Dict({
                "action_mask": spaces.MultiBinary(max_routes),
                "call": call_space,
                "driver": spaces.Box(0.0, 1.0, shape=(4,), dtype=np.float32),
                "routes": spaces.Box(0.0, 1.0, shape=(max_routes, 6), dtype=np.float32),
            }),
        }
        self.action_spaces = {
            DISPATCHER_AGENT: spaces.Discrete(max_units),
            ROUTING_AGENT: spaces.Discrete(max_routes),
        }
        state_size = 5 + max_units * 5 + max_routes * 6 + 4 + max_units + max_routes
        self.state_space = spaces.Box(0.0, 1.0, shape=(state_size,), dtype=np.float32)

        self._rng = np.random.default_rng()
        self._state: DecisionState | None = None
        self._cycle = 0
        self._cumulative_reward = 0.0
        self._last_info: dict[str, Any] = {}

    def observation_space(self, agent: str) -> spaces.Space[dict[str, np.ndarray]]:
        return self.observation_spaces[agent]

    def action_space(self, agent: str) -> spaces.Space[int]:
        return self.action_spaces[agent]

    def reset(
        self,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[dict[str, dict[str, np.ndarray]], dict[str, dict[str, Any]]]:
        del options
        self._rng = np.random.default_rng(seed)
        if seed is not None:
            for index, agent in enumerate(self.possible_agents):
                self.action_spaces[agent].seed(seed + index)
                self.observation_spaces[agent].seed(seed + 100 + index)
        self.agents = self.possible_agents[:]
        self._cycle = 0
        self._cumulative_reward = 0.0
        self._last_info = {}
        self._state = self.backend.reset(seed) if self.backend is not None else self._sample_state()
        self._validate_state(self._state)
        observations = self._observations()
        infos = {agent: {"cycle": self._cycle} for agent in self.agents}
        return observations, infos

    def step(
        self,
        actions: dict[str, int],
    ) -> tuple[
        dict[str, dict[str, np.ndarray]],
        dict[str, float],
        dict[str, bool],
        dict[str, bool],
        dict[str, dict[str, Any]],
    ]:
        if not self.agents or self._state is None:
            raise RuntimeError("step() requires an active episode; call reset()")
        if set(actions) != set(self.agents):
            raise ValueError(f"actions must be provided for {sorted(self.agents)}")

        current_agents = self.agents[:]
        unit_action = int(actions[DISPATCHER_AGENT])
        route_action = int(actions[ROUTING_AGENT])
        self._validate_action_range(DISPATCHER_AGENT, unit_action)
        self._validate_action_range(ROUTING_AGENT, route_action)
        reward, reward_info, next_state, backend_terminated = self._evaluate(
            unit_action, route_action
        )
        self._cumulative_reward += reward
        self._cycle += 1

        completed = backend_terminated or self._cycle >= self.episode_calls
        truncated = self._cycle >= self.max_cycles and not completed
        terminations = {agent: completed for agent in current_agents}
        truncations = {agent: truncated for agent in current_agents}
        rewards = {agent: reward for agent in current_agents}
        infos = {
            agent: {
                **reward_info,
                "cycle": self._cycle,
                "cumulative_reward": self._cumulative_reward,
            }
            for agent in current_agents
        }
        self._last_info = reward_info

        if completed or truncated:
            observations = self._observations()
            self.agents = []
        else:
            if self.backend is not None:
                if next_state is None:
                    raise RuntimeError("continuing simulator backend did not return next_state")
                self._validate_state(next_state)
                self._state = next_state
            else:
                self._state = self._sample_state()
            observations = self._observations()
        return observations, rewards, terminations, truncations, infos

    def state(self) -> np.ndarray:
        if self._state is None:
            raise RuntimeError("state() requires reset()")
        return np.concatenate([
            self._state.call,
            self._state.units.reshape(-1),
            self._state.routes.reshape(-1),
            self._state.driver,
            self._state.unit_mask.astype(np.float32),
            self._state.route_mask.astype(np.float32),
        ]).astype(np.float32, copy=False)

    def render(self) -> str | None:
        if self.render_mode != "ansi":
            return None
        return (
            f"cycle={self._cycle} cumulative_reward={self._cumulative_reward:.3f} "
            f"last={self._last_info}"
        )

    def close(self) -> None:
        if self.backend is not None:
            self.backend.close()
        self.agents = []
        self._state = None

    def _sample_state(self) -> DecisionState:
        # call: urgency, waiting, weather severity, congestion, incident flag
        call = np.array([
            self._rng.uniform(0.2, 1.0),
            min(1.0, self._cycle / max(1, self.episode_calls - 1)),
            self._rng.uniform(0.0, 1.0),
            self._rng.uniform(0.1, 1.0),
            float(self._rng.random() < 0.35),
        ], dtype=np.float32)

        # units: available, ETA, stress, fatigue, required-type match
        units = self._rng.uniform(0.0, 1.0, size=(self.max_units, 5)).astype(np.float32)
        units[:, 0] = (self._rng.random(self.max_units) > 0.2).astype(np.float32)
        units[:, 4] = (self._rng.random(self.max_units) > 0.15).astype(np.float32)
        unit_mask = ((units[:, 0] > 0.5) & (units[:, 4] > 0.5)).astype(np.int8)
        if not unit_mask.any():
            units[0, (0, 4)] = 1.0
            unit_mask[0] = 1

        # routes: travel time, distance, congestion, safety risk, closure, signal delay
        routes = self._rng.uniform(0.0, 1.0, size=(self.max_routes, 6)).astype(np.float32)
        routes[:, 4] = (self._rng.random(self.max_routes) < 0.2).astype(np.float32)
        route_mask = (routes[:, 4] < 0.5).astype(np.int8)
        if not route_mask.any():
            routes[0, 4] = 0.0
            route_mask[0] = 1

        # driver: stress, fatigue, inverse attention, normalized reaction time
        driver = self._rng.uniform(0.0, 1.0, size=4).astype(np.float32)
        return DecisionState(call, units, routes, driver, unit_mask, route_mask)

    def _observations(self) -> dict[str, dict[str, np.ndarray]]:
        assert self._state is not None
        return {
            DISPATCHER_AGENT: {
                "action_mask": self._state.unit_mask.copy(),
                "call": self._state.call.copy(),
                "units": self._state.units.copy(),
            },
            ROUTING_AGENT: {
                "action_mask": self._state.route_mask.copy(),
                "call": self._state.call.copy(),
                "driver": self._state.driver.copy(),
                "routes": self._state.routes.copy(),
            },
        }

    def _evaluate(
        self, unit_action: int, route_action: int
    ) -> tuple[float, dict[str, Any], DecisionState | None, bool]:
        assert self._state is not None
        unit_valid = bool(self._state.unit_mask[unit_action])
        route_valid = bool(self._state.route_mask[route_action])
        if not unit_valid or not route_valid:
            penalty = -self.reward_weights.invalid_action
            return penalty, {
                "success": False,
                "invalid_action": True,
                "selected_unit": unit_action,
                "selected_route": route_action,
                "response_seconds": None,
                "safety_risk": None,
                "civilian_delay_seconds": None,
                "reward_terms": {"invalid_action": penalty},
            }, self._state if self.backend is not None else None, False

        unit = self._state.units[unit_action]
        route = self._state.routes[route_action]
        if self.backend is not None:
            transition = self.backend.evaluate(unit_action, route_action)
            outcome = transition.outcome
            response_seconds = outcome.response_seconds
            safety_risk = outcome.safety_risk
            civilian_delay_seconds = outcome.civilian_delay_seconds
            success = outcome.success
            details = outcome.details
            next_state = transition.next_state
            backend_terminated = transition.terminated
        else:
            response_seconds = float(60.0 + unit[1] * 540.0 + route[0] * 900.0 + route[5] * 120.0)
            safety_risk = float(np.clip(
                0.45 * route[3]
                + 0.20 * self._state.driver[0]
                + 0.15 * self._state.driver[1]
                + 0.10 * self._state.driver[2]
                + 0.10 * self._state.call[2],
                0.0,
                1.0,
            ))
            civilian_delay_seconds = float(route[1] * route[2] * 600.0)
            success = safety_risk < 0.85
            details = {"backend": "synthetic"}
            next_state = None
            backend_terminated = False

        terms = {
            "response_time": -self.reward_weights.response_time * response_seconds / 1_620.0,
            "safety_risk": -self.reward_weights.safety_risk * safety_risk,
            "civilian_delay": -self.reward_weights.civilian_delay * civilian_delay_seconds / 600.0,
            "success": self.reward_weights.success * (1.0 if success else -1.0),
        }
        reward = float(sum(terms.values()))
        return reward, {
            "success": success,
            "invalid_action": False,
            "selected_unit": unit_action,
            "selected_route": route_action,
            "response_seconds": response_seconds,
            "safety_risk": safety_risk,
            "civilian_delay_seconds": civilian_delay_seconds,
            "reward_terms": terms,
            **details,
        }, next_state, backend_terminated

    def _validate_action_range(self, agent: str, action: int) -> None:
        if not self.action_spaces[agent].contains(action):
            raise ValueError(f"action {action} is outside {agent} action space")

    def _validate_state(self, state: DecisionState) -> None:
        expected = {
            "call": (5,),
            "units": (self.max_units, 5),
            "routes": (self.max_routes, 6),
            "driver": (4,),
            "unit_mask": (self.max_units,),
            "route_mask": (self.max_routes,),
        }
        for field_name, shape in expected.items():
            value = getattr(state, field_name)
            if value.shape != shape:
                raise ValueError(f"backend state {field_name} has shape {value.shape}, expected {shape}")
        if not state.unit_mask.any() or not state.route_mask.any():
            raise ValueError("backend state must expose at least one valid unit and route")


def parallel_env(**kwargs: Any) -> EmergencyLogisticsParallelEnv:
    """Create the project Parallel environment without global registration."""
    return EmergencyLogisticsParallelEnv(**kwargs)
