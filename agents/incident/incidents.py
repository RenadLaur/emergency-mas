"""Deterministic road-closure incidents, independent from SUMO."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class IncidentStatus(StrEnum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    RESOLVED = "resolved"


@dataclass
class RoadClosureIncident:
    incident_id: str
    lane_ids: tuple[str, ...]
    starts_at_seconds: float
    ends_at_seconds: float | None = None
    status: IncidentStatus = IncidentStatus.SCHEDULED


class IncidentManager:
    """Activates and resolves lane closures exactly once."""

    def __init__(self, incidents: list[RoadClosureIncident]):
        if len({incident.incident_id for incident in incidents}) != len(incidents):
            raise ValueError("Incident IDs must be unique")
        self.incidents = {incident.incident_id: incident for incident in incidents}

    def activate_due(self, time_seconds: float) -> list[RoadClosureIncident]:
        activated: list[RoadClosureIncident] = []
        for incident in self.incidents.values():
            if incident.status is IncidentStatus.SCHEDULED and time_seconds >= incident.starts_at_seconds:
                incident.status = IncidentStatus.ACTIVE
                activated.append(incident)
        return activated

    def resolve_due(self, time_seconds: float) -> list[RoadClosureIncident]:
        resolved: list[RoadClosureIncident] = []
        for incident in self.incidents.values():
            if (incident.status is IncidentStatus.ACTIVE and incident.ends_at_seconds is not None
                    and time_seconds >= incident.ends_at_seconds):
                incident.status = IncidentStatus.RESOLVED
                resolved.append(incident)
        return resolved

    def closed_lanes(self) -> set[str]:
        return {lane_id for incident in self.incidents.values() if incident.status is IncidentStatus.ACTIVE
                for lane_id in incident.lane_ids}
