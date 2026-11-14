import pytest

from agents.incident.safety import SafetyMonitor


def test_ttc_near_miss_is_recorded_once_per_continuous_risk_episode() -> None:
    monitor = SafetyMonitor(2.0)
    first = monitor.observe("ambulance", "civilian", gap_meters=10, ego_speed_mps=15, leader_speed_mps=10)
    repeated = monitor.observe("ambulance", "civilian", gap_meters=8, ego_speed_mps=15, leader_speed_mps=10)

    assert first is not None
    assert first.ttc_seconds == 2.0
    assert repeated is None
    assert monitor.summary() == {"near_misses": 1, "minimum_ttc_seconds": 1.6}


def test_ttc_requires_a_positive_closing_speed_and_resets_after_safety() -> None:
    monitor = SafetyMonitor(2.0)
    assert monitor.observe("ambulance", "civilian", 3, 10, 10) is None
    assert monitor.observe("ambulance", "civilian", 3, 14, 10) is not None
    assert monitor.observe("ambulance", "civilian", 10, 11, 10) is None
    assert monitor.observe("ambulance", "civilian", 3, 14, 10) is not None
    assert monitor.summary()["near_misses"] == 2


def test_ttc_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError, match="ttc_threshold_seconds"):
        SafetyMonitor(0)
