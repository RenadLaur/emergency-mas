"""Explainable TTC and near-miss monitoring independent of SUMO."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class NearMissEvent:
    ego_vehicle_id: str
    leader_vehicle_id: str
    gap_meters: float
    closing_speed_mps: float
    ttc_seconds: float


class SafetyMonitor:
    """Detect distinct TTC threshold crossings without inventing collisions."""

    def __init__(self, ttc_threshold_seconds: float):
        if ttc_threshold_seconds <= 0:
            raise ValueError("ttc_threshold_seconds must be positive")
        self.ttc_threshold_seconds = ttc_threshold_seconds
        self._active_risk_pairs: set[tuple[str, str]] = set()
        self.near_misses: list[NearMissEvent] = []
        self.minimum_ttc_seconds: float | None = None

    def observe(self, ego_vehicle_id: str, leader_vehicle_id: str | None,
                gap_meters: float | None, ego_speed_mps: float, leader_speed_mps: float) -> NearMissEvent | None:
        """Observe one leader relationship and return a new near-miss, if any."""
        self._clear_ego_except(ego_vehicle_id, leader_vehicle_id)
        if leader_vehicle_id is None or gap_meters is None or gap_meters < 0:
            return None
        key = (ego_vehicle_id, leader_vehicle_id)
        closing_speed = ego_speed_mps - leader_speed_mps
        if closing_speed <= 0:
            self._active_risk_pairs.discard(key)
            return None
        ttc = gap_meters / closing_speed
        self.minimum_ttc_seconds = ttc if self.minimum_ttc_seconds is None else min(self.minimum_ttc_seconds, ttc)
        if ttc > self.ttc_threshold_seconds:
            self._active_risk_pairs.discard(key)
            return None
        if key in self._active_risk_pairs:
            return None
        self._active_risk_pairs.add(key)
        event = NearMissEvent(ego_vehicle_id, leader_vehicle_id, round(gap_meters, 3),
                               round(closing_speed, 3), round(ttc, 3))
        self.near_misses.append(event)
        return event

    def _clear_ego_except(self, ego_vehicle_id: str, leader_vehicle_id: str | None) -> None:
        self._active_risk_pairs = {
            pair for pair in self._active_risk_pairs
            if pair[0] != ego_vehicle_id or pair[1] == leader_vehicle_id
        }

    def summary(self) -> dict[str, float | int | None]:
        return {"near_misses": len(self.near_misses), "minimum_ttc_seconds": self.minimum_ttc_seconds}


def near_miss_payload(event: NearMissEvent) -> dict[str, float | str]:
    """Keep runner event logs independent of the monitor's internal representation."""
    return asdict(event)
