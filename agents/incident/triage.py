"""Explainable, non-clinical dispatch triage for simulated road incidents.

The model chooses resources for a simulation.  It does not diagnose people or
claim to reproduce the operational protocol of a particular emergency service.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class IncidentClass(StrEnum):
    MINOR = "minor"
    INJURY = "injury"
    MULTI_CASUALTY = "multi_casualty"
    RESCUE_HAZARD = "rescue_hazard"


@dataclass(frozen=True)
class IncidentAssessment:
    """Observable incident consequences supplied by SUMO or a scenario.

    ``impact_speed_mps`` is retained for audit and later calibration. It must
    not on its own decide that people are injured or that a fire engine is
    required.
    """

    incident_id: str
    estimated_injured: int = 0
    impact_speed_mps: float | None = None
    people_trapped: bool = False
    fire_or_smoke: bool = False
    hazardous_material: bool = False
    blocked_lanes: int = 0

    def __post_init__(self) -> None:
        if self.estimated_injured < 0:
            raise ValueError("estimated_injured must not be negative")
        if self.impact_speed_mps is not None and self.impact_speed_mps < 0:
            raise ValueError("impact_speed_mps must not be negative")
        if self.blocked_lanes < 0:
            raise ValueError("blocked_lanes must not be negative")


@dataclass(frozen=True)
class ResponsePlan:
    """Resources and auditable reasons selected by the dispatcher policy."""

    incident_id: str
    incident_class: IncidentClass
    police_units: int
    ambulance_units: int
    fire_rescue_units: int
    reasons: tuple[str, ...]


class ResponsePlanner:
    """Map observable risk indicators to a deliberately small MVP response plan."""

    def plan(self, assessment: IncidentAssessment) -> ResponsePlan:
        reasons = ["scene_safety_and_traffic_management"]
        ambulance_units = 0
        fire_rescue_units = 0

        if assessment.estimated_injured:
            ambulance_units = assessment.estimated_injured
            reasons.append("suspected_injured")
        if assessment.people_trapped:
            fire_rescue_units = 1
            reasons.append("people_trapped")
        if assessment.fire_or_smoke:
            fire_rescue_units = 1
            reasons.append("fire_or_smoke")
        if assessment.hazardous_material:
            fire_rescue_units = 1
            reasons.append("hazardous_material")
        if assessment.blocked_lanes:
            reasons.append("road_blockage")

        if fire_rescue_units:
            incident_class = IncidentClass.RESCUE_HAZARD
        elif assessment.estimated_injured > 1:
            incident_class = IncidentClass.MULTI_CASUALTY
        elif assessment.estimated_injured:
            incident_class = IncidentClass.INJURY
        else:
            incident_class = IncidentClass.MINOR

        return ResponsePlan(
            incident_id=assessment.incident_id,
            incident_class=incident_class,
            police_units=1,
            ambulance_units=ambulance_units,
            fire_rescue_units=fire_rescue_units,
            reasons=tuple(reasons),
        )
