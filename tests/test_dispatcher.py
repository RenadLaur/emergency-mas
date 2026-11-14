from agents.dispatcher.dispatcher import (CallStatus, EmergencyCall, EmergencyUnit,
                                          NearestFreeDispatcher, UnitStatus, UnitType)


def route_provider(unit: EmergencyUnit, call: EmergencyCall):
    costs = {("unit_a", "call"): 30, ("unit_b", "call"): 10}
    cost = costs.get((unit.unit_id, call.call_id))
    return ((unit.edge_id, call.destination_edge_id), cost) if cost is not None else None


def test_nearest_free_unit_is_assigned() -> None:
    dispatcher = NearestFreeDispatcher([EmergencyUnit("unit_a", "a"), EmergencyUnit("unit_b", "b")])
    dispatcher.create_call(EmergencyCall("call", 0, "incident"))
    assignment = dispatcher.assign("call", route_provider)
    assert assignment and assignment.unit_id == "unit_b"
    assert dispatcher.units["unit_b"].status is UnitStatus.BUSY


def test_unavailable_unit_triggers_reassignment() -> None:
    dispatcher = NearestFreeDispatcher([EmergencyUnit("unit_a", "a"), EmergencyUnit("unit_b", "b")])
    dispatcher.create_call(EmergencyCall("call", 0, "incident"))
    first = dispatcher.assign("call", route_provider)
    affected = dispatcher.mark_unavailable(first.unit_id)
    second = dispatcher.assign(affected.call_id, route_provider)
    assert second and second.unit_id == "unit_a"
    assert second.attempt == 2
    assert dispatcher.calls["call"].status is CallStatus.ASSIGNED


def test_completion_returns_unit_to_available_at_incident() -> None:
    dispatcher = NearestFreeDispatcher([EmergencyUnit("unit_a", "a")])
    dispatcher.create_call(EmergencyCall("call", 0, "incident"))
    dispatcher.assign("call", lambda unit, call: ((unit.edge_id, call.destination_edge_id), 5))
    call = dispatcher.complete("unit_a", 10)
    assert call.status is CallStatus.COMPLETED
    assert dispatcher.units["unit_a"].status is UnitStatus.AVAILABLE
    assert dispatcher.units["unit_a"].edge_id == "incident"


def test_call_only_uses_a_matching_resource_type() -> None:
    dispatcher = NearestFreeDispatcher([
        EmergencyUnit("ambulance_00", "a", unit_type=UnitType.AMBULANCE),
        EmergencyUnit("police_00", "b", unit_type=UnitType.POLICE),
    ])
    dispatcher.create_call(EmergencyCall("scene", 0, "incident", required_unit_type=UnitType.POLICE))

    assignment = dispatcher.assign("scene", lambda unit, call: ((unit.edge_id, call.destination_edge_id), 1))

    assert assignment is not None
    assert assignment.unit_id == "police_00"
    assert assignment.unit_type is UnitType.POLICE


def test_call_stays_pending_when_its_resource_type_is_unavailable() -> None:
    dispatcher = NearestFreeDispatcher([EmergencyUnit("ambulance_00", "a")])
    dispatcher.create_call(EmergencyCall("scene", 0, "incident", required_unit_type=UnitType.FIRE_RESCUE))

    assert dispatcher.assign("scene", lambda unit, call: ((unit.edge_id, call.destination_edge_id), 1)) is None
    assert dispatcher.calls["scene"].status is CallStatus.PENDING
