import pytest

from agents.communication.message_bus import CommunicationBus


def test_messages_are_delivered_only_after_their_latency() -> None:
    bus = CommunicationBus()
    first = bus.send("incident_report", "sensor", "dispatcher", 10, 2)
    second = bus.send("reroute_command", "dispatcher", "ambulance", 10, 1)
    assert bus.deliver_due(10.9) == []
    assert bus.deliver_due(11) == [second]
    assert bus.deliver_due(12) == [first]


def test_message_latency_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="latency"):
        CommunicationBus().send("x", "a", "b", 0, -1)
