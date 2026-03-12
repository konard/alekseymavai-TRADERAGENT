from __future__ import annotations

import pytest

from bot.agents.tools.learning import (
    HypothesisSkill,
    JournalSkill,
    LearningAggregator,
    _binomial_p_value,
)


# ===== Fixtures =====

@pytest.fixture
def hyp_skill() -> HypothesisSkill:
    return HypothesisSkill()


@pytest.fixture
def journal_skill() -> JournalSkill:
    return JournalSkill()


def _make_hypothesis(skill: HypothesisSkill, expert: str = "trend_expert", **overrides):
    params = {
        "expert_name": expert,
        "hypothesis": "Bull trend continues when ADX > 25",
        "test_conditions": {"regime": "bull_trend", "adx_above": 25, "direction": "long"},
        "expected_outcome": "win",
        "confidence": 0.8,
        **overrides,
    }
    return skill.execute(params)


def _record_entry(skill: JournalSkill, trade_id: str = "t1", expert: str = "trend_expert", **overrides):
    params = {
        "action": "record",
        "trade_id": trade_id,
        "expert_name": expert,
        "decision": "open long",
        "reasoning": "ADX rising, EMA bullish",
        "market_context": {"regime": "bull_trend", "adx": 30},
        "confidence": 0.75,
        **overrides,
    }
    return skill.execute(params)


def _reflect_entry(skill: JournalSkill, trade_id: str = "t1", outcome: str = "win", **overrides):
    params = {
        "action": "reflect",
        "trade_id": trade_id,
        "outcome": outcome,
        "pnl": 50.0 if outcome == "win" else -30.0,
        "lesson": "Wait for confirmation",
        "what_was_right": "Good entry timing",
        "what_was_wrong": "Stop too tight",
        **overrides,
    }
    return skill.execute(params)


# ===== HypothesisSkill Tests =====

class TestHypothesisSkill:
    def test_create_hypothesis(self, hyp_skill: HypothesisSkill):
        result = _make_hypothesis(hyp_skill)
        assert result.success is True
        assert "hypothesis_id" in result.data
        assert result.data["status"] == "pending"
        assert result.data["expert_name"] == "trend_expert"

    def test_create_hypothesis_missing_fields(self, hyp_skill: HypothesisSkill):
        result = hyp_skill.execute({"expert_name": "x"})
        assert result.success is False
        assert "Missing" in result.error

    def test_validate_confirmed(self, hyp_skill: HypothesisSkill):
        res = _make_hypothesis(hyp_skill)
        hyp_id = res.data["hypothesis_id"]
        # 8 wins out of 10 matched → win_rate=0.8, p_value should be small
        outcomes = [
            {"matched_conditions": True, "result": "win", "pnl": 100.0}
        ] * 8 + [
            {"matched_conditions": True, "result": "loss", "pnl": -50.0}
        ] * 2
        validation = hyp_skill.validate_hypothesis(hyp_id, outcomes)
        assert validation["status"] == "confirmed"
        assert validation["sample_size"] == 10
        assert validation["wins"] == 8
        assert validation["win_rate"] == pytest.approx(0.8)
        assert validation["p_value"] < 0.1

    def test_validate_rejected(self, hyp_skill: HypothesisSkill):
        res = _make_hypothesis(hyp_skill)
        hyp_id = res.data["hypothesis_id"]
        # 4 wins out of 10 → win_rate=0.4, should be rejected
        outcomes = [
            {"matched_conditions": True, "result": "win", "pnl": 20.0}
        ] * 4 + [
            {"matched_conditions": True, "result": "loss", "pnl": -20.0}
        ] * 6
        validation = hyp_skill.validate_hypothesis(hyp_id, outcomes)
        assert validation["status"] == "rejected"
        assert validation["win_rate"] == pytest.approx(0.4)

    def test_validate_insufficient_data_stays_testing(self, hyp_skill: HypothesisSkill):
        res = _make_hypothesis(hyp_skill)
        hyp_id = res.data["hypothesis_id"]
        outcomes = [
            {"matched_conditions": True, "result": "win", "pnl": 100.0}
        ] * 5
        validation = hyp_skill.validate_hypothesis(hyp_id, outcomes)
        assert validation["status"] == "testing"
        assert validation["sample_size"] == 5

    def test_validate_not_found(self, hyp_skill: HypothesisSkill):
        result = hyp_skill.validate_hypothesis("nonexistent", [])
        assert "error" in result

    def test_unmatched_conditions_excluded(self, hyp_skill: HypothesisSkill):
        res = _make_hypothesis(hyp_skill)
        hyp_id = res.data["hypothesis_id"]
        outcomes = [
            {"matched_conditions": False, "result": "win", "pnl": 100.0}
        ] * 20
        validation = hyp_skill.validate_hypothesis(hyp_id, outcomes)
        assert validation["sample_size"] == 0
        assert validation["status"] == "testing"

    def test_p_value_calculation(self):
        # 10 wins out of 10 → p_value = 0.5^10 ≈ 0.000977
        p = _binomial_p_value(10, 10, 0.5)
        assert p == pytest.approx(0.5**10, abs=1e-9)
        # 5 wins out of 10 → p_value ≈ 0.623
        p2 = _binomial_p_value(5, 10, 0.5)
        assert p2 > 0.5

    def test_query_by_expert(self, hyp_skill: HypothesisSkill):
        _make_hypothesis(hyp_skill, expert="alice")
        _make_hypothesis(hyp_skill, expert="bob")
        _make_hypothesis(hyp_skill, expert="alice")
        assert len(hyp_skill.get_hypotheses(expert_name="alice")) == 2
        assert len(hyp_skill.get_hypotheses(expert_name="bob")) == 1

    def test_query_by_status(self, hyp_skill: HypothesisSkill):
        r1 = _make_hypothesis(hyp_skill)
        _make_hypothesis(hyp_skill)
        # Confirm one
        outcomes = [{"matched_conditions": True, "result": "win", "pnl": 10}] * 10
        hyp_skill.validate_hypothesis(r1.data["hypothesis_id"], outcomes)
        assert len(hyp_skill.get_hypotheses(status="confirmed")) == 1
        assert len(hyp_skill.get_hypotheses(status="pending")) == 1

    def test_stats(self, hyp_skill: HypothesisSkill):
        _make_hypothesis(hyp_skill)
        _make_hypothesis(hyp_skill)
        stats = hyp_skill.get_stats()
        assert stats["total"] == 2
        assert stats["pending"] == 2
        assert stats["confirmed"] == 0


# ===== JournalSkill Tests =====

class TestJournalSkill:
    def test_record_entry(self, journal_skill: JournalSkill):
        result = _record_entry(journal_skill)
        assert result.success is True
        assert result.data["trade_id"] == "t1"
        assert result.data["reflection"] is None

    def test_record_missing_fields(self, journal_skill: JournalSkill):
        result = journal_skill.execute({"action": "record", "trade_id": "x"})
        assert result.success is False

    def test_reflect_on_trade(self, journal_skill: JournalSkill):
        _record_entry(journal_skill, trade_id="t1")
        result = _reflect_entry(journal_skill, trade_id="t1", outcome="win")
        assert result.success is True
        assert result.data["reflection"]["outcome"] == "win"
        # Check it's linked
        entry = journal_skill._entries["t1"]
        assert entry["reflection"]["pnl"] == 50.0

    def test_reflect_missing_entry(self, journal_skill: JournalSkill):
        result = _reflect_entry(journal_skill, trade_id="nonexistent")
        assert result.success is False

    def test_query_by_expert(self, journal_skill: JournalSkill):
        _record_entry(journal_skill, trade_id="t1", expert="alice")
        _record_entry(journal_skill, trade_id="t2", expert="bob")
        result = journal_skill.execute({"action": "query", "expert_name": "alice"})
        assert result.success is True
        assert result.data["count"] == 1

    def test_query_by_outcome(self, journal_skill: JournalSkill):
        _record_entry(journal_skill, trade_id="t1")
        _record_entry(journal_skill, trade_id="t2")
        _reflect_entry(journal_skill, trade_id="t1", outcome="win")
        _reflect_entry(journal_skill, trade_id="t2", outcome="loss")
        result = journal_skill.execute({"action": "query", "outcome": "win"})
        assert result.data["count"] == 1
        assert result.data["entries"][0]["trade_id"] == "t1"

    def test_get_lessons(self, journal_skill: JournalSkill):
        _record_entry(journal_skill, trade_id="t1")
        _record_entry(journal_skill, trade_id="t2")
        _reflect_entry(journal_skill, trade_id="t1", lesson="Patience pays")
        _reflect_entry(journal_skill, trade_id="t2", lesson="Cut losses early")
        lessons = journal_skill.get_lessons()
        assert len(lessons) == 2
        assert "Patience pays" in lessons

    def test_decision_quality_with_wins_and_losses(self, journal_skill: JournalSkill):
        _record_entry(journal_skill, trade_id="t1", confidence=0.9)
        _record_entry(journal_skill, trade_id="t2", confidence=0.3)
        _reflect_entry(journal_skill, trade_id="t1", outcome="win")
        _reflect_entry(journal_skill, trade_id="t2", outcome="loss")
        quality = journal_skill.get_decision_quality()
        assert quality["total_decisions"] == 2
        assert quality["reflected_pct"] == pytest.approx(1.0)
        assert quality["avg_confidence_on_wins"] == pytest.approx(0.9)
        assert quality["avg_confidence_on_losses"] == pytest.approx(0.3)

    def test_empty_journal(self, journal_skill: JournalSkill):
        quality = journal_skill.get_decision_quality()
        assert quality["total_decisions"] == 0
        assert quality["reflected_pct"] == 0.0
        stats = journal_skill.get_stats()
        assert stats["total_entries"] == 0

    def test_unknown_action(self, journal_skill: JournalSkill):
        result = journal_skill.execute({"action": "unknown"})
        assert result.success is False

    def test_get_stats(self, journal_skill: JournalSkill):
        _record_entry(journal_skill, trade_id="t1")
        _record_entry(journal_skill, trade_id="t2")
        _reflect_entry(journal_skill, trade_id="t1", outcome="win")
        stats = journal_skill.get_stats()
        assert stats["total_entries"] == 2
        assert stats["reflected"] == 1
        assert stats["unreflected"] == 1


# ===== LearningAggregator Tests =====

class TestLearningAggregator:
    def test_insights_with_confirmed_hypotheses(self):
        hyp = HypothesisSkill()
        journal = JournalSkill()
        agg = LearningAggregator(hyp, journal)

        # Create and confirm a hypothesis
        r = _make_hypothesis(hyp, expert="alice")
        outcomes = [{"matched_conditions": True, "result": "win", "pnl": 10}] * 10
        hyp.validate_hypothesis(r.data["hypothesis_id"], outcomes)

        # Add journal entries
        _record_entry(journal, trade_id="t1", expert="alice", confidence=0.9)
        _reflect_entry(journal, trade_id="t1", outcome="win",
                       what_was_right="Good timing", what_was_wrong="Stop too tight")

        insights = agg.generate_insights(expert_name="alice")
        assert len(insights["confirmed_hypotheses"]) == 1
        assert "Stop too tight" in insights["common_mistakes"]
        assert "Good timing" in insights["strengths"]
        assert len(insights["recommendations"]) > 0

    def test_confidence_calibration(self):
        hyp = HypothesisSkill()
        journal = JournalSkill()
        agg = LearningAggregator(hyp, journal)

        # High confidence wins, low confidence losses
        _record_entry(journal, trade_id="t1", confidence=0.9)
        _record_entry(journal, trade_id="t2", confidence=0.2)
        _reflect_entry(journal, trade_id="t1", outcome="win")
        _reflect_entry(journal, trade_id="t2", outcome="loss")

        insights = agg.generate_insights()
        cal = insights["confidence_calibration"]
        assert cal["avg_confidence_on_wins"] == pytest.approx(0.9)
        assert cal["avg_confidence_on_losses"] == pytest.approx(0.2)
        assert cal["spread"] == pytest.approx(0.7)
        assert cal["total_reflected"] == 2

    def test_empty_state(self):
        hyp = HypothesisSkill()
        journal = JournalSkill()
        agg = LearningAggregator(hyp, journal)

        insights = agg.generate_insights()
        assert insights["confirmed_hypotheses"] == []
        assert insights["common_mistakes"] == []
        assert insights["strengths"] == []
        assert insights["confidence_calibration"]["total_reflected"] == 0
        assert insights["recommendations"] == []

    def test_poor_calibration_recommendation(self):
        hyp = HypothesisSkill()
        journal = JournalSkill()
        agg = LearningAggregator(hyp, journal)

        # Similar confidence for wins and losses → poor calibration
        _record_entry(journal, trade_id="t1", confidence=0.5)
        _record_entry(journal, trade_id="t2", confidence=0.5)
        _reflect_entry(journal, trade_id="t1", outcome="win")
        _reflect_entry(journal, trade_id="t2", outcome="loss",
                       what_was_wrong="Overtrading")

        insights = agg.generate_insights()
        recs = " ".join(insights["recommendations"])
        assert "calibration" in recs.lower() or "Overtrading" in recs
