"""SUMO adapter for one-step PettingZoo logistics smoke episodes."""
from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import sumolib
import traci

from .emergency_parallel_env import DecisionOutcome, DecisionState, DecisionTransition


ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = ROOT / "simulation" / "sumo" / "rl_smoke"
DEPOT_EDGES = ("depot_0_in", "depot_1_in", "depot_2_in", "depot_3_in")
ROUTE_BRANCHES = (
    ("upper_in", "upper_out"),
    ("center_in", "center_out"),
    ("lower_in", "lower_out"),
)
ROUTE_NAMES = ("upper", "center", "lower")


class SumoDecisionBackend:
    """Apply a unit/route decision in SUMO and return measured metrics.

    The adapter is intentionally one-call-per-episode. It uses a generated
    temporary network so training smoke tests never mutate tracked SUMO files.
    """

    max_units = len(DEPOT_EDGES)
    max_routes = len(ROUTE_BRANCHES)

    def __init__(self, *, warmup_seconds: int = 10, decision_timeout_seconds: int = 180) -> None:
        if warmup_seconds < 0 or decision_timeout_seconds < 1:
            raise ValueError("SUMO timing values must be non-negative and non-zero")
        self.warmup_seconds = warmup_seconds
        self.decision_timeout_seconds = decision_timeout_seconds
        self._temporary_dir: tempfile.TemporaryDirectory[str] | None = None
        self._connection: Any | None = None
        self._label: str | None = None
        self._network: sumolib.net.Net | None = None
        self._evaluated = False

    def reset(self, seed: int | None) -> DecisionState:
        self.close()
        resolved_seed = 0 if seed is None else int(seed)
        self._temporary_dir = tempfile.TemporaryDirectory(prefix="emergency-mas-rl-")
        network_path = Path(self._temporary_dir.name) / "rl_smoke.net.xml"
        subprocess.run(
            [
                sumolib.checkBinary("netconvert"),
                "--node-files", str(SCENARIO_DIR / "rl_smoke.nod.xml"),
                "--edge-files", str(SCENARIO_DIR / "rl_smoke.edg.xml"),
                "--connection-files", str(SCENARIO_DIR / "rl_smoke.con.xml"),
                "--output-file", str(network_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self._network = sumolib.net.readNet(str(network_path))
        self._label = f"rl-smoke-{uuid.uuid4().hex}"
        command = [
            sumolib.checkBinary("sumo"),
            "-n", str(network_path),
            "-r", str(SCENARIO_DIR / "rl_smoke.rou.xml"),
            "--seed", str(resolved_seed),
            "--step-length", "1",
            "--time-to-teleport", "-1",
            "--collision.action", "warn",
            "--collision.check-junctions",
            "--no-step-log", "true",
            "--duration-log.disable", "true",
        ]
        traci.start(command, label=self._label, doSwitch=False)
        self._connection = traci.getConnection(self._label)
        for _ in range(self.warmup_seconds):
            self._connection.simulationStep()
        self._evaluated = False
        return self._state_from_sumo(resolved_seed)

    def evaluate(self, unit_action: int, route_action: int) -> DecisionTransition:
        if self._connection is None or self._network is None:
            raise RuntimeError("SUMO backend requires reset()")
        if self._evaluated:
            raise RuntimeError("SUMO backend supports one decision per episode")
        self._evaluated = True
        connection = self._connection
        route_edges = (
            DEPOT_EDGES[unit_action],
            "hub_to_split",
            *ROUTE_BRANCHES[route_action],
            "finish",
        )
        route_id = f"emergency_route_{unit_action}_{route_action}"
        vehicle_id = "ambulance_rl"
        connection.route.add(route_id, route_edges)
        connection.vehicle.add(
            vehicle_id,
            route_id,
            typeID="ambulance",
            depart="now",
            departLane="best",
            departSpeed="max",
        )
        started_at = float(connection.simulation.getTime())
        deadline = started_at + self.decision_timeout_seconds
        arrived = False
        collision = False
        min_ttc: float | None = None
        civilian_time_loss: dict[str, float] = {}
        steps = 0
        try:
            while connection.simulation.getTime() < deadline:
                connection.simulationStep()
                steps += 1
                active = set(connection.vehicle.getIDList())
                for civilian_id in (item for item in active if item.startswith("civilian_")):
                    civilian_time_loss[civilian_id] = max(
                        civilian_time_loss.get(civilian_id, 0.0),
                        float(connection.vehicle.getTimeLoss(civilian_id)),
                    )
                if vehicle_id in connection.simulation.getCollidingVehiclesIDList():
                    collision = True
                if vehicle_id in active:
                    leader = connection.vehicle.getLeader(vehicle_id, 100.0)
                    if leader is not None and leader[0] in active:
                        leader_id, gap = leader
                        closing_speed = (
                            connection.vehicle.getSpeed(vehicle_id)
                            - connection.vehicle.getSpeed(leader_id)
                        )
                        if closing_speed > 0 and gap >= 0:
                            ttc = float(gap / closing_speed)
                            min_ttc = ttc if min_ttc is None else min(min_ttc, ttc)
                if vehicle_id in connection.simulation.getArrivedIDList():
                    arrived = True
                    break
            response_seconds = float(connection.simulation.getTime() - started_at)
        finally:
            self.close()

        safety_risk = 1.0 if collision else (
            0.0 if min_ttc is None else float(np.clip(1.0 - min_ttc / 5.0, 0.0, 1.0))
        )
        civilian_delay = (
            float(np.mean(list(civilian_time_loss.values()))) if civilian_time_loss else 0.0
        )
        return DecisionTransition(
            outcome=DecisionOutcome(
                response_seconds=response_seconds,
                safety_risk=safety_risk,
                civilian_delay_seconds=civilian_delay,
                success=arrived and not collision,
                details={
                    "backend": "sumo",
                    "selected_unit_id": f"ambulance_{unit_action}",
                    "selected_route_id": ROUTE_NAMES[route_action],
                    "route_edges": list(route_edges),
                    "minimum_ttc_seconds": min_ttc,
                    "physical_collision": collision,
                    "simulation_steps": steps,
                },
            ),
            next_state=None,
            terminated=True,
        )

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close(wait=False)
            except (traci.TraCIException, traci.FatalTraCIError, OSError):
                pass
        self._connection = None
        self._label = None
        self._network = None
        if self._temporary_dir is not None:
            self._temporary_dir.cleanup()
            self._temporary_dir = None

    def _state_from_sumo(self, seed: int) -> DecisionState:
        assert self._connection is not None and self._network is not None
        rng = np.random.default_rng(seed)
        unit_lengths = np.array([
            self._network.getEdge(edge_id).getLength()
            + self._network.getEdge("hub_to_split").getLength()
            for edge_id in DEPOT_EDGES
        ], dtype=np.float32)
        units = np.zeros((self.max_units, 5), dtype=np.float32)
        units[:, 0] = 1.0
        units[:, 1] = unit_lengths / max(float(unit_lengths.max()), 1.0)
        units[:, 2] = rng.uniform(0.05, 0.35, size=self.max_units)
        units[:, 3] = rng.uniform(0.0, 0.15, size=self.max_units)
        units[:, 4] = 1.0

        route_lengths = np.array([
            sum(self._network.getEdge(edge_id).getLength() for edge_id in (*branch, "finish"))
            for branch in ROUTE_BRANCHES
        ], dtype=np.float32)
        congestion = np.array([
            sum(self._connection.edge.getLastStepVehicleNumber(edge_id) for edge_id in branch)
            for branch in ROUTE_BRANCHES
        ], dtype=np.float32)
        congestion /= max(float(congestion.max()), 1.0)
        routes = np.zeros((self.max_routes, 6), dtype=np.float32)
        routes[:, 0] = route_lengths / (13.89 * 60.0)
        routes[:, 1] = route_lengths / max(float(route_lengths.max()), 1.0)
        routes[:, 2] = congestion
        routes[:, 3] = np.array([0.35, 0.15, 0.30], dtype=np.float32)
        routes[:, 4] = 0.0
        routes[:, 5] = 0.0

        call = np.array([
            rng.uniform(0.5, 1.0),
            0.0,
            rng.uniform(0.0, 0.4),
            float(congestion.mean()),
            0.0,
        ], dtype=np.float32)
        driver = np.array([
            units[0, 2],
            units[0, 3],
            rng.uniform(0.0, 0.2),
            rng.uniform(0.2, 0.6),
        ], dtype=np.float32)
        return DecisionState(
            call=call,
            units=units,
            routes=np.clip(routes, 0.0, 1.0),
            driver=driver,
            unit_mask=np.ones(self.max_units, dtype=np.int8),
            route_mask=np.ones(self.max_routes, dtype=np.int8),
        )


def sumo_parallel_env(**kwargs: Any):
    """Create a one-call PettingZoo environment backed by SUMO."""
    from .emergency_parallel_env import EmergencyLogisticsParallelEnv

    episode_calls = kwargs.pop("episode_calls", 1)
    if episode_calls != 1:
        raise ValueError("SUMO smoke backend supports episode_calls=1 only")
    backend = kwargs.pop("backend", None) or SumoDecisionBackend()
    return EmergencyLogisticsParallelEnv(episode_calls=1, backend=backend, **kwargs)
