import pytest

from simulation.weather.model import WeatherCondition, profile_from_config


def test_weather_defaults_have_progressively_lower_grip_than_dry() -> None:
    dry = profile_from_config({"condition": "dry"})
    rain = profile_from_config({"condition": "rain"})
    snow = profile_from_config({"condition": "snow"})
    ice = profile_from_config({"condition": "ice"})

    assert dry.surface_friction > rain.surface_friction > snow.surface_friction > ice.surface_friction
    assert dry.speed_factor > rain.speed_factor > snow.speed_factor > ice.speed_factor


def test_fog_limits_visibility_without_implying_icy_road() -> None:
    fog = profile_from_config({"condition": WeatherCondition.FOG.value})
    assert fog.visibility_meters < 100
    assert fog.surface_friction > .5


def test_weather_config_rejects_unknown_condition_and_invalid_friction() -> None:
    with pytest.raises(ValueError, match="weather.condition"):
        profile_from_config({"condition": "hail"})
    with pytest.raises(ValueError, match="surface_friction"):
        profile_from_config({"condition": "rain", "surface_friction": 0})
