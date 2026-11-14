"""Small, explicit weather model for reproducible SUMO scenarios.

The defaults are experimental sensitivity parameters, not calibrated local
weather observations. A scenario may override them after a documented source
or calibration is available.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class WeatherCondition(StrEnum):
    DRY = "dry"
    RAIN = "rain"
    SNOW = "snow"
    ICE = "ice"
    FOG = "fog"


@dataclass(frozen=True)
class WeatherProfile:
    condition: WeatherCondition
    surface_friction: float
    speed_factor: float
    visibility_meters: float
    reaction_time_multiplier: float

    def __post_init__(self) -> None:
        if not 0 < self.surface_friction <= 1:
            raise ValueError("surface_friction must be in (0, 1]")
        if not 0 < self.speed_factor <= 1:
            raise ValueError("speed_factor must be in (0, 1]")
        if self.visibility_meters <= 0:
            raise ValueError("visibility_meters must be positive")
        if self.reaction_time_multiplier < 1:
            raise ValueError("reaction_time_multiplier must be at least 1")


DEFAULT_PROFILES: dict[WeatherCondition, WeatherProfile] = {
    WeatherCondition.DRY: WeatherProfile(WeatherCondition.DRY, 1.00, 1.00, 250.0, 1.00),
    WeatherCondition.RAIN: WeatherProfile(WeatherCondition.RAIN, .75, .88, 120.0, 1.15),
    WeatherCondition.SNOW: WeatherProfile(WeatherCondition.SNOW, .55, .70, 80.0, 1.35),
    WeatherCondition.ICE: WeatherProfile(WeatherCondition.ICE, .25, .45, 40.0, 1.65),
    WeatherCondition.FOG: WeatherProfile(WeatherCondition.FOG, .90, .78, 35.0, 1.45),
}


def profile_from_config(config: dict[str, Any] | None) -> WeatherProfile:
    """Resolve a condition and optional auditable scenario overrides."""
    config = config or {}
    try:
        condition = WeatherCondition(config.get("condition", WeatherCondition.DRY))
    except ValueError as error:
        valid = ", ".join(item.value for item in WeatherCondition)
        raise ValueError(f"weather.condition must be one of: {valid}") from error
    default = DEFAULT_PROFILES[condition]
    return WeatherProfile(
        condition=condition,
        surface_friction=float(config.get("surface_friction", default.surface_friction)),
        speed_factor=float(config.get("speed_factor", default.speed_factor)),
        visibility_meters=float(config.get("visibility_meters", default.visibility_meters)),
        reaction_time_multiplier=float(config.get("reaction_time_multiplier", default.reaction_time_multiplier)),
    )
