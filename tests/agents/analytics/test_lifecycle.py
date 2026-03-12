from __future__ import annotations

import pytest

from bot.agents.analytics.lifecycle import (
    DrawdownRecoveryProtocol,
    StrategyLifecycleManager,
    StrategyPhase,
)


# ======================================================================
# StrategyLifecycleManager tests
# ======================================================================


class TestLifecycleBirth:
    def test_new_strategy_is_birth(self):
        mgr = StrategyLifecycleManager(min_trades_birth=10)
        assert mgr.get_phase("strat_a") == StrategyPhase.BIRTH

    def test_under_min_trades_stays_birth(self):
        mgr = StrategyLifecycleManager(min_trades_birth=10)
        for i in range(9):
            mgr.record_trade("strat_a", 1.0, ts=float(i))
        assert mgr.get_phase("strat_a") == StrategyPhase.BIRTH


class TestLifecycleGrowth:
    def test_growth_transition(self):
        mgr = StrategyLifecycleManager(
            min_trades_birth=5, growth_threshold=0.6, decline_threshold=0.4
        )
        # 5 small wins then 15 bigger wins (improving trend)
        for i in range(5):
            mgr.record_trade("strat_a", 0.5, ts=float(i))
        for i in range(15):
            mgr.record_trade("strat_a", 2.0, ts=float(5 + i))
        assert mgr.get_phase("strat_a") == StrategyPhase.GROWTH

    def test_growth_recommendation(self):
        mgr = StrategyLifecycleManager(min_trades_birth=5, growth_threshold=0.6)
        for i in range(5):
            mgr.record_trade("s", 0.5, ts=float(i))
        for i in range(15):
            mgr.record_trade("s", 2.0, ts=float(5 + i))
        rec = mgr.get_recommendation("s")
        assert rec["action"] == "increase"
        assert rec["allocation_pct"] == 1.2


class TestLifecycleMaturity:
    def test_maturity_detection(self):
        mgr = StrategyLifecycleManager(
            min_trades_birth=5, growth_threshold=0.6, decline_threshold=0.4
        )
        # Stable positive results with no improving trend
        for i in range(20):
            mgr.record_trade("strat_a", 1.0, ts=float(i))
        # win_rate=1.0 > 0.6, avg_pnl>0 but trend=0 → MATURITY
        assert mgr.get_phase("strat_a") == StrategyPhase.MATURITY

    def test_maturity_recommendation(self):
        mgr = StrategyLifecycleManager(min_trades_birth=5)
        for i in range(20):
            mgr.record_trade("s", 1.0, ts=float(i))
        rec = mgr.get_recommendation("s")
        assert rec["action"] == "maintain"
        assert rec["allocation_pct"] == 1.0


class TestLifecycleDecline:
    def test_decline_low_winrate(self):
        mgr = StrategyLifecycleManager(
            min_trades_birth=5, decline_threshold=0.4
        )
        # 5 wins then 15 losses → win_rate in window < 0.4
        for i in range(5):
            mgr.record_trade("strat_a", 1.0, ts=float(i))
        for i in range(15):
            mgr.record_trade("strat_a", -1.0, ts=float(5 + i))
        assert mgr.get_phase("strat_a") == StrategyPhase.DECLINE

    def test_decline_negative_avg_pnl(self):
        mgr = StrategyLifecycleManager(min_trades_birth=5, decline_threshold=0.4)
        # Many small wins but a few huge losses → avg_pnl < 0
        for i in range(10):
            mgr.record_trade("strat_a", 0.1, ts=float(i))
        for i in range(10):
            mgr.record_trade("strat_a", -5.0, ts=float(10 + i))
        assert mgr.get_phase("strat_a") == StrategyPhase.DECLINE

    def test_decline_recommendation(self):
        mgr = StrategyLifecycleManager(min_trades_birth=5, decline_threshold=0.4)
        for i in range(20):
            mgr.record_trade("s", -1.0, ts=float(i))
        rec = mgr.get_recommendation("s")
        assert rec["action"] == "reduce"
        assert rec["allocation_pct"] == 0.3


class TestLifecycleRetirement:
    def test_retirement_after_prolonged_decline(self):
        mgr = StrategyLifecycleManager(
            min_trades_birth=5, decline_threshold=0.4, retirement_trades=10
        )
        # Get past birth
        for i in range(5):
            mgr.record_trade("strat_a", 1.0, ts=float(i))
        # Now 10 losing trades in decline
        for i in range(10):
            mgr.record_trade("strat_a", -2.0, ts=float(5 + i))
        assert mgr.get_phase("strat_a") == StrategyPhase.RETIRED

    def test_should_retire(self):
        mgr = StrategyLifecycleManager(
            min_trades_birth=5, decline_threshold=0.4, retirement_trades=10
        )
        for i in range(5):
            mgr.record_trade("strat_a", 1.0, ts=float(i))
        for i in range(10):
            mgr.record_trade("strat_a", -2.0, ts=float(5 + i))
        assert mgr.should_retire("strat_a") is True

    def test_retired_stays_retired(self):
        mgr = StrategyLifecycleManager(
            min_trades_birth=5, decline_threshold=0.4, retirement_trades=5
        )
        for i in range(5):
            mgr.record_trade("s", 1.0, ts=float(i))
        for i in range(5):
            mgr.record_trade("s", -2.0, ts=float(5 + i))
        assert mgr.get_phase("s") == StrategyPhase.RETIRED
        # Even winning trades don't revive
        for i in range(10):
            mgr.record_trade("s", 10.0, ts=float(10 + i))
        assert mgr.get_phase("s") == StrategyPhase.RETIRED

    def test_retirement_recommendation(self):
        mgr = StrategyLifecycleManager(
            min_trades_birth=5, decline_threshold=0.4, retirement_trades=5
        )
        for i in range(5):
            mgr.record_trade("s", 1.0, ts=float(i))
        for i in range(5):
            mgr.record_trade("s", -2.0, ts=float(5 + i))
        rec = mgr.get_recommendation("s")
        assert rec["action"] == "disable"
        assert rec["allocation_pct"] == 0.0


class TestLifecycleUnknownStrategy:
    def test_unknown_returns_birth_phase(self):
        mgr = StrategyLifecycleManager()
        assert mgr.get_phase("nonexistent") == StrategyPhase.BIRTH

    def test_unknown_recommendation(self):
        mgr = StrategyLifecycleManager()
        rec = mgr.get_recommendation("nonexistent")
        assert rec["action"] == "monitor"


class TestLifecycleMultipleStrategies:
    def test_independent_tracking(self):
        mgr = StrategyLifecycleManager(min_trades_birth=5, decline_threshold=0.4)
        # strat_a: winning → growth/maturity
        for i in range(20):
            mgr.record_trade("strat_a", 1.0 + i * 0.1, ts=float(i))
        # strat_b: losing → decline
        for i in range(20):
            mgr.record_trade("strat_b", -1.0, ts=float(i))
        phases = mgr.get_all_phases()
        assert phases["strat_a"] in (StrategyPhase.GROWTH, StrategyPhase.MATURITY)
        assert phases["strat_b"] == StrategyPhase.DECLINE

    def test_get_stats_multiple(self):
        mgr = StrategyLifecycleManager(min_trades_birth=3)
        mgr.record_trade("a", 1.0, ts=0.0)
        mgr.record_trade("b", -1.0, ts=0.0)
        stats = mgr.get_stats()
        assert "a" in stats
        assert "b" in stats
        assert stats["a"]["trade_count"] == 1
        assert stats["b"]["trade_count"] == 1


# ======================================================================
# DrawdownRecoveryProtocol tests
# ======================================================================


class TestDrawdownNormal:
    def test_no_drawdown_normal_mode(self):
        drp = DrawdownRecoveryProtocol(trigger_pct=0.10)
        drp.update(10000.0)
        assert drp.is_recovery_mode() is False

    def test_normal_risk_multiplier(self):
        drp = DrawdownRecoveryProtocol()
        drp.update(10000.0)
        assert drp.get_risk_multiplier() == 1.0

    def test_normal_allows_any_trade(self):
        drp = DrawdownRecoveryProtocol()
        drp.update(10000.0)
        assert drp.should_allow_trade(0.1) is True


class TestDrawdownTrigger:
    def test_trigger_recovery(self):
        drp = DrawdownRecoveryProtocol(trigger_pct=0.10)
        drp.update(10000.0)
        drp.update(8900.0)  # 11% drawdown
        assert drp.is_recovery_mode() is True

    def test_just_below_trigger_no_recovery(self):
        drp = DrawdownRecoveryProtocol(trigger_pct=0.10)
        drp.update(10000.0)
        drp.update(9100.0)  # 9% drawdown
        assert drp.is_recovery_mode() is False


class TestDrawdownRecoveryExit:
    def test_recovery_exit(self):
        drp = DrawdownRecoveryProtocol(trigger_pct=0.10, recovery_pct=0.05)
        drp.update(10000.0)
        drp.update(8900.0)  # trigger
        assert drp.is_recovery_mode() is True
        drp.update(9600.0)  # 4% dd, below recovery_pct
        assert drp.is_recovery_mode() is False

    def test_stays_in_recovery_until_threshold(self):
        drp = DrawdownRecoveryProtocol(trigger_pct=0.10, recovery_pct=0.05)
        drp.update(10000.0)
        drp.update(8900.0)  # trigger
        drp.update(9400.0)  # 6% dd, still above recovery_pct
        assert drp.is_recovery_mode() is True


class TestDrawdownRiskMultiplier:
    def test_recovery_multiplier(self):
        drp = DrawdownRecoveryProtocol(
            trigger_pct=0.10, conservative_risk_mult=0.5
        )
        drp.update(10000.0)
        drp.update(8900.0)  # 11% dd
        mult = drp.get_risk_multiplier()
        assert mult < 0.5  # slightly below conservative due to scaling
        assert mult >= 0.2

    def test_deep_drawdown_multiplier(self):
        drp = DrawdownRecoveryProtocol(
            trigger_pct=0.10, conservative_risk_mult=0.5
        )
        drp.update(10000.0)
        drp.update(3000.0)  # 70% drawdown
        mult = drp.get_risk_multiplier()
        assert mult <= 0.35
        assert mult >= 0.2

    def test_multiplier_scales_linearly(self):
        drp = DrawdownRecoveryProtocol(
            trigger_pct=0.10, conservative_risk_mult=0.5
        )
        drp.update(10000.0)
        drp.update(8500.0)  # 15%
        mult_15 = drp.get_risk_multiplier()
        drp.update(7000.0)  # 30%
        mult_30 = drp.get_risk_multiplier()
        assert mult_15 > mult_30  # deeper → lower


class TestDrawdownTradeBlocking:
    def test_blocks_low_confidence_in_recovery(self):
        drp = DrawdownRecoveryProtocol(
            trigger_pct=0.10, min_confidence=0.7
        )
        drp.update(10000.0)
        drp.update(8900.0)
        assert drp.should_allow_trade(0.5) is False

    def test_allows_high_confidence_in_recovery(self):
        drp = DrawdownRecoveryProtocol(
            trigger_pct=0.10, min_confidence=0.7
        )
        drp.update(10000.0)
        drp.update(8900.0)
        assert drp.should_allow_trade(0.8) is True

    def test_trades_blocked_counter(self):
        drp = DrawdownRecoveryProtocol(trigger_pct=0.10, min_confidence=0.7)
        drp.update(10000.0)
        drp.update(8900.0)
        drp.should_allow_trade(0.3)
        drp.should_allow_trade(0.4)
        drp.should_allow_trade(0.9)  # allowed
        status = drp.get_status()
        assert status["trades_blocked"] == 2


class TestDrawdownStatus:
    def test_status_normal(self):
        drp = DrawdownRecoveryProtocol()
        drp.update(10000.0)
        status = drp.get_status()
        assert status["mode"] == "normal"
        assert status["drawdown_pct"] == 0.0
        assert status["peak"] == 10000.0
        assert status["risk_multiplier"] == 1.0

    def test_status_recovery(self):
        drp = DrawdownRecoveryProtocol(trigger_pct=0.10, recovery_pct=0.05)
        drp.update(10000.0)
        drp.update(8500.0)
        status = drp.get_status()
        assert status["mode"] == "recovery"
        assert status["drawdown_pct"] == pytest.approx(0.15)
        assert 0.0 <= status["recovery_progress"] <= 1.0

    def test_get_stats(self):
        drp = DrawdownRecoveryProtocol(trigger_pct=0.10)
        drp.update(10000.0)
        drp.update(8500.0)
        stats = drp.get_stats()
        assert stats["peak"] == 10000.0
        assert stats["current"] == 8500.0
        assert stats["in_recovery"] is True
        assert stats["trigger_pct"] == 0.10


class TestDrawdownPeakTracking:
    def test_peak_value_parameter(self):
        drp = DrawdownRecoveryProtocol(trigger_pct=0.10)
        drp.update(5000.0, peak_value=10000.0)
        # peak should be 10000 (from parameter), current 5000
        assert drp.is_recovery_mode() is True
        status = drp.get_status()
        assert status["peak"] == 10000.0

    def test_peak_auto_tracks(self):
        drp = DrawdownRecoveryProtocol()
        drp.update(8000.0)
        drp.update(10000.0)
        drp.update(9500.0)
        status = drp.get_status()
        assert status["peak"] == 10000.0
