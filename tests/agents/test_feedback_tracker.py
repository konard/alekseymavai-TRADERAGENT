"""Tests for ExpertFeedbackTracker."""

import pytest
import pytest_asyncio

from bot.agents.base_expert import Vote, Verdict, CommitteeDecision
from bot.agents.feedback_tracker import ExpertFeedbackTracker, ExpertScorecard
from bot.core.event_bus import EventBus, DomainEvent


def _make_decision(votes: list[Vote], session_id: str | None = None) -> CommitteeDecision:
    kw = {}
    if session_id is not None:
        kw["session_id"] = session_id
    return CommitteeDecision(
        verdict=Verdict.APPROVE,
        score=0.7,
        votes=votes,
        reasoning="test",
        **kw,
    )


class TestRecordAndScore:
    def test_record_and_score_approve_correct(self):
        tracker = ExpertFeedbackTracker()
        decision = _make_decision([
            Vote(expert_name="smc", verdict=Verdict.APPROVE, confidence=0.8, reasoning="bullish"),
        ])
        tracker.record_decision(decision, position_id="pos_1")
        results = tracker.record_outcome("pos_1", pnl=50.0)

        assert results == [("smc", True)]
        sc = tracker.get_scorecard("smc")
        assert sc.correct_votes == 1
        assert sc.incorrect_votes == 0
        assert sc.accuracy == 1.0
        assert sc.profit_when_approved == 50.0

    def test_record_and_score_approve_incorrect(self):
        tracker = ExpertFeedbackTracker()
        decision = _make_decision([
            Vote(expert_name="smc", verdict=Verdict.APPROVE, confidence=0.8, reasoning="bullish"),
        ])
        tracker.record_decision(decision, position_id="pos_1")
        results = tracker.record_outcome("pos_1", pnl=-30.0)

        assert results == [("smc", False)]
        sc = tracker.get_scorecard("smc")
        assert sc.correct_votes == 0
        assert sc.incorrect_votes == 1
        assert sc.accuracy == 0.0
        assert sc.loss_when_approved == -30.0

    def test_record_and_score_reject_correct(self):
        tracker = ExpertFeedbackTracker()
        decision = _make_decision([
            Vote(expert_name="risk", verdict=Verdict.REJECT, confidence=0.9, reasoning="too risky"),
        ])
        tracker.record_decision(decision, position_id="pos_1")
        results = tracker.record_outcome("pos_1", pnl=-40.0)

        assert results == [("risk", True)]
        sc = tracker.get_scorecard("risk")
        assert sc.correct_votes == 1
        assert sc.accuracy == 1.0
        assert sc.avoided_losses == 40.0

    def test_defer_not_counted_as_error(self):
        tracker = ExpertFeedbackTracker()
        decision = _make_decision([
            Vote(expert_name="trend", verdict=Verdict.DEFER, confidence=0.3, reasoning="uncertain"),
        ])
        tracker.record_decision(decision, position_id="pos_1")
        results = tracker.record_outcome("pos_1", pnl=-20.0)

        assert results == []
        sc = tracker.get_scorecard("trend")
        assert sc.total_votes == 1
        assert sc.deferred_votes == 1
        assert sc.correct_votes == 0
        assert sc.incorrect_votes == 0
        assert sc.accuracy == 0.0

    def test_multiple_experts_scored_independently(self):
        tracker = ExpertFeedbackTracker()
        decision = _make_decision([
            Vote(expert_name="smc", verdict=Verdict.APPROVE, confidence=0.8, reasoning="bullish"),
            Vote(expert_name="trend", verdict=Verdict.REJECT, confidence=0.6, reasoning="counter-trend"),
            Vote(expert_name="risk", verdict=Verdict.APPROVE, confidence=0.5, reasoning="ok risk"),
            Vote(expert_name="contrarian", verdict=Verdict.DEFER, confidence=0.3, reasoning="unsure"),
        ])
        tracker.record_decision(decision, position_id="pos_1")
        results = tracker.record_outcome("pos_1", pnl=100.0)

        # APPROVE on winner = correct, REJECT on winner = incorrect, DEFER = neutral
        assert ("smc", True) in results
        assert ("trend", False) in results
        assert ("risk", True) in results
        assert len(results) == 3  # contrarian deferred, not in results

        assert tracker.get_accuracy("smc") == 1.0
        assert tracker.get_accuracy("trend") == 0.0
        assert tracker.get_accuracy("risk") == 1.0
        assert tracker.get_accuracy("contrarian") == 0.0

    def test_accuracy_calculation(self):
        tracker = ExpertFeedbackTracker()

        # 10 decisions: 7 correct approves, 3 incorrect approves
        for i in range(10):
            decision = _make_decision(
                [Vote(expert_name="smc", verdict=Verdict.APPROVE, confidence=0.7, reasoning="go")],
                session_id=f"session_{i}",
            )
            tracker.record_decision(decision, position_id=f"pos_{i}")

        for i in range(7):
            tracker.record_outcome(f"pos_{i}", pnl=10.0)
        for i in range(7, 10):
            tracker.record_outcome(f"pos_{i}", pnl=-5.0)

        sc = tracker.get_scorecard("smc")
        assert sc.total_votes == 10
        assert sc.correct_votes == 7
        assert sc.incorrect_votes == 3
        assert sc.accuracy == pytest.approx(0.7)

    def test_profit_tracking(self):
        tracker = ExpertFeedbackTracker()

        d1 = _make_decision(
            [Vote(expert_name="smc", verdict=Verdict.APPROVE, confidence=0.8, reasoning="go")],
            session_id="s1",
        )
        d2 = _make_decision(
            [Vote(expert_name="smc", verdict=Verdict.APPROVE, confidence=0.8, reasoning="go")],
            session_id="s2",
        )
        tracker.record_decision(d1, position_id="p1")
        tracker.record_decision(d2, position_id="p2")

        tracker.record_outcome("p1", pnl=120.0)
        tracker.record_outcome("p2", pnl=-45.0)

        sc = tracker.get_scorecard("smc")
        assert sc.profit_when_approved == pytest.approx(120.0)
        assert sc.loss_when_approved == pytest.approx(-45.0)

    def test_avoided_losses(self):
        tracker = ExpertFeedbackTracker()
        decision = _make_decision([
            Vote(expert_name="risk", verdict=Verdict.REJECT, confidence=0.9, reasoning="bad setup"),
        ])
        tracker.record_decision(decision, position_id="pos_1")
        tracker.record_outcome("pos_1", pnl=-80.0)

        sc = tracker.get_scorecard("risk")
        assert sc.avoided_losses == pytest.approx(80.0)

    def test_unknown_position_ignored(self):
        tracker = ExpertFeedbackTracker()
        results = tracker.record_outcome("nonexistent_pos", pnl=50.0)
        assert results == []
        assert tracker.get_all_scorecards() == []


@pytest.mark.asyncio
async def test_eventbus_integration():
    bus = EventBus(bot_name="test_bot")
    tracker = ExpertFeedbackTracker(event_bus=bus)
    await tracker.start()

    decision = _make_decision([
        Vote(expert_name="smc", verdict=Verdict.APPROVE, confidence=0.8, reasoning="bullish"),
        Vote(expert_name="trend", verdict=Verdict.REJECT, confidence=0.6, reasoning="counter-trend"),
    ])

    # Publish DECISION_MADE event
    await bus.publish(DomainEvent(
        entity_type="session",
        entity_id=decision.session_id,
        event_type="DECISION_MADE",
        data={
            "decision": decision,
            "position_id": "pos_evt_1",
            "regime": "bull",
        },
        bot_name="test_bot",
    ))

    # Publish POSITION_CLOSED event
    await bus.publish(DomainEvent(
        entity_type="position",
        entity_id="pos_evt_1",
        event_type="POSITION_CLOSED",
        data={
            "position_id": "pos_evt_1",
            "pnl": 75.0,
        },
        bot_name="test_bot",
    ))

    # smc APPROVE on winner = correct
    assert tracker.get_scorecard("smc").correct_votes == 1
    assert tracker.get_scorecard("smc").accuracy == 1.0
    # trend REJECT on winner = incorrect
    assert tracker.get_scorecard("trend").incorrect_votes == 1
    assert tracker.get_scorecard("trend").accuracy == 0.0

    await tracker.stop()

    # Verify stats
    stats = tracker.get_stats()
    assert stats["experts_tracked"] == 2
    assert stats["pending_decisions"] == 0
