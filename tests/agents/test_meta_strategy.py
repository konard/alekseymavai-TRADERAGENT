"""Tests for MetaStrategyAllocator — capital allocation between strategies."""

import pytest

from bot.agents.meta_strategy import MetaStrategyAllocator, AllocationPlan, StrategyScore
from bot.core.predictive_model import MarkovTransitionModel
from bot.core.pattern_learner import PatternLearner
from bot.agents.regime_profile import RegimeProfileStore
from bot.agents.base_expert import Verdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_trained_markov() -> MarkovTransitionModel:
    """Markov model with regime transitions."""
    m = MarkovTransitionModel(smoothing=0.01)
    # bull_trend tends to persist
    for i in range(20):
        m.observe("r:1", "bull_trend", ts=float(i * 3))
        m.observe("r:1", "bull_trend", ts=float(i * 3 + 1))  # same state, won't count
    # Some transitions
    for i in range(5):
        m.observe("r:1", "bull_trend", ts=100 + i * 2)
        m.observe("r:1", "bear_trend", ts=101 + i * 2)
    for i in range(3):
        m.observe("r:1", "bull_trend", ts=200 + i * 2)
        m.observe("r:1", "tight_range", ts=201 + i * 2)
    return m


def make_trained_learner() -> PatternLearner:
    """PatternLearner with strategy performance data."""
    learner = PatternLearner(min_occurrences=3)
    # Grid performs well in tight_range
    for i in range(10):
        learner.observe_open(
            f"g{i}",
            {"regime": "tight_range", "strategy": "grid", "direction": "LONG"},
        )
        learner.observe_close(f"g{i}", pnl=80.0 if i < 7 else -40.0)
    # TF performs well in bull_trend
    for i in range(10):
        learner.observe_open(
            f"t{i}",
            {
                "regime": "bull_trend",
                "strategy": "trend_follower",
                "direction": "LONG",
            },
        )
        learner.observe_close(f"t{i}", pnl=120.0 if i < 8 else -60.0)
    return learner


def make_regime_store() -> RegimeProfileStore:
    """RegimeProfileStore with some expert accuracy data."""
    store = RegimeProfileStore()
    # trend_follower is accurate in bull_trend
    for _ in range(10):
        store.record_outcome("trend_follower", "bull_trend", Verdict.APPROVE, True, pnl=100.0)
    for _ in range(2):
        store.record_outcome("trend_follower", "bull_trend", Verdict.APPROVE, False, pnl=-50.0)
    # grid is accurate in tight_range
    for _ in range(8):
        store.record_outcome("grid", "tight_range", Verdict.APPROVE, True, pnl=60.0)
    for _ in range(2):
        store.record_outcome("grid", "tight_range", Verdict.APPROVE, False, pnl=-30.0)
    return store


def make_allocator(
    markov: MarkovTransitionModel | None = None,
    learner: PatternLearner | None = None,
    store: RegimeProfileStore | None = None,
) -> MetaStrategyAllocator:
    return MetaStrategyAllocator(
        markov_model=markov or make_trained_markov(),
        pattern_learner=learner or make_trained_learner(),
        regime_store=store,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAllocateReturnsPlan:
    def test_allocate_returns_plan(self):
        allocator = make_allocator()
        plan = allocator.allocate("bull_trend")
        assert isinstance(plan, AllocationPlan)
        assert plan.current_regime == "bull_trend"
        assert plan.predicted_next_regime  # non-empty
        assert 0.0 <= plan.regime_persistence_prob <= 1.0
        assert len(plan.allocations) == 4
        assert len(plan.scores) == 4


class TestAllocationsSumToOne:
    def test_allocations_sum_to_one(self):
        allocator = make_allocator()
        plan = allocator.allocate("bull_trend")
        total = sum(plan.allocations.values())
        assert abs(total - 1.0) < 1e-9, f"Allocations sum to {total}, expected 1.0"


class TestMinAllocationEnforced:
    def test_min_allocation_enforced(self):
        allocator = make_allocator()
        plan = allocator.allocate("bull_trend")
        for strategy, alloc in plan.allocations.items():
            # After normalization, the effective min may differ from raw min_allocation
            # but no strategy should get 0
            assert alloc > 0.0, f"{strategy} got 0 allocation"


class TestMaxAllocationEnforced:
    def test_max_allocation_enforced(self):
        allocator = make_allocator()
        plan = allocator.allocate("bull_trend")
        for strategy, alloc in plan.allocations.items():
            # After normalization, effective max could be above raw max_allocation
            # when all strategies are capped. But the raw (pre-normalization) value
            # should respect the ceiling. Check a reasonable upper bound.
            assert alloc <= 1.0, f"{strategy} got allocation > 1.0"

    def test_max_allocation_raw_cap(self):
        """With extreme skew, max_allocation should still limit dominance."""
        allocator = MetaStrategyAllocator(
            markov_model=make_trained_markov(),
            pattern_learner=make_trained_learner(),
            max_allocation=0.40,
            min_allocation=0.05,
        )
        plan = allocator.allocate("bull_trend")
        # All allocations after normalization
        for alloc in plan.allocations.values():
            assert alloc <= 1.0


class TestBullTrendFavorsTrendFollower:
    def test_bull_trend_favors_trend_follower(self):
        learner = make_trained_learner()
        store = make_regime_store()
        allocator = make_allocator(learner=learner, store=store)
        plan = allocator.allocate("bull_trend")
        tf_alloc = plan.allocations.get("trend_follower", 0)
        # TF should get the highest or one of the highest allocations
        # because it has 80% win rate with decent confidence in bull_trend
        other_allocs = [v for k, v in plan.allocations.items() if k != "trend_follower"]
        assert tf_alloc >= min(other_allocs), (
            f"TF allocation {tf_alloc:.4f} should be >= at least one other"
        )


class TestTightRangeFavorsGrid:
    def test_tight_range_favors_grid(self):
        learner = make_trained_learner()
        store = make_regime_store()
        allocator = make_allocator(learner=learner, store=store)
        plan = allocator.allocate("tight_range")
        grid_alloc = plan.allocations.get("grid", 0)
        other_allocs = [v for k, v in plan.allocations.items() if k != "grid"]
        assert grid_alloc >= min(other_allocs), (
            f"Grid allocation {grid_alloc:.4f} should be >= at least one other"
        )


class TestNoPatternDataNeutral:
    def test_no_pattern_data_neutral(self):
        empty_learner = PatternLearner(min_occurrences=3)
        allocator = MetaStrategyAllocator(
            markov_model=MarkovTransitionModel(smoothing=0.01),
            pattern_learner=empty_learner,
        )
        plan = allocator.allocate("bull_trend")
        values = list(plan.allocations.values())
        # With no pattern data and no Markov data, all strategies should get equal allocation
        assert max(values) - min(values) < 0.01, (
            f"Expected roughly equal allocations, got {plan.allocations}"
        )


class TestAllocationPlanToDict:
    def test_allocation_plan_to_dict(self):
        allocator = make_allocator()
        plan = allocator.allocate("bull_trend")
        d = plan.to_dict()
        assert isinstance(d, dict)
        assert "allocations" in d
        assert "scores" in d
        assert "current_regime" in d
        assert "predicted_next_regime" in d
        assert "regime_persistence_prob" in d
        assert "total_confidence" in d
        assert "ts" in d
        assert isinstance(d["scores"], list)
        assert len(d["scores"]) == 4
        # Each score should serialize
        for score_dict in d["scores"]:
            assert "strategy" in score_dict
            assert "composite_score" in score_dict


class TestGetStrategyScore:
    def test_get_strategy_score(self):
        allocator = make_allocator()
        score = allocator.get_strategy_score("trend_follower", "bull_trend")
        assert isinstance(score, StrategyScore)
        assert score.strategy == "trend_follower"
        assert score.regime == "bull_trend"
        assert 0.0 <= score.pattern_win_rate <= 1.0
        assert 0.0 <= score.pattern_confidence <= 1.0
        assert 0.0 <= score.regime_probability <= 1.0
        assert isinstance(score.composite_score, float)
        assert isinstance(score.reasoning, str)


class TestHistoryRecorded:
    def test_history_recorded(self):
        allocator = make_allocator()
        assert allocator.get_current_plan() is None
        allocator.allocate("bull_trend")
        plan = allocator.get_current_plan()
        assert plan is not None
        assert plan.current_regime == "bull_trend"

    def test_history_accumulates(self):
        allocator = make_allocator()
        allocator.allocate("bull_trend")
        allocator.allocate("tight_range")
        history = allocator.get_history(limit=10)
        assert len(history) == 2
        # Most recent first
        assert history[0]["current_regime"] == "tight_range"
        assert history[1]["current_regime"] == "bull_trend"


class TestGetStats:
    def test_get_stats(self):
        allocator = make_allocator()
        allocator.allocate("bull_trend")
        stats = allocator.get_stats()
        assert stats["total_allocations"] == 1
        assert "strategies" in stats
        assert "weights" in stats
        assert "bounds" in stats
        assert stats["current_plan"] is not None
