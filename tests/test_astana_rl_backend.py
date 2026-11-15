from ai.env import DISPATCHER_AGENT, ROUTING_AGENT, astana_parallel_env
from ai.env.astana_backend import AstanaCityBackend


def test_astana_factory_preserves_multi_call_dimensions_without_starting_sumo() -> None:
    environment = astana_parallel_env(
        backend_kwargs={"ordinary_vehicles": 10, "episode_calls": 2}
    )

    assert environment.episode_calls == 2
    assert environment.action_space(DISPATCHER_AGENT).n == 4
    assert environment.action_space(ROUTING_AGENT).n == 3
    environment.close()


def test_civilian_delay_uses_observed_peak_for_arrived_vehicles() -> None:
    before = {"civilian_0": 2.0, "civilian_1": 1.0}
    observed = {"civilian_0": 8.0, "civilian_1": 4.0, "civilian_2": 5.0}

    delay = AstanaCityBackend._mean_time_loss_delta(before, observed)

    assert delay == (6.0 + 3.0 + 5.0) / 3.0
