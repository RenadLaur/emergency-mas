"""Small cooperative PPO trainer for the PettingZoo logistics environment.

This is deliberately dependency-light and transparent.  Two actor heads use
their local PettingZoo observations while one centralized critic consumes the
environment ``state()``.  It is a training-pipeline smoke test, not the final
MAPPO implementation used for diploma conclusions.
"""
from __future__ import annotations

import argparse
import json
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch import nn
from torch.distributions import Categorical

from ai.env import (
    DISPATCHER_AGENT,
    ROUTING_AGENT,
    astana_parallel_env,
    parallel_env,
    sumo_parallel_env,
)


@dataclass(frozen=True)
class PPOConfig:
    """Hyperparameters for a short, reproducible PPO run."""

    seed: int = 42
    updates: int = 20
    rollout_steps: int = 256
    update_epochs: int = 4
    minibatch_size: int = 64
    learning_rate: float = 3e-4
    gamma: float = 0.99
    gae_lambda: float = 0.95
    clip_ratio: float = 0.2
    value_coefficient: float = 0.5
    entropy_coefficient: float = 0.01
    max_grad_norm: float = 0.5
    hidden_size: int = 64

    def __post_init__(self) -> None:
        positive = (
            "updates",
            "rollout_steps",
            "update_epochs",
            "minibatch_size",
            "learning_rate",
            "max_grad_norm",
            "hidden_size",
        )
        for name in positive:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("gamma", "gae_lambda", "clip_ratio"):
            if not 0.0 < getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")


def _mlp(input_size: int, hidden_size: int, output_size: int) -> nn.Sequential:
    return nn.Sequential(
        nn.Linear(input_size, hidden_size),
        nn.Tanh(),
        nn.Linear(hidden_size, hidden_size),
        nn.Tanh(),
        nn.Linear(hidden_size, output_size),
    )


class CooperativePPO(nn.Module):
    """Two local actors and one centralized value function."""

    def __init__(
        self,
        *,
        dispatcher_features: int,
        routing_features: int,
        state_features: int,
        dispatcher_actions: int,
        routing_actions: int,
        hidden_size: int = 64,
    ) -> None:
        super().__init__()
        self.dispatcher_actor = _mlp(dispatcher_features, hidden_size, dispatcher_actions)
        self.routing_actor = _mlp(routing_features, hidden_size, routing_actions)
        self.critic = _mlp(state_features, hidden_size, 1)

    @staticmethod
    def masked_distribution(logits: torch.Tensor, mask: torch.Tensor) -> Categorical:
        """Create a categorical policy that cannot sample invalid actions."""
        if torch.any(mask.sum(dim=-1) < 1):
            raise ValueError("every action mask must allow at least one action")
        masked_logits = logits.masked_fill(~mask.bool(), torch.finfo(logits.dtype).min)
        return Categorical(logits=masked_logits)

    def distributions(
        self,
        dispatcher_observation: torch.Tensor,
        dispatcher_mask: torch.Tensor,
        routing_observation: torch.Tensor,
        routing_mask: torch.Tensor,
    ) -> tuple[Categorical, Categorical]:
        return (
            self.masked_distribution(
                self.dispatcher_actor(dispatcher_observation), dispatcher_mask
            ),
            self.masked_distribution(self.routing_actor(routing_observation), routing_mask),
        )

    def value(self, state: torch.Tensor) -> torch.Tensor:
        return self.critic(state).squeeze(-1)


def deterministic_actions(
    model: CooperativePPO,
    observations: dict[str, dict[str, np.ndarray]],
) -> dict[str, int]:
    """Select masked argmax actions for evaluation and policy comparison."""
    with torch.no_grad():
        dispatcher_distribution, routing_distribution = model.distributions(
            _tensor(_dispatcher_features(observations[DISPATCHER_AGENT])).unsqueeze(0),
            _tensor(
                observations[DISPATCHER_AGENT]["action_mask"], dtype=torch.bool
            ).unsqueeze(0),
            _tensor(_routing_features(observations[ROUTING_AGENT])).unsqueeze(0),
            _tensor(
                observations[ROUTING_AGENT]["action_mask"], dtype=torch.bool
            ).unsqueeze(0),
        )
    return {
        DISPATCHER_AGENT: int(dispatcher_distribution.probs.argmax().item()),
        ROUTING_AGENT: int(routing_distribution.probs.argmax().item()),
    }


def load_policy(
    checkpoint: Path,
    environment: Any,
    *,
    hidden_size: int = 64,
) -> CooperativePPO:
    """Load a policy checkpoint without resetting or starting the environment."""
    model = build_model(environment, hidden_size)
    state_dict = torch.load(checkpoint, map_location="cpu", weights_only=True)
    model.load_state_dict(state_dict)
    model.eval()
    return model


def _dispatcher_features(observation: dict[str, np.ndarray]) -> np.ndarray:
    return np.concatenate((observation["call"], observation["units"].reshape(-1))).astype(
        np.float32, copy=False
    )


def _routing_features(observation: dict[str, np.ndarray]) -> np.ndarray:
    return np.concatenate(
        (
            observation["call"],
            observation["driver"],
            observation["routes"].reshape(-1),
        )
    ).astype(np.float32, copy=False)


def _tensor(array: np.ndarray | list[Any], *, dtype: torch.dtype = torch.float32) -> torch.Tensor:
    return torch.as_tensor(np.asarray(array), dtype=dtype)


def build_model(environment: Any, hidden_size: int) -> CooperativePPO:
    """Build a policy from declared spaces without starting the simulator."""
    dispatcher_space = environment.observation_space(DISPATCHER_AGENT)
    routing_space = environment.observation_space(ROUTING_AGENT)
    model = CooperativePPO(
        dispatcher_features=int(
            np.prod(dispatcher_space["call"].shape)
            + np.prod(dispatcher_space["units"].shape)
        ),
        routing_features=int(
            np.prod(routing_space["call"].shape)
            + np.prod(routing_space["driver"].shape)
            + np.prod(routing_space["routes"].shape)
        ),
        state_features=int(np.prod(environment.state_space.shape)),
        dispatcher_actions=environment.action_space(DISPATCHER_AGENT).n,
        routing_actions=environment.action_space(ROUTING_AGENT).n,
        hidden_size=hidden_size,
    )
    return model


def _collect_rollout(
    model: CooperativePPO,
    environment: Any,
    *,
    steps: int,
    seed_base: int,
    gamma: float,
    gae_lambda: float,
) -> tuple[dict[str, torch.Tensor], dict[str, float]]:
    storage: dict[str, list[Any]] = {
        "dispatcher_observation": [],
        "dispatcher_mask": [],
        "routing_observation": [],
        "routing_mask": [],
        "state": [],
        "dispatcher_action": [],
        "routing_action": [],
        "old_log_probability": [],
        "reward": [],
        "value": [],
        "done": [],
    }
    episode_index = 0
    observations, _ = environment.reset(seed=seed_base)
    episode_reward = 0.0
    completed_rewards: list[float] = []
    successes: list[float] = []

    for step_index in range(steps):
        dispatcher_observation = _dispatcher_features(observations[DISPATCHER_AGENT])
        routing_observation = _routing_features(observations[ROUTING_AGENT])
        dispatcher_mask = observations[DISPATCHER_AGENT]["action_mask"]
        routing_mask = observations[ROUTING_AGENT]["action_mask"]
        state = environment.state()

        with torch.no_grad():
            dispatcher_distribution, routing_distribution = model.distributions(
                _tensor(dispatcher_observation).unsqueeze(0),
                _tensor(dispatcher_mask, dtype=torch.bool).unsqueeze(0),
                _tensor(routing_observation).unsqueeze(0),
                _tensor(routing_mask, dtype=torch.bool).unsqueeze(0),
            )
            dispatcher_action = dispatcher_distribution.sample()
            routing_action = routing_distribution.sample()
            log_probability = (
                dispatcher_distribution.log_prob(dispatcher_action)
                + routing_distribution.log_prob(routing_action)
            )
            value = model.value(_tensor(state).unsqueeze(0))

        actions = {
            DISPATCHER_AGENT: int(dispatcher_action.item()),
            ROUTING_AGENT: int(routing_action.item()),
        }
        next_observations, rewards, terminations, truncations, infos = environment.step(actions)
        reward = float(rewards[DISPATCHER_AGENT])
        successes.append(float(infos[DISPATCHER_AGENT]["success"]))
        done = bool(
            terminations[DISPATCHER_AGENT] or truncations[DISPATCHER_AGENT]
        )

        storage["dispatcher_observation"].append(dispatcher_observation)
        storage["dispatcher_mask"].append(dispatcher_mask)
        storage["routing_observation"].append(routing_observation)
        storage["routing_mask"].append(routing_mask)
        storage["state"].append(state)
        storage["dispatcher_action"].append(actions[DISPATCHER_AGENT])
        storage["routing_action"].append(actions[ROUTING_AGENT])
        storage["old_log_probability"].append(float(log_probability.item()))
        storage["reward"].append(reward)
        storage["value"].append(float(value.item()))
        storage["done"].append(done)

        episode_reward += reward
        if done:
            completed_rewards.append(episode_reward)
            episode_reward = 0.0
            episode_index += 1
            if step_index + 1 < steps:
                observations, _ = environment.reset(seed=seed_base + episode_index)
        else:
            observations = next_observations

    with torch.no_grad():
        bootstrap_value = float(model.value(_tensor(environment.state()).unsqueeze(0)).item())
    rewards_array = np.asarray(storage["reward"], dtype=np.float32)
    values_array = np.asarray(storage["value"], dtype=np.float32)
    dones_array = np.asarray(storage["done"], dtype=np.float32)
    advantages = np.zeros_like(rewards_array)
    gae = 0.0
    next_value = bootstrap_value
    for index in reversed(range(steps)):
        nonterminal = 1.0 - dones_array[index]
        delta = rewards_array[index] + gamma * next_value * nonterminal - values_array[index]
        gae = delta + gamma * gae_lambda * nonterminal * gae
        advantages[index] = gae
        next_value = values_array[index]
    returns = advantages + values_array

    batch = {
        "dispatcher_observation": _tensor(storage["dispatcher_observation"]),
        "dispatcher_mask": _tensor(storage["dispatcher_mask"], dtype=torch.bool),
        "routing_observation": _tensor(storage["routing_observation"]),
        "routing_mask": _tensor(storage["routing_mask"], dtype=torch.bool),
        "state": _tensor(storage["state"]),
        "dispatcher_action": _tensor(storage["dispatcher_action"], dtype=torch.long),
        "routing_action": _tensor(storage["routing_action"], dtype=torch.long),
        "old_log_probability": _tensor(storage["old_log_probability"]),
        "advantage": _tensor(advantages),
        "return": _tensor(returns),
    }
    metrics = {
        "mean_episode_reward": float(np.mean(completed_rewards)) if completed_rewards else 0.0,
        "success_rate": float(np.mean(successes)) if successes else 0.0,
        "completed_episodes": float(len(completed_rewards)),
    }
    return batch, metrics


def _update_model(
    model: CooperativePPO,
    optimizer: torch.optim.Optimizer,
    batch: dict[str, torch.Tensor],
    config: PPOConfig,
) -> dict[str, float]:
    advantages = batch["advantage"]
    advantages = (advantages - advantages.mean()) / (advantages.std(unbiased=False) + 1e-8)
    sample_count = advantages.shape[0]
    losses: list[float] = []
    policy_losses: list[float] = []
    value_losses: list[float] = []

    for _ in range(config.update_epochs):
        indices = torch.randperm(sample_count)
        for start in range(0, sample_count, config.minibatch_size):
            selected = indices[start : start + config.minibatch_size]
            dispatcher_distribution, routing_distribution = model.distributions(
                batch["dispatcher_observation"][selected],
                batch["dispatcher_mask"][selected],
                batch["routing_observation"][selected],
                batch["routing_mask"][selected],
            )
            log_probability = (
                dispatcher_distribution.log_prob(batch["dispatcher_action"][selected])
                + routing_distribution.log_prob(batch["routing_action"][selected])
            )
            ratio = torch.exp(log_probability - batch["old_log_probability"][selected])
            selected_advantage = advantages[selected]
            unclipped = ratio * selected_advantage
            clipped = torch.clamp(
                ratio, 1.0 - config.clip_ratio, 1.0 + config.clip_ratio
            ) * selected_advantage
            policy_loss = -torch.minimum(unclipped, clipped).mean()
            value_loss = nn.functional.mse_loss(
                model.value(batch["state"][selected]), batch["return"][selected]
            )
            entropy = (
                dispatcher_distribution.entropy() + routing_distribution.entropy()
            ).mean()
            loss = (
                policy_loss
                + config.value_coefficient * value_loss
                - config.entropy_coefficient * entropy
            )

            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), config.max_grad_norm)
            optimizer.step()
            losses.append(float(loss.item()))
            policy_losses.append(float(policy_loss.item()))
            value_losses.append(float(value_loss.item()))

    return {
        "loss": float(np.mean(losses)),
        "policy_loss": float(np.mean(policy_losses)),
        "value_loss": float(np.mean(value_losses)),
    }


def train_ppo(
    config: PPOConfig,
    *,
    environment_factory: Callable[[], Any] = parallel_env,
    model: CooperativePPO | None = None,
) -> tuple[CooperativePPO, list[dict[str, float]]]:
    """Train or fine-tune through any compatible environment factory."""
    random.seed(config.seed)
    np.random.seed(config.seed)
    torch.manual_seed(config.seed)
    torch.use_deterministic_algorithms(True)
    environment = environment_factory()
    if model is None:
        model = build_model(environment, config.hidden_size)
    optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate)
    history: list[dict[str, float]] = []
    try:
        for update in range(config.updates):
            batch, rollout_metrics = _collect_rollout(
                model,
                environment,
                steps=config.rollout_steps,
                seed_base=config.seed + update * 10_000,
                gamma=config.gamma,
                gae_lambda=config.gae_lambda,
            )
            update_metrics = _update_model(model, optimizer, batch, config)
            history.append({
                "update": float(update + 1),
                **rollout_metrics,
                **update_metrics,
            })
    finally:
        environment.close()
    return model, history


def evaluate_policy(
    model: CooperativePPO,
    *,
    episodes: int = 20,
    seed: int = 10_000,
    use_sumo: bool = False,
    use_astana: bool = False,
    astana_backend_kwargs: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Evaluate deterministic masked actions on synthetic or SUMO episodes."""
    if episodes < 1:
        raise ValueError("episodes must be positive")
    if use_sumo and use_astana:
        raise ValueError("choose only one simulator backend")
    if use_astana:
        environment = astana_parallel_env(backend_kwargs=astana_backend_kwargs)
    else:
        environment = sumo_parallel_env() if use_sumo else parallel_env()
    episode_rewards: list[float] = []
    successes: list[float] = []
    response_times: list[float] = []
    try:
        for episode in range(episodes):
            observations, _ = environment.reset(seed=seed + episode)
            total_reward = 0.0
            while environment.agents:
                actions = deterministic_actions(model, observations)
                observations, rewards, terminations, truncations, infos = environment.step(actions)
                total_reward += float(rewards[DISPATCHER_AGENT])
                info = infos[DISPATCHER_AGENT]
                successes.append(float(info["success"]))
                if info["response_seconds"] is not None:
                    response_times.append(float(info["response_seconds"]))
                if terminations[DISPATCHER_AGENT] or truncations[DISPATCHER_AGENT]:
                    break
            episode_rewards.append(total_reward)
    finally:
        environment.close()
    return {
        "episodes": float(episodes),
        "mean_episode_reward": float(np.mean(episode_rewards)),
        "success_rate": float(np.mean(successes)),
        "mean_response_seconds": float(np.mean(response_times)) if response_times else 0.0,
    }


def evaluate_random_policy(
    *,
    episodes: int = 20,
    seed: int = 10_000,
) -> dict[str, float]:
    """Evaluate a uniform valid-action baseline on the same synthetic seeds."""
    if episodes < 1:
        raise ValueError("episodes must be positive")
    rng = np.random.default_rng(seed)
    environment = parallel_env()
    episode_rewards: list[float] = []
    successes: list[float] = []
    response_times: list[float] = []
    try:
        for episode in range(episodes):
            observations, _ = environment.reset(seed=seed + episode)
            total_reward = 0.0
            while environment.agents:
                dispatcher_valid = np.flatnonzero(
                    observations[DISPATCHER_AGENT]["action_mask"]
                )
                routing_valid = np.flatnonzero(observations[ROUTING_AGENT]["action_mask"])
                actions = {
                    DISPATCHER_AGENT: int(rng.choice(dispatcher_valid)),
                    ROUTING_AGENT: int(rng.choice(routing_valid)),
                }
                observations, rewards, terminations, truncations, infos = environment.step(actions)
                total_reward += float(rewards[DISPATCHER_AGENT])
                info = infos[DISPATCHER_AGENT]
                successes.append(float(info["success"]))
                response_times.append(float(info["response_seconds"]))
                if terminations[DISPATCHER_AGENT] or truncations[DISPATCHER_AGENT]:
                    break
            episode_rewards.append(total_reward)
    finally:
        environment.close()
    return {
        "episodes": float(episodes),
        "mean_episode_reward": float(np.mean(episode_rewards)),
        "success_rate": float(np.mean(successes)),
        "mean_response_seconds": float(np.mean(response_times)),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--updates", type=int, default=20)
    parser.add_argument("--rollout-steps", type=int, default=256)
    parser.add_argument("--evaluation-episodes", type=int, default=50)
    parser.add_argument("--sumo-evaluation-episodes", type=int, default=0)
    parser.add_argument("--astana-evaluation-episodes", type=int, default=0)
    parser.add_argument("--astana-ordinary-vehicles", type=int, default=100)
    parser.add_argument("--astana-episode-calls", type=int, default=3)
    parser.add_argument("--city-finetune-updates", type=int, default=0)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = PPOConfig(
        seed=args.seed,
        updates=args.updates,
        rollout_steps=args.rollout_steps,
    )
    model, history = train_ppo(config)
    city_finetune_history: list[dict[str, float]] = []
    if args.city_finetune_updates:
        city_config = replace(
            config,
            seed=args.seed + 2_500_000,
            updates=args.city_finetune_updates,
            rollout_steps=args.astana_episode_calls,
            minibatch_size=min(config.minibatch_size, args.astana_episode_calls),
        )
        city_factory = lambda: astana_parallel_env(  # noqa: E731
            backend_kwargs={
                "ordinary_vehicles": args.astana_ordinary_vehicles,
                "episode_calls": args.astana_episode_calls,
                "decision_timeout_seconds": 2_400,
            }
        )
        model, city_finetune_history = train_ppo(
            city_config,
            environment_factory=city_factory,
            model=model,
        )
    evaluation = evaluate_policy(
        model, episodes=args.evaluation_episodes, seed=args.seed + 1_000_000
    )
    random_evaluation = evaluate_random_policy(
        episodes=args.evaluation_episodes, seed=args.seed + 1_000_000
    )
    result: dict[str, Any] = {
        "config": asdict(config),
        "history": history,
        "city_finetune_history": city_finetune_history,
        "evaluation": evaluation,
        "random_evaluation": random_evaluation,
        "reward_improvement_over_random": (
            evaluation["mean_episode_reward"] - random_evaluation["mean_episode_reward"]
        ),
    }
    if args.sumo_evaluation_episodes:
        result["sumo_evaluation"] = evaluate_policy(
            model,
            episodes=args.sumo_evaluation_episodes,
            seed=args.seed + 2_000_000,
            use_sumo=True,
        )
    if args.astana_evaluation_episodes:
        result["astana_evaluation"] = evaluate_policy(
            model,
            episodes=args.astana_evaluation_episodes,
            seed=args.seed + 3_000_000,
            use_astana=True,
            astana_backend_kwargs={
                "ordinary_vehicles": args.astana_ordinary_vehicles,
                "episode_calls": args.astana_episode_calls,
                "decision_timeout_seconds": 2_400,
            },
        )
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        torch.save(model.state_dict(), args.output_dir / "policy.pt")
        (args.output_dir / "metrics.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
