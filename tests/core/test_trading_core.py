"""Tests for TradingCoreConfig, HybridCoordinator, and TradingCore (Phase 1)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bot.core.trading_core import (
    CoordinatedDecision,
    HybridCoordinator,
    TradingCore,
    TradingCoreConfig,
)
from bot.strategies.hybrid.hybrid_config import HybridMode


# ---------------------------------------------------------------------------
# TradingCoreConfig
# ---------------------------------------------------------------------------


class TestTradingCoreConfig:
    """Verify TradingCoreConfig defaults and helper methods."""

    def test_defaults(self) -> None:
        cfg = TradingCoreConfig()
        assert cfg.symbol == "BTC/USDT"
        assert cfg.initial_balance == Decimal("10000")
        assert cfg.cooldown_seconds == 600
        assert cfg.regime_check_interval_seconds == 3600
        assert cfg.max_daily_loss_pct == 0.05
        assert cfg.max_position_size_pct == 0.25
        assert cfg.enable_grid is True
        assert cfg.enable_dca is True
        assert cfg.enable_trend_follower is True
        assert cfg.enable_smc is False

    def test_cooldown_bars_m5(self) -> None:
        """600 s / 300 s-per-bar = 2 bars for M5."""
        cfg = TradingCoreConfig(cooldown_seconds=600)
        assert cfg.cooldown_bars(bar_duration_seconds=300) == 2

    def test_cooldown_bars_m1(self) -> None:
        """600 s / 60 s-per-bar = 10 bars for M1."""
        cfg = TradingCoreConfig(cooldown_seconds=600)
        assert cfg.cooldown_bars(bar_duration_seconds=60) == 10

    def test_cooldown_bars_minimum_one(self) -> None:
        """Even with cooldown_seconds < bar_duration_seconds, result >= 1."""
        cfg = TradingCoreConfig(cooldown_seconds=30)
        assert cfg.cooldown_bars(bar_duration_seconds=300) == 1

    def test_regime_check_bars_h1_on_m5(self) -> None:
        """3600 s / 300 s = 12 M5 bars per regime check."""
        cfg = TradingCoreConfig(regime_check_interval_seconds=3600)
        assert cfg.regime_check_bars(bar_duration_seconds=300) == 12

    def test_max_daily_loss_absolute(self) -> None:
        cfg = TradingCoreConfig(initial_balance=Decimal("10000"), max_daily_loss_pct=0.05)
        assert cfg.max_daily_loss_absolute() == Decimal("500")

    def test_max_position_size_absolute(self) -> None:
        cfg = TradingCoreConfig(initial_balance=Decimal("10000"), max_position_size_pct=0.25)
        assert cfg.max_position_size_absolute() == Decimal("2500")

    def test_fees_match_bybit_vip0(self) -> None:
        cfg = TradingCoreConfig()
        assert cfg.maker_fee == Decimal("0.0002")
        assert cfg.taker_fee == Decimal("0.00055")
        assert cfg.slippage == Decimal("0.0003")


# ---------------------------------------------------------------------------
# HybridCoordinator
# ---------------------------------------------------------------------------


class TestHybridCoordinator:
    """Verify HybridCoordinator.evaluate() decision logic."""

    def test_none_adx_returns_grid_only(self) -> None:
        coord = HybridCoordinator()
        decision = coord.evaluate(adx=None)
        assert isinstance(decision, CoordinatedDecision)
        assert decision.run_grid is True
        assert decision.run_dca is False
        assert decision.reason == "no_adx_data"

    def test_high_adx_returns_dca_active(self) -> None:
        coord = HybridCoordinator(adx_dca_threshold=25.0)
        decision = coord.evaluate(adx=30.0)
        assert decision.run_grid is False
        assert decision.run_dca is True
        assert decision.mode == HybridMode.DCA_ACTIVE

    def test_low_adx_returns_grid_only(self) -> None:
        coord = HybridCoordinator(adx_dca_threshold=25.0)
        decision = coord.evaluate(adx=20.0)
        assert decision.run_grid is True
        assert decision.run_dca is False
        assert decision.mode == HybridMode.GRID_ONLY

    def test_adx_at_threshold_returns_grid_only(self) -> None:
        """Exactly at threshold → grid (> required for DCA)."""
        coord = HybridCoordinator(adx_dca_threshold=25.0)
        decision = coord.evaluate(adx=25.0)
        assert decision.run_grid is True
        assert decision.run_dca is False

    def test_allow_both_in_tolerance_band(self) -> None:
        coord = HybridCoordinator(
            adx_dca_threshold=25.0, allow_both=True, adx_tolerance=3.0
        )
        # adx=26 is in [22, 28] band
        decision = coord.evaluate(adx=26.0)
        assert decision.run_grid is True
        assert decision.run_dca is True

    def test_allow_both_outside_band_follows_normal_rules(self) -> None:
        coord = HybridCoordinator(
            adx_dca_threshold=25.0, allow_both=True, adx_tolerance=3.0
        )
        # adx=35 is outside band → DCA only
        decision = coord.evaluate(adx=35.0)
        assert decision.run_grid is False
        assert decision.run_dca is True

    def test_reason_string_present(self) -> None:
        coord = HybridCoordinator(adx_dca_threshold=25.0)
        decision = coord.evaluate(adx=28.0)
        assert "adx=28.0" in decision.reason
        assert "threshold=25.0" in decision.reason

    def test_custom_threshold(self) -> None:
        coord = HybridCoordinator(adx_dca_threshold=35.0)
        assert coord.evaluate(adx=34.0).run_grid is True
        assert coord.evaluate(adx=36.0).run_dca is True


# ---------------------------------------------------------------------------
# TradingCore
# ---------------------------------------------------------------------------


class TestTradingCore:
    """Verify TradingCore factory and convenience methods."""

    def test_from_config_creates_instance(self) -> None:
        cfg = TradingCoreConfig()
        core = TradingCore.from_config(cfg)
        assert core.config is cfg
        assert isinstance(core.hybrid_coordinator, HybridCoordinator)

    def test_cooldown_bars_delegates_to_config(self) -> None:
        cfg = TradingCoreConfig(cooldown_seconds=600)
        core = TradingCore.from_config(cfg)
        assert core.cooldown_bars(bar_duration_seconds=300) == 2

    def test_regime_check_bars_delegates_to_config(self) -> None:
        cfg = TradingCoreConfig(regime_check_interval_seconds=3600)
        core = TradingCore.from_config(cfg)
        assert core.regime_check_bars(bar_duration_seconds=300) == 12

    def test_hybrid_coordinator_evaluates_correctly(self) -> None:
        core = TradingCore.from_config(TradingCoreConfig())
        decision = core.hybrid_coordinator.evaluate(adx=30.0)
        assert decision.run_dca is True

    def test_independent_instances_share_no_state(self) -> None:
        core1 = TradingCore.from_config(TradingCoreConfig(symbol="BTC/USDT"))
        core2 = TradingCore.from_config(TradingCoreConfig(symbol="ETH/USDT"))
        assert core1.config.symbol != core2.config.symbol
        assert core1.hybrid_coordinator is not core2.hybrid_coordinator
