"""Multi-call PettingZoo backend on the full Astana SUMO network."""
from __future__ import annotations

import random
import tempfile
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import sumolib
import traci
import traci.constants as tc

from agents.dispatcher.dispatcher import (
    EmergencyCall,
    EmergencyUnit,
    NearestFreeDispatcher,
    UnitStatus,
)
from agents.driver.emergency_state import EmergencyDriverModel
from agents.incident.safety import SafetyMonitor
from experiments.baseline_runner import (
    astar_path,
    build_vehicle_specs,
    passenger_component,
    write_routes,
)
from simulation.weather.model import WeatherProfile, profile_from_config

from .emergency_parallel_env import (
    DecisionOutcome,
    DecisionState,
    DecisionTransition,
    EmergencyLogisticsParallelEnv,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_NETWORK = ROOT / "simulation" / "maps" / "astana_city" / "generated" / "astana_city.net.xml"
ROUTE_NAMES = ("dijkstra", "astar", "traffic_aware")


class AstanaCityBackend:
    """Run sequential emergency calls amid live traffic on the full city map.

    The interface remains only ``reset/evaluate/close``. Network loading,
    civilian demand, fleet state, routing algorithms, driver evolution and
    TraCI resource cleanup stay inside this adapter.
    """

    max_units = 4
    max_routes = len(ROUTE_NAMES)

    def __init__(
        self,
        *,
        network_path: Path = DEFAULT_NETWORK,
        ordinary_vehicles: int = 100,
        episode_calls: int = 3,
        warmup_seconds: int = 30,
        decision_timeout_seconds: int = 1_200,
        weather: dict[str, Any] | None = None,
    ) -> None:
        if ordinary_vehicles < 1:
            raise ValueError("ordinary_vehicles must be positive")
        if episode_calls < 1:
            raise ValueError("episode_calls must be positive")
        if warmup_seconds < 0 or decision_timeout_seconds < 1:
            raise ValueError("SUMO timing values must be non-negative and non-zero")
        self.network_path = Path(network_path)
        self.ordinary_vehicles = ordinary_vehicles
        self.episode_calls = episode_calls
        self.warmup_seconds = warmup_seconds
        self.decision_timeout_seconds = decision_timeout_seconds
        self.weather: WeatherProfile = profile_from_config(weather or {"condition": "rain"})

        self._temporary_dir: tempfile.TemporaryDirectory[str] | None = None
        self._connection: Any | None = None
        self._label: str | None = None
        self._network: sumolib.net.Net | None = None
        self._pool: list[Any] = []
        self._rng = random.Random()
        self._seed = 0
        self._dispatcher: NearestFreeDispatcher | None = None
        self._driver_model: EmergencyDriverModel | None = None
        self._current_call: EmergencyCall | None = None
        self._call_index = 0
        self._call_destinations: list[str] = []
        self._call_urgencies: list[float] = []
        self._route_cache: dict[tuple[str, int], tuple[tuple[str, ...], float]] = {}
        self._weather_edges: set[str] = set()

    def reset(self, seed: int | None) -> DecisionState:
        self.close()
        if not self.network_path.exists():
            raise FileNotFoundError(self.network_path)
        self._seed = 0 if seed is None else int(seed)
        self._rng = random.Random(f"astana-rl:{self._seed}")
        self._network = sumolib.net.readNet(str(self.network_path))
        pool = self._emergency_pool()
        self._pool = pool
        depot_edges = self._rng.sample(pool, self.max_units)
        self._plan_calls([edge.getID() for edge in depot_edges])
        self._dispatcher = NearestFreeDispatcher([
            EmergencyUnit(f"ambulance_{index}", edge.getID())
            for index, edge in enumerate(depot_edges)
        ])
        self._driver_model = EmergencyDriverModel(
            self._seed, self.weather.reaction_time_multiplier
        )
        for unit_id in self._dispatcher.units:
            self._driver_model.register(unit_id)

        self._temporary_dir = tempfile.TemporaryDirectory(prefix="emergency-mas-astana-rl-")
        route_path = Path(self._temporary_dir.name) / "astana_rl.rou.xml"
        scenario = {
            "seed": self._seed,
            "ordinary_vehicles": self.ordinary_vehicles,
            "emergency_vehicles": 1,
            "ordinary_departure_window_seconds": 600,
            "routing_strategy": "traffic_aware",
        }
        civilian_specs = build_vehicle_specs(self._network, scenario, include_emergency=False)
        write_routes(
            route_path,
            civilian_specs,
            {
                "mode": "blue_light",
                "speed_factor": 1.15,
                "lateral_resolution_meters": 0.4,
                "reaction_distance_meters": 75,
            },
            self.weather,
        )
        command = [
            sumolib.checkBinary("sumo"),
            "-n", str(self.network_path),
            "-r", str(route_path),
            "--seed", str(self._seed),
            "--time-to-teleport", "-1",
            "--collision.action", "warn",
            "--collision.check-junctions",
            "--lateral-resolution", "0.4",
            "--device.bluelight.reactiondist", "75",
            "--no-step-log", "true",
            "--duration-log.disable", "true",
        ]
        if self.weather.surface_friction < 1.0:
            command.extend(["--device.friction.probability", "1"])
        self._label = f"astana-rl-{uuid.uuid4().hex}"
        traci.start(command, label=self._label, doSwitch=False)
        self._connection = traci.getConnection(self._label)
        self._weather_edges.clear()
        for _ in range(self.warmup_seconds):
            self._connection.simulationStep()
            self._apply_weather_to_active_edges()
        self._call_index = 0
        state = self._prepare_next_call()
        if state is None:
            self.close()
            raise RuntimeError("could not create a reachable Astana emergency call")
        return state

    def evaluate(self, unit_action: int, route_action: int) -> DecisionTransition:
        if (
            self._connection is None
            or self._dispatcher is None
            or self._driver_model is None
            or self._current_call is None
        ):
            raise RuntimeError("Astana backend requires reset()")
        unit_id = f"ambulance_{unit_action}"
        route = self._route_cache.get((unit_id, route_action))
        if route is None:
            raise ValueError("selected Astana unit/route combination is not feasible")
        path, estimate_seconds = route
        assignment = self._dispatcher.assign_unit(
            self._current_call.call_id,
            unit_id,
            lambda unit, call: (path, estimate_seconds),
        )
        if assignment is None:
            raise ValueError("selected Astana unit is no longer available")

        connection = self._connection
        call_id = self._current_call.call_id
        destination_edge_id = self._current_call.destination_edge_id
        vehicle_id = f"rl_{call_id}_{unit_id}"
        route_id = f"route_{vehicle_id}"
        connection.route.add(route_id, list(path))
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
        civilian_before = self._civilian_time_loss()
        civilian_observed = dict(civilian_before)
        safety = SafetyMonitor(2.0)
        arrived = False
        collision = False
        steps = 0
        last_driver_update = started_at

        while connection.simulation.getTime() < deadline:
            connection.simulationStep()
            steps += 1
            if steps % 10 == 0:
                for civilian_id, time_loss in self._civilian_time_loss().items():
                    civilian_observed[civilian_id] = max(
                        civilian_observed.get(civilian_id, 0.0), time_loss
                    )
            self._apply_weather_to_active_edges()
            active = set(connection.vehicle.getIDList())
            if vehicle_id in connection.simulation.getCollidingVehiclesIDList():
                collision = True
            if vehicle_id in active:
                now = float(connection.simulation.getTime())
                driver_state = self._driver_model.advance(
                    unit_id,
                    now - last_driver_update,
                    connection.vehicle.getSpeed(vehicle_id),
                    connection.vehicle.getAllowedSpeed(vehicle_id),
                )
                last_driver_update = now
                connection.vehicle.setSpeedFactor(
                    vehicle_id, self.weather.speed_factor * 1.15 * driver_state.speed_factor
                )
                leader = connection.vehicle.getLeader(vehicle_id, 0.0)
                if leader is None or leader[0] not in active:
                    safety.observe(
                        vehicle_id,
                        None,
                        None,
                        connection.vehicle.getSpeed(vehicle_id),
                        0.0,
                    )
                else:
                    leader_id, gap = leader
                    safety.observe(
                        vehicle_id,
                        leader_id,
                        gap,
                        connection.vehicle.getSpeed(vehicle_id),
                        connection.vehicle.getSpeed(leader_id),
                    )
            if vehicle_id in connection.simulation.getArrivedIDList():
                arrived = True
                break

        response_seconds = float(connection.simulation.getTime() - started_at)
        for civilian_id, time_loss in self._civilian_time_loss().items():
            civilian_observed[civilian_id] = max(
                civilian_observed.get(civilian_id, 0.0), time_loss
            )
        civilian_delay = self._mean_time_loss_delta(civilian_before, civilian_observed)
        if arrived and not collision:
            self._dispatcher.complete(unit_id, float(connection.simulation.getTime()))
        else:
            if vehicle_id in connection.vehicle.getIDList():
                connection.vehicle.remove(vehicle_id)
            self._dispatcher.mark_unavailable(unit_id)

        minimum_ttc = safety.minimum_ttc_seconds
        safety_risk = 1.0 if collision else (
            0.0
            if minimum_ttc is None
            else float(np.clip(1.0 - minimum_ttc / 5.0, 0.0, 1.0))
        )
        self._call_index += 1
        next_state = (
            self._prepare_next_call() if self._call_index < self.episode_calls else None
        )
        terminated = next_state is None
        outcome = DecisionOutcome(
            response_seconds=response_seconds,
            safety_risk=safety_risk,
            civilian_delay_seconds=civilian_delay,
            success=arrived and not collision,
            details={
                "backend": "astana_city",
                "call_id": call_id,
                "scenario_seed": self._seed,
                "destination_edge_id": destination_edge_id,
                "selected_unit_id": unit_id,
                "selected_route_id": ROUTE_NAMES[route_action],
                "route_edges": len(path),
                "estimated_response_seconds": estimate_seconds,
                "minimum_ttc_seconds": minimum_ttc,
                "near_misses": len(safety.near_misses),
                "physical_collision": collision,
                "simulation_steps": steps,
                "simulation_time_seconds": float(connection.simulation.getTime()),
            },
        )
        return DecisionTransition(outcome, next_state, terminated)

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close(wait=False)
            except (traci.TraCIException, traci.FatalTraCIError, OSError):
                pass
        self._connection = None
        self._label = None
        self._network = None
        self._pool = []
        self._dispatcher = None
        self._driver_model = None
        self._current_call = None
        self._call_destinations.clear()
        self._call_urgencies.clear()
        self._route_cache.clear()
        self._weather_edges.clear()
        if self._temporary_dir is not None:
            self._temporary_dir.cleanup()
            self._temporary_dir = None

    def _emergency_pool(self) -> list[Any]:
        assert self._network is not None
        pool = [
            edge
            for edge in passenger_component(self._network)
            if edge.allows("emergency") and edge.getLength() >= 80
        ]
        if len(pool) < self.max_units + 1:
            raise ValueError("Astana network has too few emergency-accessible edges")
        return pool

    def _plan_calls(self, depot_edge_ids: list[str]) -> None:
        """Freeze exogenous calls before any policy action can affect the city.

        Every destination is reachable from every initial depot and every
        earlier destination. Therefore different policies see the same calls,
        while their fleet movements and traffic consequences may still differ.
        """
        assert self._network is not None
        destinations: list[str] = []
        possible_origins = list(depot_edge_ids)
        forbidden = set(depot_edge_ids)
        for _ in range(self.episode_calls):
            for _attempt in range(500):
                destination = self._rng.choice(self._pool).getID()
                if destination in forbidden:
                    continue
                destination_edge = self._network.getEdge(destination)
                reachable = all(
                    self._network.getShortestPath(
                        self._network.getEdge(origin),
                        destination_edge,
                        vClass="emergency",
                    )[0]
                    for origin in possible_origins
                )
                if not reachable:
                    continue
                destinations.append(destination)
                possible_origins.append(destination)
                forbidden.add(destination)
                break
            else:
                raise RuntimeError("could not plan policy-independent Astana calls")
        self._call_destinations = destinations
        self._call_urgencies = [
            self._rng.uniform(0.6, 1.0) for _ in range(self.episode_calls)
        ]

    def _prepare_next_call(self) -> DecisionState | None:
        assert self._connection is not None and self._dispatcher is not None
        available = [
            unit for unit in self._dispatcher.units.values()
            if unit.status is UnitStatus.AVAILABLE
        ]
        if not available:
            return None
        destination = self._call_destinations[self._call_index]
        call = EmergencyCall(
            f"call_{self._call_index:02d}",
            float(self._connection.simulation.getTime()),
            destination,
        )
        routes: dict[tuple[str, int], tuple[tuple[str, ...], float]] = {}
        for unit in available:
            for route_index in range(self.max_routes):
                route = self._find_route(unit, call, route_index)
                if route is not None:
                    routes[(unit.unit_id, route_index)] = route
        feasible_units = [
            unit for unit in available
            if all(
                (unit.unit_id, route_index) in routes
                for route_index in range(self.max_routes)
            )
        ]
        if not feasible_units:
            return None
        self._route_cache = routes
        self._current_call = call
        self._dispatcher.create_call(call)
        return self._state_for_call(feasible_units)

    def _find_route(
        self,
        unit: EmergencyUnit,
        call: EmergencyCall,
        route_index: int,
    ) -> tuple[tuple[str, ...], float] | None:
        assert self._network is not None and self._connection is not None
        origin = self._network.getEdge(unit.edge_id)
        destination = self._network.getEdge(call.destination_edge_id)
        if route_index == 0:
            edges, _ = self._network.getShortestPath(
                origin, destination, vClass="emergency"
            )
            path = tuple(edge.getID() for edge in edges) if edges else ()
            estimate = self._free_flow_seconds(path)
        elif route_index == 1:
            edges, _ = astar_path(
                self._network, origin, destination, v_class="emergency"
            )
            path = tuple(edge.getID() for edge in edges) if edges else ()
            estimate = self._free_flow_seconds(path)
        else:
            try:
                result = self._connection.simulation.findRoute(
                    unit.edge_id,
                    call.destination_edge_id,
                    vType="ambulance",
                    depart=float(self._connection.simulation.getTime()),
                    routingMode=tc.ROUTING_MODE_AGGREGATED,
                )
            except traci.TraCIException:
                return None
            path = tuple(result.edges)
            estimate = float(result.travelTime)
        if len(path) < 2 or not np.isfinite(estimate):
            return None
        return path, max(1.0, estimate)

    def _free_flow_seconds(self, path: tuple[str, ...]) -> float:
        assert self._network is not None
        return sum(
            self._network.getEdge(edge_id).getLength()
            / max(self._network.getEdge(edge_id).getSpeed(), 0.1)
            for edge_id in path
        )

    def _state_for_call(self, feasible_units: list[EmergencyUnit]) -> DecisionState:
        assert self._dispatcher is not None and self._driver_model is not None
        feasible_ids = {unit.unit_id for unit in feasible_units}
        units = np.zeros((self.max_units, 5), dtype=np.float32)
        unit_mask = np.zeros(self.max_units, dtype=np.int8)
        for index in range(self.max_units):
            unit_id = f"ambulance_{index}"
            unit = self._dispatcher.units[unit_id]
            state = self._driver_model.states[unit_id]
            estimates = [
                self._route_cache[(unit_id, route_index)][1]
                for route_index in range(self.max_routes)
                if (unit_id, route_index) in self._route_cache
            ]
            available = unit.status is UnitStatus.AVAILABLE and unit_id in feasible_ids
            units[index] = (
                float(available),
                min(estimates, default=1_800.0) / 1_800.0,
                state.stress,
                state.fatigue,
                1.0,
            )
            unit_mask[index] = int(available)

        routes = np.zeros((self.max_routes, 6), dtype=np.float32)
        route_mask = np.ones(self.max_routes, dtype=np.int8)
        for route_index in range(self.max_routes):
            candidates = [
                (self._route_cache[(unit.unit_id, route_index)], unit.unit_id)
                for unit in feasible_units
            ]
            (path, estimate), _ = min(candidates, key=lambda item: item[0][1])
            distance = self._route_distance(path)
            congestion = self._route_congestion(path)
            weather_risk = 1.0 - self.weather.surface_friction
            signal_count = self._signal_count(path)
            routes[route_index] = (
                np.clip(estimate / 1_800.0, 0.0, 1.0),
                np.clip(distance / 25_000.0, 0.0, 1.0),
                congestion,
                np.clip(0.55 * weather_risk + 0.45 * congestion, 0.0, 1.0),
                0.0,
                np.clip(signal_count / 30.0, 0.0, 1.0),
            )

        driver_states = [
            self._driver_model.states[unit.unit_id] for unit in feasible_units
        ]
        driver = np.array([
            np.mean([state.stress for state in driver_states]),
            np.mean([state.fatigue for state in driver_states]),
            np.mean([1.0 - state.attention for state in driver_states]),
            np.mean([min(state.reaction_seconds / 3.0, 1.0) for state in driver_states]),
        ], dtype=np.float32)
        severity = {
            "dry": 0.0,
            "rain": 0.35,
            "snow": 0.6,
            "ice": 1.0,
            "fog": 0.65,
        }[self.weather.condition.value]
        call = np.array([
            self._call_urgencies[self._call_index],
            0.0,
            severity,
            float(routes[:, 2].mean()),
            0.0,
        ], dtype=np.float32)
        return DecisionState(
            call=call,
            units=np.clip(units, 0.0, 1.0),
            routes=np.clip(routes, 0.0, 1.0),
            driver=np.clip(driver, 0.0, 1.0),
            unit_mask=unit_mask,
            route_mask=route_mask,
        )

    def _route_distance(self, path: tuple[str, ...]) -> float:
        assert self._network is not None
        return sum(self._network.getEdge(edge_id).getLength() for edge_id in path)

    def _route_congestion(self, path: tuple[str, ...]) -> float:
        assert self._connection is not None
        if not path:
            return 0.0
        counts = [
            self._connection.edge.getLastStepVehicleNumber(edge_id) for edge_id in path
        ]
        return float(np.clip(np.mean(counts) / 3.0, 0.0, 1.0))

    def _signal_count(self, path: tuple[str, ...]) -> int:
        assert self._network is not None
        return sum(
            self._network.getEdge(edge_id).getToNode().getType() == "traffic_light"
            for edge_id in path
        )

    def _apply_weather_to_active_edges(self) -> None:
        if self._connection is None or self.weather.surface_friction >= 1.0:
            return
        for vehicle_id in self._connection.vehicle.getIDList():
            edge_id = self._connection.vehicle.getRoadID(vehicle_id)
            if edge_id.startswith(":") or edge_id in self._weather_edges:
                continue
            self._connection.edge.setFriction(edge_id, self.weather.surface_friction)
            self._weather_edges.add(edge_id)

    def _civilian_time_loss(self) -> dict[str, float]:
        assert self._connection is not None
        return {
            vehicle_id: float(self._connection.vehicle.getTimeLoss(vehicle_id))
            for vehicle_id in self._connection.vehicle.getIDList()
            if vehicle_id.startswith("civilian_")
        }

    @staticmethod
    def _mean_time_loss_delta(
        before: dict[str, float], after: dict[str, float]
    ) -> float:
        vehicle_ids = set(before) | set(after)
        if not vehicle_ids:
            return 0.0
        return float(np.mean([
            max(0.0, after.get(vehicle_id, before.get(vehicle_id, 0.0)) - before.get(vehicle_id, 0.0))
            for vehicle_id in vehicle_ids
        ]))


def astana_parallel_env(
    *,
    backend_kwargs: dict[str, Any] | None = None,
    **environment_kwargs: Any,
) -> EmergencyLogisticsParallelEnv:
    """Create a multi-call PettingZoo environment on the full Astana map."""
    backend = AstanaCityBackend(**(backend_kwargs or {}))
    requested_calls = environment_kwargs.pop("episode_calls", backend.episode_calls)
    if requested_calls != backend.episode_calls:
        raise ValueError("episode_calls must match AstanaCityBackend.episode_calls")
    return EmergencyLogisticsParallelEnv(
        max_units=backend.max_units,
        max_routes=backend.max_routes,
        episode_calls=backend.episode_calls,
        backend=backend,
        **environment_kwargs,
    )
