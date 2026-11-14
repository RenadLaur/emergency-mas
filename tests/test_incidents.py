from agents.incident.incidents import IncidentManager, IncidentStatus, RoadClosureIncident
from agents.routing.routing import route_avoids_closed_lanes


def test_incident_lifecycle_and_closed_lane_view() -> None:
    incident = RoadClosureIncident("crash", ("upper_out_0",), starts_at_seconds=10, ends_at_seconds=20)
    manager = IncidentManager([incident])
    assert manager.activate_due(9) == []
    assert manager.activate_due(10) == [incident]
    assert incident.status is IncidentStatus.ACTIVE
    assert manager.closed_lanes() == {"upper_out_0"}
    assert manager.resolve_due(20) == [incident]
    assert incident.status is IncidentStatus.RESOLVED
    assert manager.closed_lanes() == set()


def test_route_validation_rejects_closed_edge() -> None:
    assert not route_avoids_closed_lanes(("start", "upper_in", "upper_out", "finish"), {"upper_out_0"})
    assert route_avoids_closed_lanes(("start", "lower_in", "lower_out", "finish"), {"upper_out_0"})
