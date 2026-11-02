"""Deterministic nearest-free dispatcher, independent of SUMO and TraCI."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable


class UnitStatus(StrEnum):
    AVAILABLE = "available"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


class CallStatus(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class EmergencyUnit:
    unit_id: str
    edge_id: str
    status: UnitStatus = UnitStatus.AVAILABLE
    active_call_id: str | None = None


@dataclass
class EmergencyCall:
    call_id: str
    created_time_seconds: float
    destination_edge_id: str
    status: CallStatus = CallStatus.PENDING
    assigned_unit_id: str | None = None
    completed_time_seconds: float | None = None
    assignments: list["Assignment"] = field(default_factory=list)


@dataclass(frozen=True)
class Assignment:
    call_id: str
    unit_id: str
    path: tuple[str, ...]
    estimated_seconds: float
    attempt: int


RouteProvider = Callable[[EmergencyUnit, EmergencyCall], tuple[tuple[str, ...], float] | None]


class NearestFreeDispatcher:
    """Assign the feasible free unit with the lowest route-cost estimate."""

    def __init__(self, units: list[EmergencyUnit]):
        if len({unit.unit_id for unit in units}) != len(units):
            raise ValueError("Dispatcher unit IDs must be unique")
        self.units = {unit.unit_id: unit for unit in units}
        self.calls: dict[str, EmergencyCall] = {}

    def create_call(self, call: EmergencyCall) -> None:
        if call.call_id in self.calls:
            raise ValueError(f"Duplicate call ID: {call.call_id}")
        self.calls[call.call_id] = call

    def assign(self, call_id: str, route_provider: RouteProvider) -> Assignment | None:
        call = self.calls[call_id]
        if call.status not in {CallStatus.PENDING, CallStatus.ASSIGNED}:
            return None
        candidates: list[tuple[float, str, tuple[str, ...]]] = []
        for unit in self.units.values():
            if unit.status is not UnitStatus.AVAILABLE:
                continue
            route = route_provider(unit, call)
            if route is not None:
                path, estimate = route
                candidates.append((estimate, unit.unit_id, path))
        if not candidates:
            call.status = CallStatus.PENDING
            return None
        estimate, unit_id, path = min(candidates, key=lambda row: (row[0], row[1]))
        unit = self.units[unit_id]
        assignment = Assignment(call_id, unit_id, path, estimate, len(call.assignments) + 1)
        call.assignments.append(assignment)
        call.status = CallStatus.ASSIGNED
        call.assigned_unit_id = unit_id
        unit.status = UnitStatus.BUSY
        unit.active_call_id = call_id
        return assignment

    def mark_unavailable(self, unit_id: str) -> EmergencyCall | None:
        unit = self.units[unit_id]
        affected = self.calls.get(unit.active_call_id) if unit.active_call_id else None
        unit.status = UnitStatus.UNAVAILABLE
        unit.active_call_id = None
        if affected and affected.status is CallStatus.ASSIGNED:
            affected.status = CallStatus.PENDING
            affected.assigned_unit_id = None
        return affected

    def complete(self, unit_id: str, time_seconds: float) -> EmergencyCall:
        unit = self.units[unit_id]
        if unit.active_call_id is None:
            raise ValueError(f"Unit {unit_id} has no active call")
        call = self.calls[unit.active_call_id]
        call.status = CallStatus.COMPLETED
        call.completed_time_seconds = time_seconds
        unit.status = UnitStatus.AVAILABLE
        unit.edge_id = call.destination_edge_id
        unit.active_call_id = None
        return call
