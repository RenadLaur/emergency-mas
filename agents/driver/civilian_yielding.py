"""Transparent, seeded civilian response to a nearby emergency vehicle.

This is intentionally a behavioural approximation, not a claim about real
driver psychology. SUMO still owns car-following and collision avoidance.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CivilianProfile:
    yield_probability: float
    reaction_seconds: float
    aggressiveness: float
    attention: float


@dataclass(frozen=True)
class YieldAction:
    civilian_id: str
    emergency_id: str
    due_time_seconds: float


def _seed(root_seed: int, *parts: str) -> int:
    payload = ":".join((str(root_seed), *parts)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def profiles_for(vehicle_ids: list[str], root_seed: int) -> dict[str, CivilianProfile]:
    """Create stable individual profiles without relying on Python hash randomization."""
    profiles: dict[str, CivilianProfile] = {}
    for vehicle_id in sorted(vehicle_ids):
        rng = random.Random(_seed(root_seed, "profile", vehicle_id))
        profiles[vehicle_id] = CivilianProfile(
            yield_probability=round(min(.95, max(.20, rng.gauss(.72, .12))), 4),
            reaction_seconds=round(min(4.0, max(.4, rng.gauss(1.4, .35))), 3),
            aggressiveness=round(rng.uniform(0.0, 1.0), 4),
            attention=round(rng.uniform(.45, 1.0), 4),
        )
    return profiles


class CivilianYieldingModel:
    """Turns first detection into one scheduled, deterministic yielding decision."""

    def __init__(self, profiles: dict[str, CivilianProfile], root_seed: int, detection_range_meters: float):
        self.profiles = profiles
        self.root_seed = root_seed
        self.detection_range_meters = detection_range_meters
        self._seen: set[tuple[str, str]] = set()
        self._pending: list[YieldAction] = []
        self.counts = {"yield_detections": 0, "yield_attempts": 0, "yield_declined": 0, "yield_impossible": 0,
                       "yield_actions_applied": 0, "yield_actions_failed": 0}

    def observe(self, civilian_id: str, emergency_id: str, time_seconds: float,
                distance_meters: float, physically_possible: bool) -> list[dict[str, object]]:
        key = (civilian_id, emergency_id)
        if key in self._seen or distance_meters > self.detection_range_meters:
            return []
        self._seen.add(key)
        self.counts["yield_detections"] += 1
        profile = self.profiles[civilian_id]
        event: dict[str, object] = {"time_seconds": time_seconds, "event": "yield_detected",
                                    "vehicle_id": civilian_id, "emergency_id": emergency_id,
                                    "distance_meters": round(distance_meters, 2),
                                    "physically_possible": physically_possible}
        if not physically_possible:
            self.counts["yield_impossible"] += 1
            event["event"] = "yield_impossible"
            return [event]
        effective_probability = profile.yield_probability * profile.attention * (1 - .35 * profile.aggressiveness)
        draw = random.Random(_seed(self.root_seed, "decision", civilian_id, emergency_id)).random()
        if draw >= effective_probability:
            self.counts["yield_declined"] += 1
            event["event"] = "yield_declined"
            event["effective_probability"] = round(effective_probability, 4)
            return [event]
        action = YieldAction(civilian_id, emergency_id, time_seconds + profile.reaction_seconds)
        self._pending.append(action)
        self.counts["yield_attempts"] += 1
        event["event"] = "yield_scheduled"
        event["due_time_seconds"] = round(action.due_time_seconds, 3)
        return [event]

    def due_actions(self, time_seconds: float) -> list[YieldAction]:
        due = [action for action in self._pending if action.due_time_seconds <= time_seconds]
        self._pending = [action for action in self._pending if action.due_time_seconds > time_seconds]
        return due

    def record_action(self, applied: bool) -> None:
        self.counts["yield_actions_applied" if applied else "yield_actions_failed"] += 1

    def profile_rows(self) -> dict[str, dict[str, float]]:
        return {vehicle_id: asdict(profile) for vehicle_id, profile in self.profiles.items()}
