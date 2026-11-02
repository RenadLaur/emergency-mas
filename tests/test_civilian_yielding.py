from agents.driver.civilian_yielding import CivilianYieldingModel, profiles_for


def test_profiles_repeat_for_same_seed_and_change_for_another_seed() -> None:
    ids = ["civilian_001", "civilian_002"]
    assert profiles_for(ids, 42) == profiles_for(ids, 42)
    assert profiles_for(ids, 42) != profiles_for(ids, 43)


def test_impossible_yield_is_logged_without_action() -> None:
    profiles = profiles_for(["civilian_001"], 42)
    model = CivilianYieldingModel(profiles, 42, 100)
    events = model.observe("civilian_001", "emergency_00", 10, 20, False)
    assert events[0]["event"] == "yield_impossible"
    assert model.due_actions(20) == []
    assert model.counts["yield_impossible"] == 1


def test_yield_decision_is_created_only_once() -> None:
    profiles = profiles_for(["civilian_001"], 42)
    model = CivilianYieldingModel(profiles, 42, 100)
    model.observe("civilian_001", "emergency_00", 10, 20, True)
    assert model.observe("civilian_001", "emergency_00", 11, 10, True) == []
    assert model.counts["yield_detections"] == 1
