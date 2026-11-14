import pytest

from agents.incident.triage import IncidentAssessment, IncidentClass, ResponsePlanner


def test_minor_collision_assigns_police_but_not_medical_or_rescue_units() -> None:
    plan = ResponsePlanner().plan(IncidentAssessment("minor", blocked_lanes=1))
    assert plan.incident_class is IncidentClass.MINOR
    assert (plan.police_units, plan.ambulance_units, plan.fire_rescue_units) == (1, 0, 0)
    assert "road_blockage" in plan.reasons


def test_possible_injuries_scale_ambulances_and_keep_police() -> None:
    plan = ResponsePlanner().plan(IncidentAssessment("multiple", estimated_injured=2))
    assert plan.incident_class is IncidentClass.MULTI_CASUALTY
    assert (plan.police_units, plan.ambulance_units, plan.fire_rescue_units) == (1, 2, 0)


def test_trapping_or_fire_escalates_to_fire_rescue() -> None:
    plan = ResponsePlanner().plan(
        IncidentAssessment("hazard", estimated_injured=1, people_trapped=True, fire_or_smoke=True)
    )
    assert plan.incident_class is IncidentClass.RESCUE_HAZARD
    assert (plan.police_units, plan.ambulance_units, plan.fire_rescue_units) == (1, 1, 1)
    assert {"people_trapped", "fire_or_smoke"}.issubset(plan.reasons)


def test_speed_is_audit_data_not_an_unsupported_medical_decision() -> None:
    plan = ResponsePlanner().plan(IncidentAssessment("speed_only", impact_speed_mps=35.0))
    assert plan.incident_class is IncidentClass.MINOR
    assert (plan.ambulance_units, plan.fire_rescue_units) == (0, 0)


@pytest.mark.parametrize("field, value", [("estimated_injured", -1), ("blocked_lanes", -1),
                                             ("impact_speed_mps", -0.1)])
def test_negative_inputs_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        IncidentAssessment("bad", **{field: value})
