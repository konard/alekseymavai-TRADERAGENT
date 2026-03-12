"""Tests for CommitteeTelegramFeed."""

import pytest

from bot.agents.telegram_feed import CommitteeTelegramFeed
from bot.core.event_bus import EventBus, DomainEvent


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

def _make_decision_data(
    verdict: str = "approve",
    score: float = 0.742,
    symbol: str = "ETH/USDT",
) -> dict:
    return {
        "verdict": verdict,
        "score": score,
        "symbol": symbol,
        "reasoning": "Голосование: 3 за, 1 против...",
        "session_id": "session_abc123",
        "votes": [
            {
                "expert": "smc_structuralist",
                "verdict": "approve",
                "confidence": 0.82,
                "reasoning": "bullish OB",
            },
            {
                "expert": "trend_analyst",
                "verdict": "approve",
                "confidence": 0.71,
                "reasoning": "aligned",
            },
            {
                "expert": "risk_manager",
                "verdict": "defer",
                "confidence": 0.50,
                "reasoning": "neutral",
            },
            {
                "expert": "contrarian",
                "verdict": "reject",
                "confidence": 0.65,
                "reasoning": "volume div",
            },
        ],
        "arguments_count": 6,
        "contradictions_count": 1,
    }


def _make_anomaly_data() -> dict:
    return {
        "type": "VOLUME_SPIKE",
        "severity": 0.85,
        "z_score": 4.2,
        "value": 5000,
        "threshold": 3.0,
    }


# ------------------------------------------------------------------
# Format tests
# ------------------------------------------------------------------

class TestFormatDecision:

    def test_format_decision_approve(self):
        data = _make_decision_data(verdict="approve", score=0.742)
        msg = CommitteeTelegramFeed.format_decision(data)

        assert "APPROVE" in msg
        assert "✅" in msg
        assert "0.742" in msg
        assert "ETH/USDT" in msg
        assert "smc_structuralist" in msg
        assert "bullish OB" in msg
        assert "Contradictions: 1" in msg

    def test_format_decision_reject(self):
        data = _make_decision_data(verdict="reject", score=0.3)
        msg = CommitteeTelegramFeed.format_decision(data)

        assert "REJECT" in msg
        assert "❌" in msg
        assert "0.300" in msg

    def test_format_decision_defer(self):
        data = _make_decision_data(verdict="defer", score=0.5)
        msg = CommitteeTelegramFeed.format_decision(data)

        assert "DEFER" in msg
        assert "⏸" in msg

    def test_format_decision_no_votes(self):
        data = {"verdict": "defer", "score": 0.0, "votes": [], "contradictions_count": 0}
        msg = CommitteeTelegramFeed.format_decision(data)

        assert "DEFER" in msg
        assert "Expert Votes:" not in msg
        assert "Contradictions: 0" in msg

    def test_format_decision_no_symbol(self):
        data = _make_decision_data()
        del data["symbol"]
        msg = CommitteeTelegramFeed.format_decision(data)

        assert "Score: 0.742" in msg
        assert "Symbol" not in msg

    def test_format_decision_vote_tree_markers(self):
        data = _make_decision_data()
        msg = CommitteeTelegramFeed.format_decision(data)

        lines = msg.split("\n")
        vote_lines = [l for l in lines if l.startswith("├") or l.startswith("└")]
        assert len(vote_lines) == 4
        # Last vote uses └
        assert vote_lines[-1].startswith("└")
        # Others use ├
        for vl in vote_lines[:-1]:
            assert vl.startswith("├")


class TestFormatAnomaly:

    def test_format_anomaly(self):
        data = _make_anomaly_data()
        msg = CommitteeTelegramFeed.format_anomaly(data)

        assert "⚠️" in msg
        assert "VOLUME_SPIKE" in msg
        assert "0.85" in msg
        assert "4.2" in msg
        assert "5000" in msg
        assert "3.0" in msg


class TestFormatHalt:

    def test_format_halt(self):
        data = {"reason": "Loss streak: 3 consecutive losses"}
        msg = CommitteeTelegramFeed.format_halt(data)

        assert "🛑" in msg
        assert "HALTED" in msg
        assert "Loss streak: 3 consecutive losses" in msg


class TestFormatResume:

    def test_format_resume(self):
        data = {"reason": "Manual override"}
        msg = CommitteeTelegramFeed.format_resume(data)

        assert "RESUMED" in msg
        assert "Manual override" in msg

    def test_format_resume_no_reason(self):
        msg = CommitteeTelegramFeed.format_resume({})
        assert "RESUMED" in msg
        assert "Reason" not in msg


class TestFormatCalibration:

    def test_format_calibration(self):
        weights = {
            "SMC": {"old": 1.20, "new": 1.35},
            "Trend": {"old": 1.00, "new": 0.85},
            "Risk": {"old": 1.50, "new": 1.50},
            "Contrarian": {"old": 0.80, "new": 0.72},
        }
        msg = CommitteeTelegramFeed.format_calibration(weights)

        assert "⚖️" in msg
        assert "Recalibrated" in msg
        assert "SMC: 1.20 → 1.35 (+12.5%)" in msg
        assert "Trend: 1.00 → 0.85 (-15.0%)" in msg
        assert "(unchanged)" in msg
        assert "Contrarian: 0.80 → 0.72 (-10.0%)" in msg


# ------------------------------------------------------------------
# EventBus integration tests
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_called_on_decision():
    """Subscribe to bus, publish DECISION_MADE, callback receives formatted message."""
    bus = EventBus(bot_name="test")
    messages: list[str] = []

    async def mock_send(msg: str) -> None:
        messages.append(msg)

    feed = CommitteeTelegramFeed(event_bus=bus, send_callback=mock_send)
    await feed.start()

    event = DomainEvent(
        entity_type="session",
        entity_id="session_abc123",
        event_type="DECISION_MADE",
        data=_make_decision_data(),
    )
    await bus.publish(event)

    assert len(messages) == 1
    assert "APPROVE" in messages[0]
    assert "✅" in messages[0]
    assert "smc_structuralist" in messages[0]

    await feed.stop()


@pytest.mark.asyncio
async def test_callback_called_on_anomaly():
    bus = EventBus(bot_name="test")
    messages: list[str] = []

    async def mock_send(msg: str) -> None:
        messages.append(msg)

    feed = CommitteeTelegramFeed(event_bus=bus, send_callback=mock_send)
    await feed.start()

    event = DomainEvent(
        entity_type="risk",
        entity_id="anomaly_1",
        event_type="ANOMALY_DETECTED",
        data=_make_anomaly_data(),
    )
    await bus.publish(event)

    assert len(messages) == 1
    assert "VOLUME_SPIKE" in messages[0]

    await feed.stop()


@pytest.mark.asyncio
async def test_callback_called_on_halt():
    bus = EventBus(bot_name="test")
    messages: list[str] = []

    async def mock_send(msg: str) -> None:
        messages.append(msg)

    feed = CommitteeTelegramFeed(event_bus=bus, send_callback=mock_send)
    await feed.start()

    event = DomainEvent(
        entity_type="risk",
        entity_id="risk_1",
        event_type="RISK_HALTED",
        data={"reason": "Daily loss limit hit"},
    )
    await bus.publish(event)

    assert len(messages) == 1
    assert "HALTED" in messages[0]
    assert "Daily loss limit hit" in messages[0]

    await feed.stop()


@pytest.mark.asyncio
async def test_callback_called_on_resume():
    bus = EventBus(bot_name="test")
    messages: list[str] = []

    async def mock_send(msg: str) -> None:
        messages.append(msg)

    feed = CommitteeTelegramFeed(event_bus=bus, send_callback=mock_send)
    await feed.start()

    event = DomainEvent(
        entity_type="risk",
        entity_id="risk_1",
        event_type="RISK_RESUMED",
        data={"reason": "New trading day"},
    )
    await bus.publish(event)

    assert len(messages) == 1
    assert "RESUMED" in messages[0]

    await feed.stop()


@pytest.mark.asyncio
async def test_min_score_filter():
    """Low score decisions not reported when min_score_to_report > 0."""
    bus = EventBus(bot_name="test")
    messages: list[str] = []

    async def mock_send(msg: str) -> None:
        messages.append(msg)

    feed = CommitteeTelegramFeed(
        event_bus=bus,
        send_callback=mock_send,
        min_score_to_report=0.5,
    )
    await feed.start()

    # Low score — should be filtered out
    event_low = DomainEvent(
        entity_type="session",
        entity_id="s1",
        event_type="DECISION_MADE",
        data=_make_decision_data(score=0.3),
    )
    await bus.publish(event_low)
    assert len(messages) == 0

    # High score — should be reported
    event_high = DomainEvent(
        entity_type="session",
        entity_id="s2",
        event_type="DECISION_MADE",
        data=_make_decision_data(score=0.8),
    )
    await bus.publish(event_high)
    assert len(messages) == 1

    await feed.stop()


@pytest.mark.asyncio
async def test_report_flags():
    """report_rejects=False skips REJECT decisions."""
    bus = EventBus(bot_name="test")
    messages: list[str] = []

    async def mock_send(msg: str) -> None:
        messages.append(msg)

    feed = CommitteeTelegramFeed(
        event_bus=bus,
        send_callback=mock_send,
        report_rejects=False,
    )
    await feed.start()

    event = DomainEvent(
        entity_type="session",
        entity_id="s1",
        event_type="DECISION_MADE",
        data=_make_decision_data(verdict="reject"),
    )
    await bus.publish(event)
    assert len(messages) == 0

    # APPROVE should still be reported
    event2 = DomainEvent(
        entity_type="session",
        entity_id="s2",
        event_type="DECISION_MADE",
        data=_make_decision_data(verdict="approve"),
    )
    await bus.publish(event2)
    assert len(messages) == 1

    await feed.stop()


@pytest.mark.asyncio
async def test_report_approvals_false():
    """report_approvals=False skips APPROVE decisions."""
    bus = EventBus(bot_name="test")
    messages: list[str] = []

    async def mock_send(msg: str) -> None:
        messages.append(msg)

    feed = CommitteeTelegramFeed(
        event_bus=bus,
        send_callback=mock_send,
        report_approvals=False,
    )
    await feed.start()

    event = DomainEvent(
        entity_type="session",
        entity_id="s1",
        event_type="DECISION_MADE",
        data=_make_decision_data(verdict="approve"),
    )
    await bus.publish(event)
    assert len(messages) == 0

    await feed.stop()


@pytest.mark.asyncio
async def test_report_anomalies_false():
    """report_anomalies=False skips anomaly alerts."""
    bus = EventBus(bot_name="test")
    messages: list[str] = []

    async def mock_send(msg: str) -> None:
        messages.append(msg)

    feed = CommitteeTelegramFeed(
        event_bus=bus,
        send_callback=mock_send,
        report_anomalies=False,
    )
    await feed.start()

    event = DomainEvent(
        entity_type="risk",
        entity_id="a1",
        event_type="ANOMALY_DETECTED",
        data=_make_anomaly_data(),
    )
    await bus.publish(event)
    assert len(messages) == 0

    await feed.stop()


@pytest.mark.asyncio
async def test_get_stats():
    """Message count tracked correctly."""
    bus = EventBus(bot_name="test")
    messages: list[str] = []

    async def mock_send(msg: str) -> None:
        messages.append(msg)

    feed = CommitteeTelegramFeed(event_bus=bus, send_callback=mock_send)
    await feed.start()

    stats = feed.get_stats()
    assert stats["message_count"] == 0
    assert stats["subscriptions"] == 4

    # Send two events
    for i in range(2):
        event = DomainEvent(
            entity_type="session",
            entity_id=f"s{i}",
            event_type="DECISION_MADE",
            data=_make_decision_data(),
        )
        await bus.publish(event)

    stats = feed.get_stats()
    assert stats["message_count"] == 2

    await feed.stop()
    stats = feed.get_stats()
    assert stats["subscriptions"] == 0


@pytest.mark.asyncio
async def test_stop_unsubscribes():
    """After stop(), no more messages are sent."""
    bus = EventBus(bot_name="test")
    messages: list[str] = []

    async def mock_send(msg: str) -> None:
        messages.append(msg)

    feed = CommitteeTelegramFeed(event_bus=bus, send_callback=mock_send)
    await feed.start()
    await feed.stop()

    event = DomainEvent(
        entity_type="session",
        entity_id="s1",
        event_type="DECISION_MADE",
        data=_make_decision_data(),
    )
    await bus.publish(event)
    assert len(messages) == 0


@pytest.mark.asyncio
async def test_no_bus_start():
    """start() without bus does not crash."""
    feed = CommitteeTelegramFeed()
    await feed.start()
    assert feed.get_stats()["subscriptions"] == 0
