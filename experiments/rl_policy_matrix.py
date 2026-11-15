"""Compare dispatch/routing policies on identical full-Astana calls."""
from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from ai.env import DISPATCHER_AGENT, ROUTING_AGENT, astana_parallel_env
from ai.training.ppo_smoke import (
    CooperativePPO,
    deterministic_actions,
    load_policy,
)


POLICIES = ("random", "nearest", "dijkstra", "astar", "traffic_aware", "ppo")
ROUTE_ACTIONS = {"dijkstra": 0, "astar": 1, "traffic_aware": 2}


def select_actions(
    policy: str,
    observations: dict[str, dict[str, np.ndarray]],
    *,
    rng: np.random.Generator,
    model: CooperativePPO | None = None,
) -> dict[str, int]:
    """Choose one valid joint action using a named comparison policy."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy: {policy}")
    if policy == "ppo":
        if model is None:
            raise ValueError("ppo policy requires a checkpoint model")
        return deterministic_actions(model, observations)

    unit_observation = observations[DISPATCHER_AGENT]
    route_observation = observations[ROUTING_AGENT]
    valid_units = np.flatnonzero(unit_observation["action_mask"])
    valid_routes = np.flatnonzero(route_observation["action_mask"])
    if policy == "random":
        unit = int(rng.choice(valid_units))
        route = int(rng.choice(valid_routes))
    else:
        unit = int(valid_units[np.argmin(unit_observation["units"][valid_units, 1])])
        if policy == "nearest":
            route = int(
                valid_routes[np.argmin(route_observation["routes"][valid_routes, 0])]
            )
        else:
            route = ROUTE_ACTIONS[policy]
            if route not in valid_routes:
                raise RuntimeError(f"{policy} route is not valid in the current state")
    return {DISPATCHER_AGENT: unit, ROUTING_AGENT: route}


def _summary(policy: str, seed: int, calls: list[dict[str, Any]]) -> dict[str, Any]:
    response_times = [float(call["response_seconds"]) for call in calls]
    minimum_ttcs = [
        float(call["minimum_ttc_seconds"])
        for call in calls
        if call["minimum_ttc_seconds"] is not None
    ]
    return {
        "policy": policy,
        "seed": seed,
        "calls": len(calls),
        "successful_calls": sum(bool(call["success"]) for call in calls),
        "success_rate": float(np.mean([call["success"] for call in calls])),
        "total_reward": float(sum(call["reward"] for call in calls)),
        "mean_response_seconds": float(np.mean(response_times)),
        "p95_response_seconds": float(np.percentile(response_times, 95)),
        "max_response_seconds": float(np.max(response_times)),
        "physical_collisions": sum(bool(call["physical_collision"]) for call in calls),
        "near_misses": sum(int(call["near_misses"]) for call in calls),
        "minimum_ttc_seconds": min(minimum_ttcs, default=None),
        "mean_civilian_delay_seconds": float(
            np.mean([call["civilian_delay_seconds"] for call in calls])
        ),
    }


def run_policy(
    policy: str,
    seed: int,
    *,
    environment_factory: Callable[[], Any],
    model: CooperativePPO | None = None,
) -> dict[str, Any]:
    """Run one policy/seed pair and return call-level and aggregate metrics."""
    environment = environment_factory()
    rng = np.random.default_rng(np.random.SeedSequence([seed, POLICIES.index(policy)]))
    calls: list[dict[str, Any]] = []
    try:
        observations, _ = environment.reset(seed=seed)
        while environment.agents:
            actions = select_actions(policy, observations, rng=rng, model=model)
            observations, rewards, terminations, truncations, infos = environment.step(
                actions
            )
            info = infos[DISPATCHER_AGENT]
            calls.append({
                "call_index": len(calls),
                "call_id": info["call_id"],
                "destination_edge_id": info["destination_edge_id"],
                "selected_unit_id": info["selected_unit_id"],
                "selected_route_id": info["selected_route_id"],
                "dispatcher_action": actions[DISPATCHER_AGENT],
                "routing_action": actions[ROUTING_AGENT],
                "success": bool(info["success"]),
                "reward": float(rewards[DISPATCHER_AGENT]),
                "response_seconds": float(info["response_seconds"]),
                "civilian_delay_seconds": float(info["civilian_delay_seconds"]),
                "minimum_ttc_seconds": info["minimum_ttc_seconds"],
                "near_misses": int(info["near_misses"]),
                "physical_collision": bool(info["physical_collision"]),
                "route_edges": int(info["route_edges"]),
            })
            if terminations[DISPATCHER_AGENT] or truncations[DISPATCHER_AGENT]:
                break
    finally:
        environment.close()
    if not calls:
        raise RuntimeError(f"policy {policy} seed {seed} produced no calls")
    return {"summary": _summary(policy, seed, calls), "calls": calls}


def run_matrix(
    policies: Sequence[str],
    seeds: Sequence[int],
    *,
    environment_factory: Callable[[], Any],
    model: CooperativePPO | None = None,
) -> dict[str, Any]:
    """Run a fair policy matrix and reject mismatched call scenarios."""
    runs: list[dict[str, Any]] = []
    signatures: dict[int, tuple[str, ...]] = {}
    for seed in seeds:
        for policy in policies:
            run = run_policy(
                policy,
                seed,
                environment_factory=environment_factory,
                model=model,
            )
            signature = tuple(call["destination_edge_id"] for call in run["calls"])
            expected = signatures.setdefault(seed, signature)
            if signature != expected:
                raise RuntimeError(
                    f"scenario mismatch for seed {seed}: {policy} saw {signature}, "
                    f"expected {expected}"
                )
            runs.append(run)
    return {
        "protocol": {
            "policies": list(policies),
            "seeds": list(seeds),
            "same_calls_verified": True,
        },
        "runs": runs,
    }


def write_results(result: dict[str, Any], output: Path) -> None:
    """Write machine-readable results and flat diploma-table CSV files."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "matrix.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summaries = [run["summary"] for run in result["runs"]]
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    call_rows = [
        {"policy": run["summary"]["policy"], "seed": run["summary"]["seed"], **call}
        for run in result["runs"]
        for call in run["calls"]
    ]
    with (output / "calls.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(call_rows[0]))
        writer.writeheader()
        writer.writerows(call_rows)


def _parse_csv(value: str, cast: Callable[[str], Any]) -> list[Any]:
    return [cast(item.strip()) for item in value.split(",") if item.strip()]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policies",
        default="random,nearest,dijkstra,astar,traffic_aware",
    )
    parser.add_argument("--seeds", default="42")
    parser.add_argument("--ordinary-vehicles", type=int, default=100)
    parser.add_argument("--episode-calls", type=int, default=3)
    parser.add_argument("--decision-timeout-seconds", type=int, default=2_400)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--hidden-size", type=int, default=64)
    parser.add_argument(
        "--output", type=Path, default=Path("experiments/results/rl-policy-matrix")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    policies = _parse_csv(args.policies, str)
    seeds = _parse_csv(args.seeds, int)
    unknown = sorted(set(policies) - set(POLICIES))
    if unknown:
        raise ValueError(f"unknown policies: {unknown}")
    if "ppo" in policies and args.checkpoint is None:
        raise ValueError("--checkpoint is required when --policies includes ppo")

    def environment_factory() -> Any:
        return astana_parallel_env(backend_kwargs={
            "ordinary_vehicles": args.ordinary_vehicles,
            "episode_calls": args.episode_calls,
            "decision_timeout_seconds": args.decision_timeout_seconds,
        })

    model = None
    if args.checkpoint is not None:
        shape_environment = environment_factory()
        try:
            model = load_policy(
                args.checkpoint, shape_environment, hidden_size=args.hidden_size
            )
        finally:
            shape_environment.close()
    result = run_matrix(
        policies,
        seeds,
        environment_factory=environment_factory,
        model=model,
    )
    write_results(result, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
