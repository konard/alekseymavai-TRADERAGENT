"""
Unit tests for bot/core/portfolio_risk_manager.py

Tests cover:
- SharedCapitalPool: allocate/release/utilization
- PortfolioRiskManager: check_allocation, portfolio halt, correlation
- PortfolioRiskManager: per-symbol global stop-loss, force_close_all,
  symbol cooldown, get_risk_report
"""

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from bot.core.portfolio_risk_manager import (
    BotAllocation,
    PortfolioRiskManager,
    RiskCheckStatus,
    SharedCapitalPool,
    StrategyPositionProvider,
)


# ---------------------------------------------------------------------------
# SharedCapitalPool
# ---------------------------------------------------------------------------


class TestSharedCapitalPool:
    def _pool(self, total: float = 10000.0, max_util: float = 0.80) -> SharedCapitalPool:
        return SharedCapitalPool(
            total_capital=Decimal(str(total)),
            max_utilization_pct=max_util,
        )

    def test_allocate_success(self) -> None:
        pool = self._pool()
        assert pool.allocate("bot1", Decimal("1000")) is True
        assert pool.total_allocated == Decimal("1000")

    def test_allocate_respects_global_cap(self) -> None:
        pool = self._pool(total=1000.0, max_util=0.50)
        assert pool.allocate("bot1", Decimal("400")) is True
        # Second allocation would exceed 50%
        assert pool.allocate("bot2", Decimal("200")) is False

    def test_allocate_respects_individual_cap(self) -> None:
        pool = self._pool()
        pool.register_bot("bot1", max_limit=Decimal("500"))
        assert pool.allocate("bot1", Decimal("400")) is True
        assert pool.allocate("bot1", Decimal("200")) is False  # would exceed 500

    def test_release_returns_capital(self) -> None:
        pool = self._pool()
        pool.allocate("bot1", Decimal("1000"))
        pool.release("bot1", Decimal("500"))
        assert pool.total_allocated == Decimal("500")

    def test_release_clamps_at_zero(self) -> None:
        pool = self._pool()
        pool.release("nonexistent_bot", Decimal("999"))  # should not raise

    def test_get_utilization(self) -> None:
        pool = self._pool(total=10000.0)
        pool.allocate("bot1", Decimal("5000"))
        assert abs(pool.get_utilization() - 0.5) < 0.001

    def test_get_available(self) -> None:
        pool = self._pool(total=10000.0, max_util=0.80)
        pool.allocate("bot1", Decimal("2000"))
        expected = Decimal("10000") * Decimal("0.80") - Decimal("2000")
        assert pool.get_available() == expected

    def test_summary_structure(self) -> None:
        pool = self._pool()
        pool.allocate("bot1", Decimal("100"))
        summary = pool.get_summary()
        assert "total_capital" in summary
        assert "bots" in summary
        assert "bot1" in summary["bots"]

    def test_update_deployed(self) -> None:
        pool = self._pool()
        pool.allocate("bot1", Decimal("100"))
        pool.update_deployed("bot1", Decimal("80"))
        assert pool.total_deployed == Decimal("80")


# ---------------------------------------------------------------------------
# PortfolioRiskManager
# ---------------------------------------------------------------------------


class TestPortfolioRiskManager:
    def _prm(
        self,
        total: float = 10000.0,
        max_exposure: float = 0.80,
        max_pair: float = 0.25,
        stop_loss: float = 0.15,
    ) -> PortfolioRiskManager:
        return PortfolioRiskManager(
            total_capital=Decimal(str(total)),
            max_total_exposure_pct=max_exposure,
            max_single_pair_pct=max_pair,
            portfolio_stop_loss_pct=stop_loss,
        )

    def test_check_allocation_approved(self) -> None:
        prm = self._prm()
        result = prm.check_allocation("bot1", Decimal("1000"))
        assert result.approved is True
        assert result.status == RiskCheckStatus.APPROVED

    def test_check_allocation_exceeds_pair_limit(self) -> None:
        prm = self._prm(total=10000.0, max_pair=0.25)
        # 3000 > 25% of 10000 balance
        result = prm.check_allocation("bot1", Decimal("3000"), balance=Decimal("10000"))
        assert result.approved is False
        assert result.status == RiskCheckStatus.REJECTED_PAIR_LIMIT

    def test_check_allocation_exceeds_total_exposure(self) -> None:
        prm = self._prm(total=1000.0, max_exposure=0.5, max_pair=0.8)
        # First allocation fills pool
        prm.confirm_allocation("bot1", Decimal("500"))
        # Second allocation would exceed 50% pool cap
        result = prm.check_allocation("bot2", Decimal("100"))
        assert result.approved is False
        assert result.status == RiskCheckStatus.REJECTED_EXPOSURE

    def test_portfolio_halt_on_drawdown(self) -> None:
        prm = self._prm(total=10000.0, stop_loss=0.15)
        # Simulate 20% drawdown
        prm.update_all_balances({"bot1": Decimal("8000")})
        assert prm.is_portfolio_halted() is True

    def test_portfolio_halt_blocks_allocation(self) -> None:
        prm = self._prm(total=10000.0, stop_loss=0.15)
        prm.update_all_balances({"bot1": Decimal("8000")})  # triggers halt
        result = prm.check_allocation("bot2", Decimal("100"))
        assert result.approved is False
        assert result.status == RiskCheckStatus.REJECTED_PORTFOLIO_HALTED

    def test_portfolio_resume_clears_halt(self) -> None:
        prm = self._prm(total=10000.0, stop_loss=0.15)
        prm.update_all_balances({"bot1": Decimal("8000")})
        assert prm.is_portfolio_halted() is True
        prm.resume()
        assert prm.is_portfolio_halted() is False
        # After resume, allocation should succeed
        result = prm.check_allocation("bot2", Decimal("100"))
        assert result.approved is True

    def test_no_halt_on_small_drawdown(self) -> None:
        prm = self._prm(total=10000.0, stop_loss=0.15)
        prm.update_all_balances({"bot1": Decimal("9000")})  # 10% drawdown < 15%
        assert prm.is_portfolio_halted() is False

    def test_confirm_and_release(self) -> None:
        prm = self._prm()
        prm.confirm_allocation("bot1", Decimal("1000"), symbol="BTC/USDT")
        assert "BTC/USDT" in prm._active_symbols
        prm.release_allocation("bot1", Decimal("1000"), symbol="BTC/USDT")
        assert "BTC/USDT" not in prm._active_symbols

    def test_correlation_check_btc_eth(self) -> None:
        prm = self._prm(max_pair=0.50, max_exposure=0.90)
        prm.confirm_allocation("bot_btc", Decimal("1000"), symbol="BTC/USDT")
        # ETH is correlated with BTC — should be blocked
        result = prm.check_allocation("bot_eth", Decimal("500"), symbol="ETH/USDT")
        assert result.approved is False
        assert result.status == RiskCheckStatus.REJECTED_CORRELATION

    def test_correlation_override(self) -> None:
        prm = self._prm(max_pair=0.50, max_exposure=0.90)
        # Override correlation to be low
        prm.set_correlation("BTC/USDT", "ETH/USDT", 0.3)
        prm.confirm_allocation("bot_btc", Decimal("1000"), symbol="BTC/USDT")
        result = prm.check_allocation("bot_eth", Decimal("500"), symbol="ETH/USDT")
        assert result.approved is True

    def test_get_summary(self) -> None:
        prm = self._prm()
        summary = prm.get_summary()
        assert "total_capital" in summary
        assert "halted" in summary
        assert "pool" in summary

    def test_peak_updates_correctly(self) -> None:
        prm = self._prm(total=10000.0, stop_loss=0.15)
        # Balance grows — peak should update
        prm.update_all_balances({"bot1": Decimal("11000")})
        assert prm._peak_value == Decimal("11000")
        # Now drawdown from 11000 — 14% drawdown does not halt
        prm.update_all_balances({"bot1": Decimal("9500")})
        assert prm.is_portfolio_halted() is False
        # 16% drawdown from 11000 — should halt
        prm.update_all_balances({"bot1": Decimal("9240")})
        assert prm.is_portfolio_halted() is True


# ---------------------------------------------------------------------------
# Helpers for per-symbol tests
# ---------------------------------------------------------------------------


def _make_provider(
    symbol: str,
    size: float,
    atr_stop_distance: float,
) -> StrategyPositionProvider:
    """Create a mock position provider returning a single open position."""
    provider = MagicMock(spec=StrategyPositionProvider)
    provider.get_open_positions.return_value = [
        {
            "symbol": symbol,
            "size": Decimal(str(size)),
            "atr_stop_distance": Decimal(str(atr_stop_distance)),
        }
    ]
    return provider


def _make_prm_with_global_stop(
    total: float = 10000.0,
    global_stop_pct: float = 0.025,  # 2.5%
    cooldown: int = 1800,
) -> PortfolioRiskManager:
    return PortfolioRiskManager(
        total_capital=Decimal(str(total)),
        global_stop_loss_pct=global_stop_pct,
        force_close_cooldown_seconds=cooldown,
    )


# ---------------------------------------------------------------------------
# Per-symbol exposure aggregation
# ---------------------------------------------------------------------------


class TestGetTotalExposure:
    def test_no_providers_returns_zero(self) -> None:
        prm = _make_prm_with_global_stop()
        assert prm._get_total_exposure("BTC/USDT") == Decimal("0")

    def test_single_provider_correct_exposure(self) -> None:
        prm = _make_prm_with_global_stop()
        # size=1000, atr_stop=0.05 → exposure = 50
        provider = _make_provider("BTC/USDT", size=1000, atr_stop_distance=0.05)
        prm.register_symbol_provider("BTC/USDT", "dca", provider)
        assert prm._get_total_exposure("BTC/USDT") == Decimal("50")

    def test_multiple_providers_summed(self) -> None:
        prm = _make_prm_with_global_stop()
        # DCA: 3000 × 0.04 = 120
        dca = _make_provider("BTC/USDT", size=3000, atr_stop_distance=0.04)
        # Grid: 900 × 0.012 = 10.8
        grid = _make_provider("BTC/USDT", size=900, atr_stop_distance=0.012)
        # TF: 1000 × 0.03 = 30
        tf = _make_provider("BTC/USDT", size=1000, atr_stop_distance=0.03)
        prm.register_symbol_provider("BTC/USDT", "dca", dca)
        prm.register_symbol_provider("BTC/USDT", "grid", grid)
        prm.register_symbol_provider("BTC/USDT", "tf", tf)
        total = prm._get_total_exposure("BTC/USDT")
        assert total == Decimal("120") + Decimal("10.8") + Decimal("30")

    def test_different_symbol_not_counted(self) -> None:
        prm = _make_prm_with_global_stop()
        eth_provider = _make_provider("ETH/USDT", size=2000, atr_stop_distance=0.05)
        prm.register_symbol_provider("ETH/USDT", "eth-dca", eth_provider)
        assert prm._get_total_exposure("BTC/USDT") == Decimal("0")

    def test_provider_exception_skipped(self) -> None:
        prm = _make_prm_with_global_stop()
        failing = MagicMock(spec=StrategyPositionProvider)
        failing.get_open_positions.side_effect = RuntimeError("exchange error")
        prm.register_symbol_provider("BTC/USDT", "broken", failing)
        # Should not raise, just return 0
        assert prm._get_total_exposure("BTC/USDT") == Decimal("0")

    def test_duplicate_registration_ignored(self) -> None:
        prm = _make_prm_with_global_stop()
        p = _make_provider("BTC/USDT", size=1000, atr_stop_distance=0.05)
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        prm.register_symbol_provider("BTC/USDT", "dca", p)  # duplicate
        # Should only count once
        assert len(prm._symbol_providers["BTC/USDT"]) == 1

    def test_unregister_removes_provider(self) -> None:
        prm = _make_prm_with_global_stop()
        p = _make_provider("BTC/USDT", size=1000, atr_stop_distance=0.05)
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        prm.unregister_symbol_provider("BTC/USDT", "dca")
        assert prm._get_total_exposure("BTC/USDT") == Decimal("0")


# ---------------------------------------------------------------------------
# check_global_stop_loss
# ---------------------------------------------------------------------------


class TestCheckGlobalStopLoss:
    def test_no_config_always_false(self) -> None:
        prm = PortfolioRiskManager(total_capital=Decimal("10000"))
        assert prm.check_global_stop_loss("BTC/USDT") is False

    def test_below_threshold_false(self) -> None:
        prm = _make_prm_with_global_stop(total=10000, global_stop_pct=0.025)
        # exposure = 1000 × 0.02 = 20  →  20/10000 = 0.2% < 2.5%
        p = _make_provider("BTC/USDT", size=1000, atr_stop_distance=0.02)
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        assert prm.check_global_stop_loss("BTC/USDT") is False

    def test_at_threshold_triggers(self) -> None:
        prm = _make_prm_with_global_stop(total=10000, global_stop_pct=0.025)
        # exposure = 10000 × 0.025 = 250 → 250/10000 = 2.5%
        p = _make_provider("BTC/USDT", size=10000, atr_stop_distance=0.025)
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        assert prm.check_global_stop_loss("BTC/USDT") is True

    def test_above_threshold_triggers(self) -> None:
        prm = _make_prm_with_global_stop(total=10000, global_stop_pct=0.025)
        # exposure = 10000 × 0.05 = 500 → 5% > 2.5%
        p = _make_provider("BTC/USDT", size=10000, atr_stop_distance=0.05)
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        assert prm.check_global_stop_loss("BTC/USDT") is True

    def test_zero_total_capital_does_not_trigger(self) -> None:
        prm = PortfolioRiskManager(
            total_capital=Decimal("0"),
            global_stop_loss_pct=0.025,
        )
        p = _make_provider("BTC/USDT", size=1000, atr_stop_distance=0.05)
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        assert prm.check_global_stop_loss("BTC/USDT") is False


# ---------------------------------------------------------------------------
# force_close_all & cooldown
# ---------------------------------------------------------------------------


class TestForceCloseAll:
    def test_force_close_sets_cooldown(self) -> None:
        prm = _make_prm_with_global_stop(cooldown=1800)
        prm.force_close_all("BTC/USDT")
        assert prm._is_symbol_cooling_down("BTC/USDT") is True

    def test_callback_invoked(self) -> None:
        prm = _make_prm_with_global_stop()
        cb = MagicMock()
        prm.set_force_close_callback(cb)
        prm.force_close_all("BTC/USDT")
        cb.assert_called_once()
        symbol_arg, reason_arg = cb.call_args[0]
        assert symbol_arg == "BTC/USDT"
        assert "BTC/USDT" in reason_arg

    def test_check_allocation_blocked_during_cooldown(self) -> None:
        prm = _make_prm_with_global_stop(cooldown=1800)
        prm.force_close_all("BTC/USDT")
        result = prm.check_allocation("bot1", Decimal("100"), symbol="BTC/USDT")
        assert result.approved is False
        assert result.status == RiskCheckStatus.REJECTED_SYMBOL_COOLDOWN

    def test_cooldown_expires(self) -> None:
        import time

        prm = _make_prm_with_global_stop(cooldown=0)  # 0s cooldown = instant expiry
        prm.force_close_all("BTC/USDT")
        time.sleep(0.01)  # ensure monotonic clock advances
        # After 0-second cooldown the state should clear
        assert prm._is_symbol_cooling_down("BTC/USDT") is False

    def test_no_cooldown_after_tick_global_without_trigger(self) -> None:
        prm = _make_prm_with_global_stop(total=10000, global_stop_pct=0.025)
        # exposure well below threshold
        p = _make_provider("BTC/USDT", size=100, atr_stop_distance=0.01)
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        triggered = prm.tick_global_stop_loss(["BTC/USDT"])
        assert triggered == []
        assert prm._is_symbol_cooling_down("BTC/USDT") is False

    def test_cooldown_symbol_skipped_in_tick(self) -> None:
        prm = _make_prm_with_global_stop(cooldown=1800)
        prm.force_close_all("BTC/USDT")  # symbol now cooling down
        p = _make_provider("BTC/USDT", size=100000, atr_stop_distance=1.0)  # huge exposure
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        # tick_global_stop_loss should skip because cooldown is active
        triggered = prm.tick_global_stop_loss(["BTC/USDT"])
        assert triggered == []


# ---------------------------------------------------------------------------
# tick_global_stop_loss
# ---------------------------------------------------------------------------


class TestTickGlobalStopLoss:
    def test_disabled_returns_empty(self) -> None:
        prm = PortfolioRiskManager(total_capital=Decimal("10000"))
        triggered = prm.tick_global_stop_loss(["BTC/USDT"])
        assert triggered == []

    def test_triggers_for_exceeded_symbol(self) -> None:
        prm = _make_prm_with_global_stop(total=10000, global_stop_pct=0.025)
        p = _make_provider("BTC/USDT", size=10000, atr_stop_distance=0.05)
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        triggered = prm.tick_global_stop_loss()  # no args = all registered
        assert "BTC/USDT" in triggered
        assert prm._is_symbol_cooling_down("BTC/USDT") is True

    def test_multiple_strategies_aggregate_exposure(self) -> None:
        """DCA ($3000) + Grid ($900) + TF ($1000) — verify aggregation triggers stop."""
        prm = _make_prm_with_global_stop(total=10000, global_stop_pct=0.025)
        # DCA: 3000 × 0.04 = 120 → 1.2%
        dca = _make_provider("BTC/USDT", size=3000, atr_stop_distance=0.04)
        # Grid: 900 × 0.012 = 10.8 → 0.108%
        grid = _make_provider("BTC/USDT", size=900, atr_stop_distance=0.012)
        # TF: 1000 × 0.03 = 30 → 0.3%
        tf = _make_provider("BTC/USDT", size=1000, atr_stop_distance=0.03)
        prm.register_symbol_provider("BTC/USDT", "dca", dca)
        prm.register_symbol_provider("BTC/USDT", "grid", grid)
        prm.register_symbol_provider("BTC/USDT", "tf", tf)
        # Total: 160.8 → 1.608% < 2.5% → NOT triggered
        triggered = prm.tick_global_stop_loss()
        assert "BTC/USDT" not in triggered

    def test_high_combined_exposure_triggers_stop(self) -> None:
        """Three strategies with high individual stops exceed the aggregate limit."""
        prm = _make_prm_with_global_stop(total=10000, global_stop_pct=0.025)
        # DCA: 4000 × 0.05 = 200 → 2%
        dca = _make_provider("BTC/USDT", size=4000, atr_stop_distance=0.05)
        # SMC: 2000 × 0.05 = 100 → 1%
        smc = _make_provider("BTC/USDT", size=2000, atr_stop_distance=0.05)
        prm.register_symbol_provider("BTC/USDT", "dca", dca)
        prm.register_symbol_provider("BTC/USDT", "smc", smc)
        # Total: 300 → 3% > 2.5% → triggered
        triggered = prm.tick_global_stop_loss()
        assert "BTC/USDT" in triggered

    def test_no_cross_symbol_interference(self) -> None:
        prm = _make_prm_with_global_stop(total=10000, global_stop_pct=0.025)
        # BTC breaches; ETH should not be affected
        btc_p = _make_provider("BTC/USDT", size=4000, atr_stop_distance=0.1)
        eth_p = _make_provider("ETH/USDT", size=500, atr_stop_distance=0.01)
        prm.register_symbol_provider("BTC/USDT", "btc-dca", btc_p)
        prm.register_symbol_provider("ETH/USDT", "eth-dca", eth_p)
        triggered = prm.tick_global_stop_loss()
        assert "BTC/USDT" in triggered
        assert "ETH/USDT" not in triggered


# ---------------------------------------------------------------------------
# get_risk_report
# ---------------------------------------------------------------------------


class TestGetRiskReport:
    def test_report_structure(self) -> None:
        prm = _make_prm_with_global_stop(total=10000, global_stop_pct=0.025, cooldown=1800)
        p = _make_provider("BTC/USDT", size=1000, atr_stop_distance=0.05)
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        report = prm.get_risk_report()
        assert "global_stop_loss_pct" in report
        assert report["global_stop_loss_pct"] == 0.025
        assert "force_close_cooldown_seconds" in report
        assert "symbols" in report
        btc = report["symbols"]["BTC/USDT"]
        assert "aggregate_exposure" in btc
        assert "exposure_pct" in btc
        assert "is_cooling_down" in btc
        assert "strategies" in btc
        assert btc["strategies"] == ["dca"]

    def test_report_exposure_values_correct(self) -> None:
        prm = _make_prm_with_global_stop(total=10000)
        p = _make_provider("BTC/USDT", size=2000, atr_stop_distance=0.05)
        prm.register_symbol_provider("BTC/USDT", "dca", p)
        report = prm.get_risk_report()
        btc = report["symbols"]["BTC/USDT"]
        # exposure = 2000 × 0.05 = 100
        assert btc["aggregate_exposure"] == pytest.approx(100.0)
        # exposure_pct = 100/10000 × 100 = 1.0%
        assert btc["exposure_pct"] == pytest.approx(1.0)

    def test_no_global_stop_loss_disabled(self) -> None:
        prm = PortfolioRiskManager(total_capital=Decimal("10000"))
        report = prm.get_risk_report()
        assert report["global_stop_loss_pct"] is None
