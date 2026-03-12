from __future__ import annotations

import pytest

from bot.agents.meta.position_management import (
    DynamicStopManager,
    RiskParityAllocator,
    StopAction,
    StrategyRiskContribution,
)


# ===================================================================
# StopAction
# ===================================================================

class TestStopAction:
    def test_to_dict(self):
        sa = StopAction(action="trail", new_stop=105.0, exit_pct=0.0, reason="test")
        d = sa.to_dict()
        assert d == {
            "action": "trail",
            "new_stop": 105.0,
            "exit_pct": 0.0,
            "reason": "test",
        }

    def test_defaults(self):
        sa = StopAction(action="hold")
        assert sa.new_stop is None
        assert sa.exit_pct == 0.0
        assert sa.reason == ""


# ===================================================================
# DynamicStopManager — stateless evaluate()
# ===================================================================

class TestDynamicStopManagerEvaluate:
    def setup_method(self):
        self.mgr = DynamicStopManager(
            breakeven_trigger_r=1.0,
            partial_exit_trigger_r=2.0,
            partial_exit_pct=0.5,
            trail_activation_r=1.5,
            trail_distance_pct=0.02,
        )

    def test_hold_small_move(self):
        # entry=100, stop=95 → risk=5.  price=102 → profit=2 → R=0.4
        action = self.mgr.evaluate(100, 102, 95, 110)
        assert action.action == "hold"
        assert action.new_stop is None

    def test_breakeven_trigger(self):
        # R=1.0 exactly: profit=5, risk=5
        action = self.mgr.evaluate(100, 105, 95, 120)
        assert action.action == "move_to_breakeven"
        assert action.new_stop is not None
        assert action.new_stop > 100  # entry + small buffer

    def test_trail_activation(self):
        # R=1.6: profit=8, risk=5
        action = self.mgr.evaluate(100, 108, 95, 120, highest_since_entry=110)
        assert action.action == "trail"
        assert action.new_stop == pytest.approx(110 * 0.98)

    def test_partial_exit(self):
        # R=2.0: profit=10, risk=5
        action = self.mgr.evaluate(100, 110, 95, 120)
        assert action.action == "partial_exit"
        assert action.exit_pct == 0.5

    def test_partial_exit_already_done(self):
        # R=2.5 but already partially exited → should trail instead
        action = self.mgr.evaluate(
            100, 112.5, 95, 120,
            highest_since_entry=113,
            _has_partial_exited=True,
        )
        assert action.action == "trail"

    def test_short_direction_hold(self):
        # Short: entry=100, stop=105 → risk=5.  price=99 → profit=1 → R=0.2
        action = self.mgr.evaluate(100, 99, 105, 90, direction="short")
        assert action.action == "hold"

    def test_short_direction_breakeven(self):
        # Short: entry=100, stop=105, price=95 → profit=5, risk=5, R=1.0
        action = self.mgr.evaluate(100, 95, 105, 80, direction="short")
        assert action.action == "move_to_breakeven"
        assert action.new_stop < 100  # entry - buffer for short

    def test_short_direction_trail(self):
        # Short: entry=100, stop=105, price=92 → profit=8, risk=5, R=1.6
        action = self.mgr.evaluate(100, 92, 105, 80, direction="short", highest_since_entry=90)
        assert action.action == "trail"
        # For short, trail stop = lowest * (1 + distance)
        assert action.new_stop == pytest.approx(90 * 1.02)

    def test_zero_risk(self):
        action = self.mgr.evaluate(100, 105, 100, 110)
        assert action.action == "hold"
        assert "zero" in action.reason.lower()

    def test_negative_pnl_holds(self):
        # Price below entry (losing trade)
        action = self.mgr.evaluate(100, 93, 95, 110)
        assert action.action == "hold"


# ===================================================================
# DynamicStopManager — stateful tracking
# ===================================================================

class TestDynamicStopManagerTracking:
    def setup_method(self):
        self.mgr = DynamicStopManager(
            breakeven_trigger_r=1.0,
            partial_exit_trigger_r=2.0,
            partial_exit_pct=0.5,
            trail_activation_r=1.5,
            trail_distance_pct=0.02,
        )

    def test_track_and_update_hold(self):
        self.mgr.track_position("p1", 100, 95, 120, "long")
        result = self.mgr.update_position("p1", 101)
        assert result is None  # hold → returns None

    def test_track_and_update_breakeven(self):
        self.mgr.track_position("p1", 100, 95, 120, "long")
        result = self.mgr.update_position("p1", 105)
        assert result is not None
        assert result.action == "move_to_breakeven"

    def test_track_partial_exit_then_trail(self):
        self.mgr.track_position("p1", 100, 95, 120, "long")
        # First update: R=2 → partial exit
        r1 = self.mgr.update_position("p1", 110, high_since_entry=110)
        assert r1.action == "partial_exit"
        # Second update: R=2.5, already partially exited → trail
        r2 = self.mgr.update_position("p1", 112.5, high_since_entry=113)
        assert r2.action == "trail"

    def test_get_position_status(self):
        self.mgr.track_position("p1", 100, 95, 120, "long")
        status = self.mgr.get_position_status("p1")
        assert status is not None
        assert status["entry_price"] == 100
        assert status["has_partial_exited"] is False

    def test_get_position_status_unknown(self):
        assert self.mgr.get_position_status("unknown") is None

    def test_remove_position(self):
        self.mgr.track_position("p1", 100, 95, 120, "long")
        self.mgr.remove_position("p1")
        assert self.mgr.get_position_status("p1") is None

    def test_remove_nonexistent(self):
        # Should not raise
        self.mgr.remove_position("nope")

    def test_update_unknown_position(self):
        result = self.mgr.update_position("unknown", 100)
        assert result is None

    def test_get_stats(self):
        self.mgr.track_position("p1", 100, 95, 120, "long")
        self.mgr.track_position("p2", 200, 190, 220, "long")
        # Trigger partial exit on p1
        self.mgr.update_position("p1", 110)
        stats = self.mgr.get_stats()
        assert stats["total_tracked"] == 2
        assert stats["partial_exited"] == 1
        assert stats["active"] == 1


# ===================================================================
# StrategyRiskContribution
# ===================================================================

class TestStrategyRiskContribution:
    def test_to_dict(self):
        src = StrategyRiskContribution(
            strategy="grid",
            current_allocation=0.3,
            volatility=0.25,
            var_contribution=0.01,
            target_allocation=0.35,
            adjustment=0.05,
        )
        d = src.to_dict()
        assert d["strategy"] == "grid"
        assert d["adjustment"] == 0.05


# ===================================================================
# RiskParityAllocator
# ===================================================================

class TestRiskParityAllocator:
    def test_equal_vol_equal_allocation(self):
        rpa = RiskParityAllocator()
        rpa.set_strategy_vol("A", 0.20)
        rpa.set_strategy_vol("B", 0.20)
        allocs = rpa.calculate_allocations()
        assert allocs["A"].target_allocation == pytest.approx(0.5, abs=0.01)
        assert allocs["B"].target_allocation == pytest.approx(0.5, abs=0.01)

    def test_high_vol_lower_allocation(self):
        rpa = RiskParityAllocator(min_allocation=0.01, max_single_allocation=0.90)
        rpa.set_strategy_vol("low_vol", 0.10)
        rpa.set_strategy_vol("high_vol", 0.40)
        allocs = rpa.calculate_allocations()
        assert allocs["low_vol"].target_allocation > allocs["high_vol"].target_allocation

    def test_clamp_max(self):
        rpa = RiskParityAllocator(max_single_allocation=0.40)
        # One very low vol vs two high vol → unclamped would give >0.40
        rpa.set_strategy_vol("calm", 0.05)
        rpa.set_strategy_vol("wild1", 0.50)
        rpa.set_strategy_vol("wild2", 0.50)
        allocs = rpa.calculate_allocations()
        for src in allocs.values():
            assert src.target_allocation <= 0.40 + 1e-6

    def test_clamp_min(self):
        rpa = RiskParityAllocator(min_allocation=0.05)
        # One very high vol → unclamped would give tiny allocation
        rpa.set_strategy_vol("calm", 0.05)
        rpa.set_strategy_vol("wild", 5.0)
        allocs = rpa.calculate_allocations()
        assert allocs["wild"].target_allocation >= 0.05 - 1e-9

    def test_allocations_sum_to_one(self):
        rpa = RiskParityAllocator()
        rpa.set_strategy_vol("A", 0.15)
        rpa.set_strategy_vol("B", 0.30)
        rpa.set_strategy_vol("C", 0.45)
        allocs = rpa.calculate_allocations()
        total = sum(a.target_allocation for a in allocs.values())
        assert total == pytest.approx(1.0, abs=0.01)

    def test_single_strategy(self):
        rpa = RiskParityAllocator()
        rpa.set_strategy_vol("only", 0.20)
        allocs = rpa.calculate_allocations()
        assert allocs["only"].target_allocation == pytest.approx(1.0, abs=0.01)

    def test_empty(self):
        rpa = RiskParityAllocator()
        assert rpa.calculate_allocations() == {}

    def test_inverse_vol_weights(self):
        rpa = RiskParityAllocator()
        rpa.set_strategy_vol("A", 0.10)  # 1/0.10 = 10
        rpa.set_strategy_vol("B", 0.20)  # 1/0.20 = 5
        weights = rpa.get_inverse_vol_weights()
        # A should be 10/15 = 0.667, B should be 5/15 = 0.333
        assert weights["A"] == pytest.approx(2 / 3, abs=0.01)
        assert weights["B"] == pytest.approx(1 / 3, abs=0.01)

    def test_inverse_vol_weights_empty(self):
        rpa = RiskParityAllocator()
        assert rpa.get_inverse_vol_weights() == {}

    def test_rebalance_increase(self):
        rpa = RiskParityAllocator()
        rpa.set_strategy_vol("A", 0.20)
        rpa.set_strategy_vol("B", 0.20)
        # Target is 50/50; current is 30/70
        actions = rpa.get_rebalance_actions({"A": 0.30, "B": 0.70})
        a_action = next(a for a in actions if a["strategy"] == "A")
        b_action = next(a for a in actions if a["strategy"] == "B")
        assert a_action["action"] == "increase"
        assert b_action["action"] == "decrease"

    def test_rebalance_hold(self):
        rpa = RiskParityAllocator()
        rpa.set_strategy_vol("A", 0.20)
        rpa.set_strategy_vol("B", 0.20)
        # Current is close to target (within 2%)
        actions = rpa.get_rebalance_actions({"A": 0.49, "B": 0.51})
        assert all(a["action"] == "hold" for a in actions)

    def test_rebalance_delta_values(self):
        rpa = RiskParityAllocator()
        rpa.set_strategy_vol("A", 0.20)
        rpa.set_strategy_vol("B", 0.20)
        actions = rpa.get_rebalance_actions({"A": 0.30, "B": 0.70})
        a_action = next(a for a in actions if a["strategy"] == "A")
        assert a_action["delta_pct"] == pytest.approx(0.20, abs=0.02)
        assert a_action["from_pct"] == 0.30
        assert a_action["to_pct"] == pytest.approx(0.50, abs=0.02)

    def test_negative_vol_raises(self):
        rpa = RiskParityAllocator()
        with pytest.raises(ValueError):
            rpa.set_strategy_vol("bad", -0.1)

    def test_zero_vol_raises(self):
        rpa = RiskParityAllocator()
        with pytest.raises(ValueError):
            rpa.set_strategy_vol("bad", 0.0)

    def test_get_stats(self):
        rpa = RiskParityAllocator(target_total_risk=0.15)
        rpa.set_strategy_vol("X", 0.10)
        rpa.set_strategy_vol("Y", 0.30)
        stats = rpa.get_stats()
        assert stats["num_strategies"] == 2
        assert set(stats["strategies"]) == {"X", "Y"}
        assert stats["target_total_risk"] == 0.15
        assert "allocations" in stats
