"""A small simulation-time message queue; it is not a user-interface chat."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentMessage:
    message_id: str
    kind: str
    sender: str
    recipient: str
    sent_at_seconds: float
    delivered_at_seconds: float
    payload: dict[str, Any]


class CommunicationBus:
    """Deliver explicitly delayed agent messages in deterministic order."""

    def __init__(self) -> None:
        self._pending: list[AgentMessage] = []
        self._next_id = 0

    def send(self, kind: str, sender: str, recipient: str, time_seconds: float,
             latency_seconds: float, payload: dict[str, Any] | None = None) -> AgentMessage:
        if latency_seconds < 0:
            raise ValueError("latency_seconds must not be negative")
        message = AgentMessage(f"message_{self._next_id:04d}", kind, sender, recipient,
                               time_seconds, time_seconds + latency_seconds, payload or {})
        self._next_id += 1
        self._pending.append(message)
        return message

    def deliver_due(self, time_seconds: float) -> list[AgentMessage]:
        due = [message for message in self._pending if message.delivered_at_seconds <= time_seconds]
        self._pending = [message for message in self._pending if message.delivered_at_seconds > time_seconds]
        return sorted(due, key=lambda message: (message.delivered_at_seconds, message.message_id))
