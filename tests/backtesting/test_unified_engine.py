"""Tests for UnifiedBacktestEngine and trading_core_to_backtest_config (Phase 4)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from bot.core.trading_core import TradingCore, TradingCoreConfig
from bot.tests.backtesting.orchestrator_engine import OrchestratorBacktestConfig
from bot.tests.backtesting.unified_engine import (
    UnifiedBacktestEngine,
    trading_core_to_backtest_config,
)


# ---------------------------------------------------------------------------
# trading_core_to_backtest_config
# ---------------------------------------------------------------------------


class TestTradingCoreToBacktestConfig:
    """Verify correct parameter translation from TradingCore to OrchestratorBacktestConfig."""

    def _make_core(self, **kwargs) -> TradingCore:
        return TradingCore.from_config(TradingCoreConfig(**kwargs))

    def test_returns_orchestrator_config(self) -> None:
        core = self._make_core()
        cfg = trading_core_to_backtest_config(core)
        assert isinstance(cfg, OrchestratorBacktestConfig)

    def test_symbol_from_core(self) -> None:
        core = self._make_core(symbol="ETH/USDT")
        cfg = trading_core_to_backtest_config(core)
        assert cfg.symbol == "ETH/USDT"

    def test_symbol_override(self) -> None:
        core = self._make_core(symbol="BTC/USDT")
        cfg = trading_core_to_backtest_config(core, symbol="SOL/USDT")
        assert cfg.symbol == "SOL/USDT"

    def test_initial_balance_from_core(self) -> None:
        core = self._make_core(initial_balance=Decimal("50000"))
        cfg = trading_core_to_backtest_config(core)
        assert cfg.initial_balance == Decimal("50000")

    def test_cooldown_bars_m5_correct(self) -> None:
        """KEY: 600 s / 300 s = 2 bars — not the old default of 60 bars."""
        core = self._make_core(cooldown_seconds=600)
        cfg = trading_core_to_backtest_config(core, bar_duration_seconds=300)
        assert cfg.router_cooldown_bars == 2

    def test_cooldown_bars_m1_correct(self) -> None:
        """600 s / 60 s = 10 bars for M1 data."""
        core = self._make_core(cooldown_seconds=600)
        cfg = trading_core_to_backtest_config(core, bar_duration_seconds=60)
        assert cfg.router_cooldown_bars == 10

    def test_max_daily_loss_pct_is_5_percent(self) -> None:
        """KEY: Must be 0.05, not the old 0.25 default."""
        core = self._make_core(max_daily_loss_pct=0.05)
        cfg = trading_core_to_backtest_config(core)
        assert cfg.max_daily_loss_pct == pytest.approx(0.05)

    def test_max_position_size_pct_from_core(self) -> None:
        core = self._make_core(max_position_size_pct=0.20)
        cfg = trading_core_to_backtest_config(core)
        assert cfg.max_position_size_pct == pytest.approx(0.20)

    def test_fees_from_core_bybit_vip0(self) -> None:
        """KEY: Bybit VIP0 fees, not the old 0.1% MarketSimulator default."""
        core = self._make_core()
        cfg = trading_core_to_backtest_config(core)
        assert cfg.maker_fee == Decimal("0.0002")
        assert cfg.taker_fee == Decimal("0.00055")
        assert cfg.slippage == Decimal("0.0003")

    def test_strategies_from_core(self) -> None:
        core = self._make_core(
            enable_grid=True, enable_dca=True, enable_trend_follower=False, enable_smc=False
        )
        cfg = trading_core_to_backtest_config(core)
        assert cfg.enable_grid is True
        assert cfg.enable_dca is True
        assert cfg.enable_trend_follower is False
        assert cfg.enable_smc is False

    def test_regime_check_derived_from_core(self) -> None:
        """3600 s / 300 s = 12 bars per regime check."""
        core = self._make_core(regime_check_interval_seconds=3600)
        cfg = trading_core_to_backtest_config(core, bar_duration_seconds=300)
        assert cfg.regime_check_every_n == 12

    def test_regime_check_override(self) -> None:
        core = self._make_core()
        cfg = trading_core_to_backtest_config(core, regime_check_every_n=6)
        assert cfg.regime_check_every_n == 6

    def test_per_strategy_params_passed(self) -> None:
        core = self._make_core()
        cfg = trading_core_to_backtest_config(
            core,
            grid_params={"spacing_pct": 0.02},
            dca_params={"trigger_pct": 0.05},
        )
        assert cfg.grid_params == {"spacing_pct": 0.02}
        assert cfg.dca_params == {"trigger_pct": 0.05}

    def test_default_params_are_empty_dicts(self) -> None:
        core = self._make_core()
        cfg = trading_core_to_backtest_config(core)
        assert cfg.grid_params == {}
        assert cfg.dca_params == {}
        assert cfg.tf_params == {}
        assert cfg.smc_params == {}

    def test_strategy_router_enabled_by_default(self) -> None:
        core = self._make_core()
        cfg = trading_core_to_backtest_config(core)
        assert cfg.enable_strategy_router is True

    def test_strategy_router_can_be_disabled(self) -> None:
        core = self._make_core()
        cfg = trading_core_to_backtest_config(core, enable_strategy_router=False)
        assert cfg.enable_strategy_router is False


# ---------------------------------------------------------------------------
# UnifiedBacktestEngine
# ---------------------------------------------------------------------------


class TestUnifiedBacktestEngine:
    """Verify UnifiedBacktestEngine extends BacktestOrchestratorEngine correctly."""

    def test_is_subclass(self) -> None:
        from bot.tests.backtesting.orchestrator_engine import BacktestOrchestratorEngine
        assert issubclass(UnifiedBacktestEngine, BacktestOrchestratorEngine)

    def test_can_instantiate(self) -> None:
        engine = UnifiedBacktestEngine()
        assert engine is not None

    def test_register_strategy_factory(self) -> None:
        engine = UnifiedBacktestEngine()
        factory = MagicMock()
        engine.register_strategy_factory("grid", factory)
        assert "grid" in engine._strategy_factories

    def test_from_trading_core_factory(self) -> None:
        core = TradingCore.from_config(TradingCoreConfig())
        factory = MagicMock()
        engine = UnifiedBacktestEngine.from_trading_core(
            core, strategy_factories={"grid": factory}
        )
        assert isinstance(engine, UnifiedBacktestEngine)
        assert "grid" in engine._strategy_factories

    def test_from_trading_core_no_factories(self) -> None:
        core = TradingCore.from_config(TradingCoreConfig())
        engine = UnifiedBacktestEngine.from_trading_core(core)
        assert isinstance(engine, UnifiedBacktestEngine)
        assert engine._strategy_factories == {}

    def test_parity_cooldown_vs_live_bot(self) -> None:
        """
        Cooldown derived from same TradingCoreConfig that the live bot would use.
        This is the key parity guarantee.
        """
        # Simulate live bot config
        live_config = TradingCoreConfig(cooldown_seconds=600)
        # Simulate backtest config via UnifiedBacktestEngine
        core = TradingCore.from_config(live_config)
        backtest_cfg = trading_core_to_backtest_config(core, bar_duration_seconds=300)
        # Verify cooldown matches
        assert backtest_cfg.router_cooldown_bars == live_config.cooldown_bars(300) == 2

    def test_parity_daily_loss_vs_live_bot(self) -> None:
        """max_daily_loss_pct must match between live bot and backtest."""
        live_config = TradingCoreConfig(max_daily_loss_pct=0.05)
        core = TradingCore.from_config(live_config)
        backtest_cfg = trading_core_to_backtest_config(core)
        assert backtest_cfg.max_daily_loss_pct == live_config.max_daily_loss_pct

    def test_parity_fees_vs_live_bot(self) -> None:
        """Fees in backtest must match real Bybit VIP0 fees from TradingCoreConfig."""
        live_config = TradingCoreConfig()
        core = TradingCore.from_config(live_config)
        backtest_cfg = trading_core_to_backtest_config(core)
        assert backtest_cfg.maker_fee == live_config.maker_fee
        assert backtest_cfg.taker_fee == live_config.taker_fee
