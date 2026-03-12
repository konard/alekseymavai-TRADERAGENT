"""Tests for Adaptive Weight Calibrator."""

import pytest

from bot.agents.weight_calibrator import WeightCalibrator, DEFAULT_BASE_WEIGHTS
from bot.agents.feedback_tracker import ExpertFeedbackTracker, ExpertScorecard
from bot.agents.regime_profile import RegimeProfileStore
from bot.agents.committee import InvestmentCommittee
from bot.agents.base_expert import (
    BaseExpert,
    Vote,
    Verdict,
    CommitteeDecision,
    Argument,
    ArgumentType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DummyExpert(BaseExpert):
    def __init__(self, name: str, weight: float = 1.0):
        super().__init__()
        self.name = name
        self.role = name
        self.weight = weight

    def analyze(self, signal, market_data):
        return Argument(
            expert_name=self.name,
            argument_type=ArgumentType.RAISED,
            thesis="test",
            confidence=0.5,
        )

    def vote(self, signal, market_data, arguments):
        return Vote(
            expert_name=self.name,
            verdict=Verdict.APPROVE,
            confidence=0.5,
            reasoning="test",
        )


def _make_committee(*names: str) -> InvestmentCommittee:
    committee = InvestmentCommittee()
    for n in names:
        committee.add_expert(DummyExpert(n))
    return committee


def _make_tracker(scores: dict[str, tuple[int, int]]) -> ExpertFeedbackTracker:
    """Create tracker with pre-set scorecards.

    *scores*: mapping ``expert_name -> (correct_votes, incorrect_votes)``.
    """
    tracker = ExpertFeedbackTracker()
    for name, (correct, incorrect) in scores.items():
        total = correct + incorrect
        denom = correct + incorrect
        acc = correct / denom if denom > 0 else 0.0
        tracker._scorecards[name] = ExpertScorecard(
            expert_name=name,
            total_votes=total,
            correct_votes=correct,
            incorrect_votes=incorrect,
            accuracy=acc,
        )
    return tracker


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWeightCalibrator:

    def test_calibrate_increases_weight_for_accurate_expert(self):
        """90% accuracy should produce weight > base."""
        committee = _make_committee("expert1")
        tracker = _make_tracker({"expert1": (18, 2)})  # 90%
        cal = WeightCalibrator(base_weights={"expert1": 1.0}, min_samples=10)

        weights = cal.calibrate(committee, tracker)

        # new_weight = 1.0 * (0.5 + 0.9) = 1.4
        assert weights["expert1"] == pytest.approx(1.4)
        assert weights["expert1"] > 1.0

    def test_calibrate_decreases_weight_for_inaccurate_expert(self):
        """20% accuracy should produce weight < base."""
        committee = _make_committee("expert1")
        tracker = _make_tracker({"expert1": (4, 16)})  # 20%
        cal = WeightCalibrator(base_weights={"expert1": 1.0}, min_samples=10)

        weights = cal.calibrate(committee, tracker)

        # new_weight = 1.0 * (0.5 + 0.2) = 0.7
        assert weights["expert1"] == pytest.approx(0.7)
        assert weights["expert1"] < 1.0

    def test_weight_clamped_to_bounds(self):
        """Even extreme accuracy stays within [min_weight, max_weight]."""
        committee = _make_committee("expert1")
        tracker = _make_tracker({"expert1": (20, 0)})  # 100%
        cal = WeightCalibrator(
            base_weights={"expert1": 3.0},  # high base
            min_weight=0.3,
            max_weight=2.5,
            min_samples=10,
        )

        weights = cal.calibrate(committee, tracker)

        # raw = 3.0 * 1.5 = 4.5 → clamped to 2.5
        assert weights["expert1"] == 2.5

        # Low clamp
        cal2 = WeightCalibrator(
            base_weights={"expert1": 0.3},
            min_weight=0.3,
            max_weight=2.5,
            min_samples=10,
        )
        tracker2 = _make_tracker({"expert1": (0, 20)})  # 0%
        weights2 = cal2.calibrate(committee, tracker2)

        # raw = 0.3 * 0.5 = 0.15 → clamped to 0.3
        assert weights2["expert1"] == 0.3

    def test_base_weight_preserved_at_50pct_accuracy(self):
        """50% accuracy → weight ≈ base (multiplier = 1.0)."""
        committee = _make_committee("expert1")
        tracker = _make_tracker({"expert1": (10, 10)})  # 50%
        cal = WeightCalibrator(base_weights={"expert1": 1.2}, min_samples=10)

        weights = cal.calibrate(committee, tracker)

        # new_weight = 1.2 * (0.5 + 0.5) = 1.2
        assert weights["expert1"] == pytest.approx(1.2)

    def test_regime_aware_calibration(self):
        """Uses regime-specific accuracy from RegimeProfileStore."""
        committee = _make_committee("expert1")
        tracker = _make_tracker({"expert1": (10, 10)})  # overall 50%

        regime_store = RegimeProfileStore()
        # Record 80% accuracy in bull_trend (8 correct, 2 incorrect)
        for _ in range(8):
            regime_store.record_outcome(
                "expert1", "bull_trend", Verdict.APPROVE, True, 100.0
            )
        for _ in range(2):
            regime_store.record_outcome(
                "expert1", "bull_trend", Verdict.APPROVE, False, -50.0
            )

        cal = WeightCalibrator(base_weights={"expert1": 1.0}, min_samples=10)
        weights = cal.calibrate(
            committee, tracker, regime_store=regime_store, current_regime="bull_trend"
        )

        # new_weight = 1.0 * (0.5 + 0.8) = 1.3
        assert weights["expert1"] == pytest.approx(1.3)

    def test_insufficient_data_keeps_base_weight(self):
        """Fewer than min_samples scored votes → base weight unchanged."""
        committee = _make_committee("expert1")
        tracker = _make_tracker({"expert1": (5, 2)})  # only 7 votes
        base = 1.0
        cal = WeightCalibrator(base_weights={"expert1": base}, min_samples=10)

        weights = cal.calibrate(committee, tracker)

        assert weights["expert1"] == base

    def test_weight_history_recorded(self):
        """After calibrate(), weight_history contains an entry."""
        committee = _make_committee("expert1")
        tracker = _make_tracker({"expert1": (15, 5)})
        cal = WeightCalibrator(base_weights={"expert1": 1.0}, min_samples=10)

        cal.calibrate(committee, tracker)
        history = cal.get_weight_history()

        assert "expert1" in history
        assert len(history["expert1"]) == 1
        ts, w = history["expert1"][0]
        assert isinstance(ts, float)
        assert w == pytest.approx(1.0 * (0.5 + 0.75))

    def test_reset_restores_base_weights(self):
        """reset() clears history and calibration count."""
        committee = _make_committee("expert1")
        tracker = _make_tracker({"expert1": (18, 2)})
        cal = WeightCalibrator(base_weights={"expert1": 1.0}, min_samples=10)

        cal.calibrate(committee, tracker)
        assert cal._calibration_count == 1
        assert len(cal.get_weight_history()["expert1"]) == 1

        cal.reset()

        assert cal._calibration_count == 0
        assert cal.get_weight_history() == {}

    def test_get_stats(self):
        """get_stats returns correct structure."""
        committee = _make_committee("expert1")
        tracker = _make_tracker({"expert1": (18, 2)})
        cal = WeightCalibrator(base_weights={"expert1": 1.0}, min_samples=10)

        cal.calibrate(committee, tracker)
        stats = cal.get_stats()

        assert stats["calibration_count"] == 1
        assert "current_weights" in stats
        assert "base_weights" in stats
        assert stats["base_weights"]["expert1"] == 1.0
        assert "expert1" in stats["current_weights"]

    def test_default_base_weights(self):
        """When base_weights is None, uses default expert names."""
        cal = WeightCalibrator()

        assert cal._base_weights == DEFAULT_BASE_WEIGHTS
        assert "smc_structuralist" in cal._base_weights
        assert "trend_analyst" in cal._base_weights
        assert "risk_manager" in cal._base_weights
        assert "contrarian" in cal._base_weights
