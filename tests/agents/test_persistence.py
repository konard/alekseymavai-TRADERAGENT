"""Tests for AgentStatePersistence — save/restore of learned agent state."""

import json

import pytest

from bot.agents.persistence import AgentStatePersistence
from bot.agents.feedback_tracker import ExpertFeedbackTracker, ExpertScorecard
from bot.agents.regime_profile import (
    RegimeProfileStore,
    RegimeAccuracy,
    ExpertRegimeProfile,
)
from bot.agents.weight_calibrator import WeightCalibrator
from bot.agents.committee import InvestmentCommittee
from bot.agents.base_expert import (
    BaseExpert,
    Vote,
    Verdict,
    Argument,
    ArgumentType,
)
from bot.core.pattern_learner import PatternLearner, Pattern


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyExpert(BaseExpert):
    def __init__(self, name: str, weight: float = 1.0):
        super().__init__()
        self.name = name
        self.role = name
        self.weight = weight

    def analyze(self, s, m):
        return Argument(
            expert_name=self.name,
            argument_type=ArgumentType.RAISED,
            thesis="t",
            confidence=0.5,
        )

    def vote(self, s, m, a):
        return Vote(
            expert_name=self.name,
            verdict=Verdict.APPROVE,
            confidence=0.5,
            reasoning="t",
        )


def _make_committee(*experts: tuple[str, float]) -> InvestmentCommittee:
    c = InvestmentCommittee()
    for name, weight in experts:
        c.add_expert(DummyExpert(name, weight))
    return c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSaveAndRestoreWeights:
    def test_save_and_restore_weights(self, tmp_path):
        committee = _make_committee(
            ("smc_structuralist", 1.35),
            ("trend_analyst", 0.92),
        )
        p = AgentStatePersistence(str(tmp_path / "state"))
        p.save(committee=committee)

        # Restore into a fresh committee with same expert names
        committee2 = _make_committee(
            ("smc_structuralist", 1.0),
            ("trend_analyst", 1.0),
        )
        result = p.restore(committee=committee2)
        assert result["committee"] is True

        weights = {e.name: e.weight for e in committee2.experts}
        assert weights["smc_structuralist"] == pytest.approx(1.35)
        assert weights["trend_analyst"] == pytest.approx(0.92)


class TestSaveAndRestoreFeedback:
    def test_save_and_restore_feedback(self, tmp_path):
        tracker = ExpertFeedbackTracker()
        tracker._scorecards["expert_a"] = ExpertScorecard(
            expert_name="expert_a",
            total_votes=50,
            correct_votes=35,
            incorrect_votes=15,
            accuracy=0.7,
            profit_when_approved=120.5,
            loss_when_approved=-30.2,
            avoided_losses=45.0,
        )

        p = AgentStatePersistence(str(tmp_path / "state"))
        p.save(tracker=tracker)

        tracker2 = ExpertFeedbackTracker()
        result = p.restore(tracker=tracker2)
        assert result["tracker"] is True

        sc = tracker2.get_scorecard("expert_a")
        assert sc.total_votes == 50
        assert sc.correct_votes == 35
        assert sc.incorrect_votes == 15
        assert sc.accuracy == pytest.approx(0.7)
        assert sc.profit_when_approved == pytest.approx(120.5)
        assert sc.loss_when_approved == pytest.approx(-30.2)
        assert sc.avoided_losses == pytest.approx(45.0)


class TestSaveAndRestoreRegimeProfiles:
    def test_save_and_restore_regime_profiles(self, tmp_path):
        store = RegimeProfileStore()
        # Record some outcomes
        store.record_outcome("smc_structuralist", "bull_trend", Verdict.APPROVE, True, 50.0)
        store.record_outcome("smc_structuralist", "bull_trend", Verdict.APPROVE, True, 30.0)
        store.record_outcome("smc_structuralist", "bull_trend", Verdict.APPROVE, False, -10.0)
        store.record_outcome("smc_structuralist", "bear_trend", Verdict.REJECT, True, 0.0)

        p = AgentStatePersistence(str(tmp_path / "state"))
        p.save(regime_store=store)

        store2 = RegimeProfileStore()
        result = p.restore(regime_store=store2)
        assert result["regime_store"] is True

        profile = store2.get_profile("smc_structuralist")
        bull = profile.get_all_regimes()["bull_trend"]
        assert bull.total == 3
        assert bull.correct == 2
        assert bull.incorrect == 1
        assert bull.accuracy == pytest.approx(2.0 / 3.0, abs=0.01)

        bear = profile.get_all_regimes()["bear_trend"]
        assert bear.total == 1
        assert bear.correct == 1


class TestSaveAndRestorePatterns:
    def test_save_and_restore_patterns(self, tmp_path):
        learner = PatternLearner()
        # Manually inject a pattern
        pattern = Pattern(
            pattern_id="abc123",
            name="test_pattern",
            description="A test pattern",
            conditions={"regime": "bull_trend", "strategy": "smc"},
            occurrences=42,
            wins=30,
            losses=12,
            total_pnl=500.0,
            avg_pnl=11.9,
            win_rate=0.714,
            confidence=0.6,
            last_seen=1700000000.0,
            tags=["smc", "bull"],
        )
        learner._patterns["abc123"] = pattern

        p = AgentStatePersistence(str(tmp_path / "state"))
        p.save(pattern_learner=learner)

        learner2 = PatternLearner()
        result = p.restore(pattern_learner=learner2)
        assert result["pattern_learner"] is True

        restored = learner2._patterns["abc123"]
        assert restored.occurrences == 42
        assert restored.wins == 30
        assert restored.losses == 12
        assert restored.win_rate == pytest.approx(0.714)
        assert restored.name == "test_pattern"
        assert restored.conditions == {"regime": "bull_trend", "strategy": "smc"}
        assert restored.tags == ["smc", "bull"]


class TestDirectoryCreation:
    def test_save_creates_directory(self, tmp_path):
        state_dir = tmp_path / "deep" / "nested" / "state"
        assert not state_dir.exists()
        p = AgentStatePersistence(str(state_dir))
        assert state_dir.exists()


class TestRestoreNonexistent:
    def test_restore_nonexistent(self, tmp_path):
        p = AgentStatePersistence(str(tmp_path / "empty_state"))
        committee = _make_committee(("expert_a", 1.0))
        tracker = ExpertFeedbackTracker()
        store = RegimeProfileStore()
        calibrator = WeightCalibrator()
        learner = PatternLearner()

        result = p.restore(
            committee=committee,
            tracker=tracker,
            regime_store=store,
            calibrator=calibrator,
            pattern_learner=learner,
        )
        assert result["committee"] is False
        assert result["tracker"] is False
        assert result["regime_store"] is False
        assert result["calibrator"] is False
        assert result["pattern_learner"] is False


class TestMetadata:
    def test_metadata(self, tmp_path):
        committee = _make_committee(("expert_a", 1.5))
        tracker = ExpertFeedbackTracker()
        p = AgentStatePersistence(str(tmp_path / "state"))
        p.save(committee=committee, tracker=tracker)

        meta = p.get_metadata()
        assert "last_save_ts" in meta
        assert meta["version"] == "1.0"
        assert "weights" in meta["components"]
        assert "feedback" in meta["components"]
        assert meta["save_count"] == 1


class TestSaveIfNeeded:
    def test_save_if_needed(self, tmp_path):
        committee = _make_committee(("expert_a", 1.5))
        p = AgentStatePersistence(str(tmp_path / "state"), auto_save_interval=5)

        assert p.save_if_needed(1, committee=committee) is False
        assert p.save_if_needed(3, committee=committee) is False
        assert p.save_if_needed(5, committee=committee) is True
        assert p.exists()

        # Same count should not save again
        assert p.save_if_needed(5, committee=committee) is False
        # Next interval
        assert p.save_if_needed(10, committee=committee) is True

    def test_save_if_needed_disabled(self, tmp_path):
        p = AgentStatePersistence(str(tmp_path / "state"), auto_save_interval=0)
        committee = _make_committee(("expert_a", 1.5))
        assert p.save_if_needed(5, committee=committee) is False
        assert p.save_if_needed(10, committee=committee) is False


class TestClear:
    def test_clear(self, tmp_path):
        committee = _make_committee(("expert_a", 1.5))
        tracker = ExpertFeedbackTracker()
        p = AgentStatePersistence(str(tmp_path / "state"))
        p.save(committee=committee, tracker=tracker)

        assert p.exists()
        p.clear()
        assert not p.exists()

        # Verify individual files are gone
        state_dir = tmp_path / "state"
        assert not (state_dir / "weights.json").exists()
        assert not (state_dir / "feedback.json").exists()
        assert not (state_dir / "metadata.json").exists()


class TestAtomicWrite:
    def test_atomic_write(self, tmp_path):
        committee = _make_committee(("expert_a", 1.5))
        p = AgentStatePersistence(str(tmp_path / "state"))
        p.save(committee=committee)

        state_dir = tmp_path / "state"
        # Final file exists
        assert (state_dir / "weights.json").exists()
        # No .tmp leftovers
        assert not (state_dir / "weights.tmp").exists()
        assert not (state_dir / "metadata.tmp").exists()


class TestPartialSave:
    def test_partial_save(self, tmp_path):
        """Save only committee (no tracker/store), restore only committee."""
        committee = _make_committee(("expert_a", 1.8), ("expert_b", 0.6))
        p = AgentStatePersistence(str(tmp_path / "state"))
        p.save(committee=committee)

        state_dir = tmp_path / "state"
        assert (state_dir / "weights.json").exists()
        assert not (state_dir / "feedback.json").exists()
        assert not (state_dir / "regime_profiles.json").exists()
        assert not (state_dir / "patterns.json").exists()

        # Restore only committee
        committee2 = _make_committee(("expert_a", 1.0), ("expert_b", 1.0))
        result = p.restore(committee=committee2)
        assert result == {"committee": True}

        weights = {e.name: e.weight for e in committee2.experts}
        assert weights["expert_a"] == pytest.approx(1.8)
        assert weights["expert_b"] == pytest.approx(0.6)


class TestCalibratorPersistence:
    def test_save_and_restore_calibrator(self, tmp_path):
        calibrator = WeightCalibrator(
            base_weights={"expert_a": 1.3, "expert_b": 0.9}
        )
        calibrator._weight_history["expert_a"] = [
            (1700000000.0, 1.2),
            (1700001000.0, 1.35),
        ]
        calibrator._weight_history["expert_b"] = [
            (1700000000.0, 0.9),
        ]

        p = AgentStatePersistence(str(tmp_path / "state"))
        p.save(calibrator=calibrator)

        calibrator2 = WeightCalibrator()
        result = p.restore(calibrator=calibrator2)
        assert result["calibrator"] is True
        assert calibrator2._base_weights == {"expert_a": 1.3, "expert_b": 0.9}
        assert len(calibrator2._weight_history["expert_a"]) == 2
        assert calibrator2._weight_history["expert_a"][1] == (1700001000.0, 1.35)
