import pytest

from agents.driver.emergency_state import EmergencyDriverModel, profile_for


def test_driver_profile_is_deterministic_for_a_seed_and_identifier() -> None:
    assert profile_for("ambulance_00", 42) == profile_for("ambulance_00", 42)
    assert profile_for("ambulance_00", 42) != profile_for("ambulance_01", 42)


def test_moving_driver_accumulates_state_and_reduces_speed_factor() -> None:
    model = EmergencyDriverModel(42, weather_reaction_multiplier=1.35)
    _, initial = model.register("ambulance_00")
    final = model.advance("ambulance_00", 900, speed_mps=15, allowed_speed_mps=16.67)

    assert final.elapsed_driving_seconds == 900
    assert final.stress > initial.stress
    assert final.fatigue > initial.fatigue
    assert final.attention < initial.attention
    assert final.reaction_seconds > initial.reaction_seconds
    assert final.speed_factor < 1


def test_stationary_driver_recovers_stress_without_reducing_fatigue() -> None:
    model = EmergencyDriverModel(42)
    moving = model.advance("ambulance_00", 600, speed_mps=15, allowed_speed_mps=16.67)
    stopped = model.advance("ambulance_00", 600, speed_mps=0, allowed_speed_mps=16.67)

    assert stopped.stress < moving.stress
    assert stopped.fatigue == moving.fatigue


def test_weather_reaction_multiplier_is_bounded() -> None:
    with pytest.raises(ValueError, match="weather_reaction_multiplier"):
        EmergencyDriverModel(42, .9)
