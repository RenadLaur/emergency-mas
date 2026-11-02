"""A small, auditable emergency priority state machine."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PriorityState(StrEnum):
    NORMAL = "normal"
    CLEARING = "clearing"
    PRIORITY = "priority"


@dataclass(frozen=True)
class PriorityCommand:
    action: str
    state: PriorityState


class EmergencyPriorityController:
    """Give a requested movement priority only after an all-red clearance time."""

    def __init__(self, clearance_seconds: float = 3.0):
        if clearance_seconds <= 0:
            raise ValueError("clearance_seconds must be positive")
        self.clearance_seconds = clearance_seconds
        self.state = PriorityState.NORMAL
        self._started_at: float | None = None

    def step(self, time_seconds: float, approaching: bool, passed: bool) -> PriorityCommand | None:
        if self.state is PriorityState.NORMAL and approaching:
            self.state = PriorityState.CLEARING
            self._started_at = time_seconds
            return PriorityCommand("set_all_red", self.state)
        if self.state is PriorityState.CLEARING:
            assert self._started_at is not None
            if time_seconds - self._started_at >= self.clearance_seconds:
                self.state = PriorityState.PRIORITY
                return PriorityCommand("grant_priority", self.state)
        if self.state is PriorityState.PRIORITY and passed:
            self.state = PriorityState.NORMAL
            self._started_at = None
            return PriorityCommand("restore_program", self.state)
        return None
