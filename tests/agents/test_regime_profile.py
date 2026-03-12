"""Tests for Regime-Aware Expert Profiles."""

import pytest

from bot.agents.base_expert import Verdict
from bot.agents.regime_profile import (
    ExpertRegimeProfile,
    RegimeAccuracy,
    RegimeProfileStore,
)

REGIME_TYPES = [
    "tight_range",
    "wide_range",
    "quiet_transition",
    "volatile_transition",
    "bull_trend",
    "bear_trend",
    "accumulation",
    "distribution",
]


class TestRegimeAccuracy:
    def test_to_dict(self):
        ra = RegimeAccuracy(regime="bull_trend", total=10, correct=7, incorrect=3, accuracy=0.7)
        d = ra.to_dict()
        assert d["regime"] == "bull_trend"
        assert d["total"] == 10
        assert d["accuracy"] == 0.7


class TestExpertRegimeProfile:
    def test_record_and_retrieve_accuracy(self):
        """Record 10 outcomes in bull_trend, check accuracy."""
        profile = ExpertRegimeProfile("trend_expert")
        for i in range(10):
            correct = i < 7  # 7 correct, 3 incorrect
            profile.record("bull_trend", Verdict.APPROVE, was_correct=correct, pnl=1.0 if correct else -1.0)

        assert profile.get_accuracy("bull_trend") == pytest.approx(0.7)
        ra = profile.get_all_regimes()["bull_trend"]
        assert ra.total == 10
        assert ra.correct == 7
        assert ra.incorrect == 3

    def test_multiple_regimes_independent(self):
        """bull_trend and bear_trend are tracked separately."""
        profile = ExpertRegimeProfile("trend_expert")

        # bull_trend: 8/10 correct
        for i in range(10):
            profile.record("bull_trend", Verdict.APPROVE, was_correct=(i < 8))

        # bear_trend: 3/10 correct
        for i in range(10):
            profile.record("bear_trend", Verdict.APPROVE, was_correct=(i < 3))

        assert profile.get_accuracy("bull_trend") == pytest.approx(0.8)
        assert profile.get_accuracy("bear_trend") == pytest.approx(0.3)

    def test_best_worst_regime(self):
        """Create profile with varied accuracy, check best/worst."""
        profile = ExpertRegimeProfile("test_expert")

        # bull_trend: 9/10 = 0.9
        for i in range(10):
            profile.record("bull_trend", Verdict.APPROVE, was_correct=(i < 9))

        # bear_trend: 2/10 = 0.2
        for i in range(10):
            profile.record("bear_trend", Verdict.APPROVE, was_correct=(i < 2))

        # tight_range: 5/10 = 0.5
        for i in range(10):
            profile.record("tight_range", Verdict.APPROVE, was_correct=(i < 5))

        assert profile.get_best_regime() == "bull_trend"
        assert profile.get_worst_regime() == "bear_trend"

    def test_min_samples_for_best_worst(self):
        """Less than 5 samples returns empty string."""
        profile = ExpertRegimeProfile("test_expert")

        # Only 4 samples — below minimum
        for i in range(4):
            profile.record("bull_trend", Verdict.APPROVE, was_correct=True)

        assert profile.get_best_regime() == ""
        assert profile.get_worst_regime() == ""

    def test_defer_not_counted(self):
        """DEFER doesn't affect accuracy."""
        profile = ExpertRegimeProfile("test_expert")

        # 5 approvals (3 correct), 5 defers
        for i in range(3):
            profile.record("bull_trend", Verdict.APPROVE, was_correct=True)
        for i in range(2):
            profile.record("bull_trend", Verdict.APPROVE, was_correct=False)
        for i in range(5):
            profile.record("bull_trend", Verdict.DEFER, was_correct=True)

        ra = profile.get_all_regimes()["bull_trend"]
        assert ra.deferred == 5
        assert ra.total == 10
        assert ra.correct == 3
        assert ra.incorrect == 2
        assert ra.accuracy == pytest.approx(0.6)

    def test_avg_pnl_tracking(self):
        """APPROVE votes track average PnL."""
        profile = ExpertRegimeProfile("test_expert")

        profile.record("bull_trend", Verdict.APPROVE, was_correct=True, pnl=100.0)
        profile.record("bull_trend", Verdict.APPROVE, was_correct=True, pnl=200.0)
        profile.record("bull_trend", Verdict.APPROVE, was_correct=False, pnl=-50.0)
        # REJECT should not track PnL
        profile.record("bull_trend", Verdict.REJECT, was_correct=True, pnl=999.0)
        # DEFER should not track PnL
        profile.record("bull_trend", Verdict.DEFER, was_correct=True, pnl=999.0)

        ra = profile.get_all_regimes()["bull_trend"]
        # Only 3 APPROVE votes tracked: 100, 200, -50
        assert ra.avg_pnl_when_approved == pytest.approx(250.0 / 3)

    def test_empty_profile(self):
        """No data returns 0.5 accuracy."""
        profile = ExpertRegimeProfile("empty_expert")
        assert profile.get_accuracy("bull_trend") == 0.5
        assert profile.get_accuracy("nonexistent_regime") == 0.5


class TestRegimeProfileStore:
    def test_profile_store_multiple_experts(self):
        """4 experts with different regimes."""
        store = RegimeProfileStore()

        experts = ["trend_expert", "risk_expert", "smc_expert", "contrarian_expert"]
        regimes = ["bull_trend", "bear_trend", "tight_range", "wide_range"]

        for expert, regime in zip(experts, regimes):
            for i in range(10):
                store.record_outcome(expert, regime, Verdict.APPROVE, was_correct=(i < 7))

        assert len(store.get_all_profiles()) == 4
        for expert, regime in zip(experts, regimes):
            assert store.get_regime_accuracy(expert, regime) == pytest.approx(0.7)

    def test_all_regime_types_covered(self):
        """Record for all 8 regime types."""
        store = RegimeProfileStore()

        for regime in REGIME_TYPES:
            for i in range(6):
                store.record_outcome("test_expert", regime, Verdict.APPROVE, was_correct=(i < 4))

        profile = store.get_profile("test_expert")
        all_regimes = profile.get_all_regimes()
        assert len(all_regimes) == 8
        for regime in REGIME_TYPES:
            assert regime in all_regimes

    def test_get_stats(self):
        """store.get_stats() returns summary."""
        store = RegimeProfileStore()

        # Expert A: 10 records in bull_trend (7 correct, 3 incorrect)
        for i in range(10):
            store.record_outcome("expert_a", "bull_trend", Verdict.APPROVE, was_correct=(i < 7))

        # Expert B: 5 records in bear_trend (3 correct, 2 incorrect) + 2 deferred
        for i in range(5):
            store.record_outcome("expert_b", "bear_trend", Verdict.APPROVE, was_correct=(i < 3))
        for i in range(2):
            store.record_outcome("expert_b", "bear_trend", Verdict.DEFER, was_correct=True)

        stats = store.get_stats()
        assert stats["experts_count"] == 2
        assert stats["regimes_count"] == 2
        assert stats["total_records"] == 17
        assert stats["total_correct"] == 10  # 7 + 3
        assert stats["total_incorrect"] == 5  # 3 + 2
        assert stats["total_deferred"] == 2
        assert stats["overall_accuracy"] == pytest.approx(round(10 / 15, 4))

    def test_to_dict(self):
        """Store serializes to dict."""
        store = RegimeProfileStore()
        store.record_outcome("expert_a", "bull_trend", Verdict.APPROVE, was_correct=True)
        d = store.to_dict()
        assert "profiles" in d
        assert "stats" in d
        assert "expert_a" in d["profiles"]
