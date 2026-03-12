from __future__ import annotations

import pytest

from bot.agents.analytics.execution import (
    ExecutionAlgo,
    ExecutionOptimizer,
    ExecutionPlan,
    MarketMicrostructure,
)


# ======================================================================
# ExecutionPlan
# ======================================================================


class TestExecutionPlan:
    def test_to_dict(self):
        plan = ExecutionPlan(
            algo=ExecutionAlgo.MARKET,
            total_qty=100.0,
            slices=[{"qty": 100.0, "delay_seconds": 0.0, "price_limit": None}],
            estimated_slippage_pct=0.001,
            estimated_duration_seconds=0.0,
        )
        d = plan.to_dict()
        assert d["algo"] == "market"
        assert d["total_qty"] == 100.0
        assert len(d["slices"]) == 1
        assert d["estimated_slippage_pct"] == 0.001


# ======================================================================
# ExecutionOptimizer
# ======================================================================


class TestExecutionOptimizer:
    def test_market_order_small_qty(self):
        """Order size < max_single_order_pct of avg_volume → MARKET."""
        opt = ExecutionOptimizer(max_single_order_pct=0.02)
        plan = opt.plan_execution(
            total_qty=10.0,
            current_price=50000.0,
            avg_volume_24h=100_000.0,
        )
        assert plan.algo == ExecutionAlgo.MARKET
        assert len(plan.slices) == 1
        assert plan.slices[0]["qty"] == 10.0
        assert plan.estimated_duration_seconds == 0.0

    def test_twap_high_urgency(self):
        """High urgency with large order → TWAP with fewer slices."""
        opt = ExecutionOptimizer(default_slice_count=10)
        plan = opt.plan_execution(
            total_qty=5000.0,
            current_price=50000.0,
            avg_volume_24h=100_000.0,
            urgency="high",
        )
        assert plan.algo == ExecutionAlgo.TWAP
        assert len(plan.slices) == 5  # 10 // 2
        total = sum(s["qty"] for s in plan.slices)
        assert abs(total - 5000.0) < 1e-9

    def test_twap_equal_slices(self):
        opt = ExecutionOptimizer()
        slices = opt.generate_twap_slices(1000.0, 600.0, 5)
        assert len(slices) == 5
        for s in slices:
            assert abs(s["qty"] - 200.0) < 1e-9
        # Delays are at equal intervals
        assert slices[0]["delay_seconds"] == 0.0
        assert abs(slices[1]["delay_seconds"] - 120.0) < 1e-9

    def test_vwap_low_urgency(self):
        """Low urgency → VWAP with more slices."""
        opt = ExecutionOptimizer(default_slice_count=10)
        plan = opt.plan_execution(
            total_qty=5000.0,
            current_price=50000.0,
            avg_volume_24h=100_000.0,
            urgency="low",
        )
        assert plan.algo == ExecutionAlgo.VWAP
        assert len(plan.slices) == 20  # 10 * 2
        total = sum(s["qty"] for s in plan.slices)
        assert abs(total - 5000.0) < 1e-9

    def test_vwap_with_volume_profile(self):
        opt = ExecutionOptimizer()
        profile = [3.0, 1.0, 1.0, 1.0, 4.0]  # U-shaped
        slices = opt.generate_vwap_slices(1000.0, profile, 5)
        assert len(slices) == 5
        total = sum(s["qty"] for s in slices)
        assert abs(total - 1000.0) < 1e-9
        # First and last slices should be larger
        assert slices[0]["qty"] > slices[2]["qty"]
        assert slices[4]["qty"] > slices[2]["qty"]

    def test_vwap_default_u_shape(self):
        opt = ExecutionOptimizer()
        slices = opt.generate_vwap_slices(1000.0, None, 5)
        assert len(slices) == 5
        # U-shape: edges > middle
        assert slices[0]["qty"] > slices[2]["qty"]
        assert slices[4]["qty"] > slices[2]["qty"]

    def test_iceberg_slices(self):
        opt = ExecutionOptimizer(default_slice_count=10)
        slices = opt.generate_iceberg_slices(1000.0, visible_pct=0.2)
        total = sum(s["qty"] for s in slices)
        assert abs(total - 1000.0) < 1e-9
        # Each slice ≤ 200 (20% of 1000)
        for s in slices:
            assert s["qty"] <= 200.0 + 1e-9
        # First slice has delay 0
        assert slices[0]["delay_seconds"] == 0.0
        # Subsequent slices have delay in [5, 30]
        for s in slices[1:]:
            assert 5.0 <= s["delay_seconds"] <= 30.0

    def test_iceberg_default_algo(self):
        """Default urgency ('normal') with large order → ICEBERG."""
        opt = ExecutionOptimizer()
        plan = opt.plan_execution(
            total_qty=5000.0,
            current_price=50000.0,
            avg_volume_24h=100_000.0,
            urgency="normal",
        )
        assert plan.algo == ExecutionAlgo.ICEBERG

    def test_slippage_estimation(self):
        opt = ExecutionOptimizer(impact_factor=0.1)
        slip = opt.estimate_slippage(1000.0, 100_000.0, spread_pct=0.001)
        # spread/2 + (1000/100000)*0.1 = 0.0005 + 0.001 = 0.0015
        assert abs(slip - 0.0015) < 1e-9

    def test_slippage_zero_volume(self):
        opt = ExecutionOptimizer()
        slip = opt.estimate_slippage(100.0, 0.0)
        # volume_ratio=1.0 → spread/2 + 1.0*0.1 = 0.0005 + 0.1
        assert slip > 0.1

    def test_record_execution_and_stats(self):
        opt = ExecutionOptimizer()
        plan = ExecutionPlan(
            algo=ExecutionAlgo.MARKET,
            total_qty=100.0,
            estimated_slippage_pct=0.001,
        )
        opt.record_execution(plan, actual_avg_price=50050.0, planned_price=50000.0)
        opt.record_execution(plan, actual_avg_price=50100.0, planned_price=50000.0)

        stats = opt.get_slippage_stats()
        assert stats["count"] == 2
        assert stats["min"] == pytest.approx(0.001, abs=1e-6)
        assert stats["max"] == pytest.approx(0.002, abs=1e-6)
        assert stats["avg"] == pytest.approx(0.0015, abs=1e-6)

    def test_slippage_stats_empty(self):
        opt = ExecutionOptimizer()
        stats = opt.get_slippage_stats()
        assert stats["count"] == 0

    def test_get_stats(self):
        opt = ExecutionOptimizer(default_slice_count=8, max_single_order_pct=0.05)
        s = opt.get_stats()
        assert s["default_slice_count"] == 8
        assert s["max_single_order_pct"] == 0.05
        assert s["executions_recorded"] == 0


# ======================================================================
# MarketMicrostructure
# ======================================================================


class TestMarketMicrostructure:
    def test_observe_ticks(self):
        mm = MarketMicrostructure(window_size=5)
        for i in range(7):
            mm.observe_tick(100.0 + i, 10.0, "buy", ts=float(i))
        # Window holds only last 5
        assert len(mm._ticks) == 5

    def test_vpin_balanced_flow(self):
        """Equal buy and sell volume → VPIN near 0."""
        mm = MarketMicrostructure()
        for i in range(50):
            mm.observe_tick(100.0, 100.0, "buy", ts=float(i * 2))
            mm.observe_tick(100.0, 100.0, "sell", ts=float(i * 2 + 1))
        vpin = mm.calculate_vpin(bucket_size=500.0)
        assert vpin < 0.25  # nearly balanced

    def test_vpin_imbalanced_flow(self):
        """All buys → VPIN = 1.0."""
        mm = MarketMicrostructure()
        for i in range(50):
            mm.observe_tick(100.0, 100.0, "buy", ts=float(i))
        vpin = mm.calculate_vpin(bucket_size=500.0)
        assert vpin == pytest.approx(1.0)

    def test_vpin_empty(self):
        mm = MarketMicrostructure()
        assert mm.calculate_vpin() == 0.0

    def test_spread_metrics_basic(self):
        mm = MarketMicrostructure()
        # Alternating buy/sell with $1 spread
        for i in range(20):
            mm.observe_tick(100.0, 10.0, "buy", ts=float(i * 2))
            mm.observe_tick(101.0, 10.0, "sell", ts=float(i * 2 + 1))
        metrics = mm.calculate_spread_metrics()
        assert metrics["avg_spread"] == pytest.approx(1.0, abs=0.01)
        assert metrics["realized_spread"] > 0
        assert metrics["effective_spread"] > 0

    def test_spread_metrics_insufficient_ticks(self):
        mm = MarketMicrostructure()
        mm.observe_tick(100.0, 10.0, "buy", ts=1.0)
        metrics = mm.calculate_spread_metrics()
        assert metrics["avg_spread"] == 0.0

    def test_tick_patterns_runs(self):
        mm = MarketMicrostructure()
        # 5 buys then 5 sells → 2 runs
        for i in range(5):
            mm.observe_tick(100.0, 10.0, "buy", ts=float(i))
        for i in range(5):
            mm.observe_tick(100.0, 10.0, "sell", ts=float(i + 5))
        pat = mm.detect_tick_patterns()
        assert pat["tick_direction_runs"] == 2
        assert pat["avg_run_length"] == 5.0
        assert pat["reversal_rate"] == pytest.approx(1.0 / 9.0, abs=0.01)

    def test_tick_patterns_large_trade(self):
        mm = MarketMicrostructure()
        # 9 normal + 1 large
        for i in range(9):
            mm.observe_tick(100.0, 10.0, "buy", ts=float(i))
        mm.observe_tick(100.0, 100.0, "buy", ts=9.0)  # 10x average
        pat = mm.detect_tick_patterns()
        assert pat["large_trade_ratio"] > 0

    def test_tick_patterns_empty(self):
        mm = MarketMicrostructure()
        pat = mm.detect_tick_patterns()
        assert pat["tick_direction_runs"] == 0
        assert pat["avg_run_length"] == 0.0

    def test_toxicity_low(self):
        mm = MarketMicrostructure()
        # Balanced flow, tight spread
        for i in range(50):
            mm.observe_tick(100.0, 10.0, "buy", ts=float(i * 2))
            mm.observe_tick(100.01, 10.0, "sell", ts=float(i * 2 + 1))
        tox = mm.get_toxicity_score()
        assert tox["score"] < 0.5
        assert tox["level"] in ("low", "medium")
        assert "vpin" in tox["components"]

    def test_toxicity_high(self):
        mm = MarketMicrostructure()
        # All buys, large spread
        for i in range(50):
            mm.observe_tick(100.0 + i * 0.5, 100.0, "buy", ts=float(i))
        tox = mm.get_toxicity_score()
        assert tox["score"] >= 0.5
        assert tox["level"] in ("high", "toxic")

    def test_toxicity_empty(self):
        mm = MarketMicrostructure()
        tox = mm.get_toxicity_score()
        assert tox["score"] == 0.0
        assert tox["level"] == "low"

    def test_single_tick(self):
        mm = MarketMicrostructure()
        mm.observe_tick(100.0, 50.0, "buy", ts=1.0)
        assert mm.calculate_vpin() > 0
        metrics = mm.calculate_spread_metrics()
        assert metrics["avg_spread"] == 0.0
        pat = mm.detect_tick_patterns()
        assert pat["tick_direction_runs"] == 1

    def test_get_stats(self):
        mm = MarketMicrostructure(window_size=50)
        mm.observe_tick(100.0, 10.0, "buy", ts=1.0)
        s = mm.get_stats()
        assert s["window_size"] == 50
        assert s["ticks_observed"] == 1
        assert "toxicity" in s
