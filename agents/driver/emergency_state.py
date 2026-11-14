"""Seeded, explainable state evolution for emergency-vehicle drivers.

SUMO remains responsible for collision avoidance and vehicle dynamics. This
module represents only the driver's operational readiness and derives a
conservative desired-speed multiplier from it.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass


def _seed(root_seed: int, *parts: str) -> int:
    payload = ":".join((str(root_seed), *parts)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


@dataclass(frozen=True)
class EmergencyDriverProfile:
    """Stable individual traits drawn once from the root seed."""

    baseline_attention: float
    baseline_reaction_seconds: float
    stress_resilience: float
    fatigue_rate_per_hour: float


@dataclass(frozen=True)
class EmergencyDriverState:
    """Operational readiness at one point during an emergency mission."""

    elapsed_driving_seconds: float
    stress: float
    fatigue: float
    attention: float
    reaction_seconds: float
    speed_factor: float


def profile_for(driver_id: str, root_seed: int) -> EmergencyDriverProfile:
    """Generate one deterministic profile without Python hash randomization."""
    rng = random.Random(_seed(root_seed, "emergency_driver", driver_id))
    return EmergencyDriverProfile(
        baseline_attention=round(rng.uniform(.78, .98), 4),
        baseline_reaction_seconds=round(rng.uniform(.55, 1.15), 3),
        stress_resilience=round(rng.uniform(.35, .95), 4),
        fatigue_rate_per_hour=round(rng.uniform(.08, .22), 4),
    )


class EmergencyDriverModel:
    """Maintain driver state and produce a bounded conservative speed factor."""

    def __init__(self, root_seed: int, weather_reaction_multiplier: float = 1.0):
        if weather_reaction_multiplier < 1:
            raise ValueError("weather_reaction_multiplier must be at least 1")
        self.root_seed = root_seed
        self.weather_reaction_multiplier = weather_reaction_multiplier
        self.profiles: dict[str, EmergencyDriverProfile] = {}
        self.states: dict[str, EmergencyDriverState] = {}

    def register(self, driver_id: str) -> tuple[EmergencyDriverProfile, EmergencyDriverState]:
        if driver_id in self.profiles:
            return self.profiles[driver_id], self.states[driver_id]
        profile = profile_for(driver_id, self.root_seed)
        state = EmergencyDriverState(
            elapsed_driving_seconds=0.0,
            stress=.15,
            fatigue=0.0,
            attention=profile.baseline_attention,
            reaction_seconds=round(profile.baseline_reaction_seconds * self.weather_reaction_multiplier, 3),
            speed_factor=1.0,
        )
        self.profiles[driver_id] = profile
        self.states[driver_id] = state
        return profile, state

    def advance(self, driver_id: str, delta_seconds: float, speed_mps: float,
                allowed_speed_mps: float, urgency: float = 1.0) -> EmergencyDriverState:
        """Advance a driver while its vehicle is active.

        Stress rises most under sustained urgent high-speed movement and is
        dampened by individual resilience. Fatigue accumulates from moving time.
        Both conservatively reduce attention and the desired-speed multiplier.
        """
        if delta_seconds < 0:
            raise ValueError("delta_seconds must not be negative")
        profile, previous = self.register(driver_id)
        moving = speed_mps > .2
        speed_ratio = _clamp(speed_mps / max(allowed_speed_mps, .1), 0.0, 1.5)
        stress_gain = delta_seconds / 3600 * .42 * urgency * (1 - profile.stress_resilience) * speed_ratio
        stress_recovery = delta_seconds / 3600 * .08 if not moving else 0.0
        fatigue_gain = delta_seconds / 3600 * profile.fatigue_rate_per_hour if moving else 0.0
        stress = _clamp(previous.stress + stress_gain - stress_recovery, 0.0, 1.0)
        fatigue = _clamp(previous.fatigue + fatigue_gain, 0.0, 1.0)
        attention = _clamp(profile.baseline_attention * (1 - .30 * stress - .45 * fatigue), .25, 1.0)
        reaction_seconds = profile.baseline_reaction_seconds * self.weather_reaction_multiplier
        reaction_seconds *= 1 + .45 * stress + .65 * fatigue + (1 - attention) * .35
        speed_factor = _clamp(1 - .18 * stress - .25 * fatigue, .65, 1.0)
        state = EmergencyDriverState(
            elapsed_driving_seconds=previous.elapsed_driving_seconds + (delta_seconds if moving else 0.0),
            stress=round(stress, 5), fatigue=round(fatigue, 5), attention=round(attention, 5),
            reaction_seconds=round(reaction_seconds, 4), speed_factor=round(speed_factor, 5),
        )
        self.states[driver_id] = state
        return state

    def rows(self) -> dict[str, dict[str, float]]:
        return {
            driver_id: {**asdict(self.profiles[driver_id]), **asdict(state)}
            for driver_id, state in self.states.items()
        }
