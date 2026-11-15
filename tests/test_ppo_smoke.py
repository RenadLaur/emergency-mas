from __future__ import annotations

import numpy as np
import pytest
import torch

from ai.env import DISPATCHER_AGENT, ROUTING_AGENT, parallel_env
from ai.training.ppo_smoke import (
    PPOConfig,
    CooperativePPO,
    build_model,
    evaluate_policy,
    evaluate_random_policy,
    train_ppo,
)


def test_masked_distribution_never_selects_invalid_action() -> None:
    logits = torch.tensor([[100.0, 5.0, 1.0]])
    mask = torch.tensor([[False, True, True]])
    distribution = CooperativePPO.masked_distribution(logits, mask)

    samples = distribution.sample((100,))

    assert not torch.any(samples == 0)


def test_short_ppo_training_is_reproducible_and_evaluable() -> None:
    config = PPOConfig(
        seed=17,
        updates=2,
        rollout_steps=32,
        update_epochs=2,
        minibatch_size=16,
        hidden_size=16,
    )

    first_model, first_history = train_ppo(config)
    second_model, second_history = train_ppo(config)
    evaluation = evaluate_policy(first_model, episodes=3, seed=123)
    random_evaluation = evaluate_random_policy(episodes=3, seed=123)

    assert first_history == second_history
    assert len(first_history) == 2
    assert np.isfinite(first_history[-1]["loss"])
    assert evaluation["episodes"] == 3.0
    assert np.isfinite(evaluation["mean_episode_reward"])
    assert random_evaluation["episodes"] == 3.0

    environment = parallel_env()
    observations, _ = environment.reset(seed=9)
    assert observations[DISPATCHER_AGENT]["action_mask"].any()
    assert observations[ROUTING_AGENT]["action_mask"].any()
    environment.close()


def test_policy_evaluation_rejects_two_simulator_backends() -> None:
    config = PPOConfig(seed=1, updates=1, rollout_steps=8, update_epochs=1, minibatch_size=8)
    model, _ = train_ppo(config)

    with pytest.raises(ValueError, match="only one simulator backend"):
        evaluate_policy(model, episodes=1, use_sumo=True, use_astana=True)


def test_model_build_uses_spaces_without_resetting_environment() -> None:
    environment = parallel_env()
    environment.reset = lambda *args, **kwargs: pytest.fail("reset must not be called")

    model = build_model(environment, hidden_size=8)

    assert model.dispatcher_actor[0].in_features == 25
    assert model.routing_actor[0].in_features == 27
    environment.close()
