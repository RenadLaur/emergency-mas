from agents.traffic_light.priority import EmergencyPriorityController, PriorityState


def test_priority_sequence_includes_clearance_and_restoration() -> None:
    controller = EmergencyPriorityController(clearance_seconds=3)
    first = controller.step(10, approaching=True, passed=False)
    assert first and first.action == "set_all_red"
    assert controller.state is PriorityState.CLEARING
    assert controller.step(12, approaching=True, passed=False) is None
    second = controller.step(13, approaching=True, passed=False)
    assert second and second.action == "grant_priority"
    assert controller.state is PriorityState.PRIORITY
    final = controller.step(14, approaching=False, passed=True)
    assert final and final.action == "restore_program"
    assert controller.state is PriorityState.NORMAL
