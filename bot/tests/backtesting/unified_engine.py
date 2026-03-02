"""
UnifiedBacktestEngine — guaranteed-parity adapter between TradingCore and BacktestOrchestratorEngine.

This module bridges the TradingCore unified kernel (Phase 1) with the
BacktestOrchestratorEngine (V2.0) so that a single TradingCoreConfig drives
both the live bot and the backtest with identical parameters.

Without UnifiedBacktestEngine, engineers must manually keep OrchestratorBacktestConfig
in sync with the live bot settings — a fragile process that caused the known
mismatches (cooldown 60 bars vs 2 bars, daily_loss 25% vs 5%, fees 0.1% vs 0.02%).

Usage::

    core = TradingCore.from_config(TradingCoreConfig(symbol="BTC/USDT"))
    engine = UnifiedBacktestEngine()
    engine.register_strategy_factory("grid", GridStrategyFactory())
    engine.register_strategy_factory("dca", DCAStrategyFactory())

    result = await engine.run(data=mtf_data, core=core)
    print(result.total_return_pct)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from bot.core.trading_core import TradingCore, TradingCoreConfig
from bot.tests.backtesting.multi_tf_data_loader import MultiTimeframeData
from bot.tests.backtesting.orchestrator_engine import (
    BacktestOrchestratorEngine,
    OrchestratorBacktestConfig,
    OrchestratorBacktestResult,
)

logger = logging.getLogger(__name__)

# Seconds per M5 bar
_M5_BAR_SECONDS = 300


def trading_core_to_backtest_config(
    core: TradingCore,
    *,
    symbol: str | None = None,
    lookback: int = 100,
    warmup_bars: int = 14400,
    bar_duration_seconds: int = _M5_BAR_SECONDS,
    enable_strategy_router: bool = True,
    regime_check_every_n: int | None = None,
    grid_params: dict[str, Any] | None = None,
    dca_params: dict[str, Any] | None = None,
    tf_params: dict[str, Any] | None = None,
    smc_params: dict[str, Any] | None = None,
) -> OrchestratorBacktestConfig:
    """
    Convert a TradingCore into an OrchestratorBacktestConfig with correct parity.

    This is the canonical translation function.  All parameter derivations are
    done here and documented with their source:

    cooldown_bars
        ``core.config.cooldown_bars(bar_duration_seconds)``
        → for M5 data (300 s/bar): 600 s / 300 = **2 bars** (not 60!)

    max_daily_loss_pct
        ``core.config.max_daily_loss_pct`` = **0.05** (5 %, not 25 %)

    max_position_size_pct / max_position_pct
        From ``core.config.max_position_size_pct`` (0.25)

    maker_fee / taker_fee
        From ``core.config.maker_fee`` / ``core.config.taker_fee`` (Bybit VIP0)

    regime_check_every_n
        Derived from ``core.config.regime_check_interval_seconds`` (3600 s / 300 = 12 bars)
        unless overridden explicitly.

    Args:
        core:               TradingCore instance (wraps TradingCoreConfig).
        symbol:             Override symbol (default: core.config.symbol).
        lookback:           OHLCV lookback window for strategy analysis.
        warmup_bars:        Bars to skip before strategy execution starts.
        bar_duration_seconds: Bar length in seconds. Default 300 = M5.
        enable_strategy_router: Whether to use StrategyRouter for regime routing.
        regime_check_every_n:   Override regime check interval in bars.
        grid_params:        Per-strategy parameter overrides.
        dca_params:         Per-strategy parameter overrides.
        tf_params:          Per-strategy parameter overrides.
        smc_params:         Per-strategy parameter overrides.

    Returns:
        OrchestratorBacktestConfig with all parameters derived from TradingCore.
    """
    cfg = core.config
    cooldown_bars = core.cooldown_bars(bar_duration_seconds)
    regime_bars = (
        regime_check_every_n
        if regime_check_every_n is not None
        else core.regime_check_bars(bar_duration_seconds)
    )

    logger.debug(
        "trading_core_to_backtest_config",
        symbol=symbol or cfg.symbol,
        cooldown_seconds=cfg.cooldown_seconds,
        cooldown_bars=cooldown_bars,
        bar_duration_seconds=bar_duration_seconds,
        regime_check_every_n=regime_bars,
        max_daily_loss_pct=cfg.max_daily_loss_pct,
    )

    return OrchestratorBacktestConfig(
        symbol=symbol or cfg.symbol,
        initial_balance=cfg.initial_balance,
        lookback=lookback,
        warmup_bars=warmup_bars,
        analyze_every_n=cfg.analyze_every_n_bars,
        # Strategies
        enable_grid=cfg.enable_grid,
        enable_dca=cfg.enable_dca,
        enable_trend_follower=cfg.enable_trend_follower,
        enable_smc=cfg.enable_smc,
        # Regime routing
        enable_strategy_router=enable_strategy_router,
        router_cooldown_bars=cooldown_bars,
        regime_check_every_n=regime_bars,
        # Per-strategy params
        grid_params=grid_params or {},
        dca_params=dca_params or {},
        tf_params=tf_params or {},
        smc_params=smc_params or {},
        # Risk
        enable_risk_manager=True,
        max_position_size_pct=cfg.max_position_size_pct,
        max_daily_loss_pct=cfg.max_daily_loss_pct,
        max_position_pct=cfg.max_position_pct,
        # Position sizing
        risk_per_trade=cfg.risk_per_trade,
        # Exchange fees (Bybit VIP0 by default)
        maker_fee=cfg.maker_fee,
        taker_fee=cfg.taker_fee,
        slippage=cfg.slippage,
    )


class UnifiedBacktestEngine(BacktestOrchestratorEngine):
    """
    Guaranteed-parity backtest engine: accepts TradingCore as configuration.

    Subclasses BacktestOrchestratorEngine and adds the ``run_with_core()``
    entry point that automatically converts TradingCore → OrchestratorBacktestConfig
    via ``trading_core_to_backtest_config()``.

    Strategy factories are registered the same way as BacktestOrchestratorEngine::

        engine = UnifiedBacktestEngine()
        engine.register_strategy_factory("grid", my_grid_factory)
        engine.register_strategy_factory("dca", my_dca_factory)

        core = TradingCore.from_config(TradingCoreConfig(symbol="ETH/USDT"))
        result = await engine.run_with_core(data=mtf_data, core=core)

    You can still call the lower-level ``run(data, config)`` directly if you
    need fine-grained control over a specific ``OrchestratorBacktestConfig``.
    """

    async def run_with_core(
        self,
        data: MultiTimeframeData,
        core: TradingCore,
        *,
        symbol: str | None = None,
        lookback: int = 100,
        warmup_bars: int = 14400,
        bar_duration_seconds: int = _M5_BAR_SECONDS,
        enable_strategy_router: bool = True,
        regime_check_every_n: int | None = None,
        grid_params: dict[str, Any] | None = None,
        dca_params: dict[str, Any] | None = None,
        tf_params: dict[str, Any] | None = None,
        smc_params: dict[str, Any] | None = None,
    ) -> OrchestratorBacktestResult:
        """
        Run the backtest using a TradingCore for configuration.

        This is the preferred entry point when you want guaranteed parity with
        the live bot — all critical parameters (cooldown, fees, risk limits) are
        derived from the same TradingCoreConfig that the live bot uses.

        Args:
            data:                 Pre-loaded multi-timeframe OHLCV data.
            core:                 TradingCore instance (source of all config).
            symbol:               Override symbol (default: core.config.symbol).
            lookback:             OHLCV lookback window.
            warmup_bars:          Bars to skip before strategy execution.
            bar_duration_seconds: Bar length in seconds (300 = M5).
            enable_strategy_router: Enable regime-based routing.
            regime_check_every_n: Override regime check interval (bars).
            grid_params:          Grid strategy parameter overrides.
            dca_params:           DCA strategy parameter overrides.
            tf_params:            TrendFollower strategy parameter overrides.
            smc_params:           SMC strategy parameter overrides.

        Returns:
            OrchestratorBacktestResult with full metrics.
        """
        backtest_config = trading_core_to_backtest_config(
            core,
            symbol=symbol,
            lookback=lookback,
            warmup_bars=warmup_bars,
            bar_duration_seconds=bar_duration_seconds,
            enable_strategy_router=enable_strategy_router,
            regime_check_every_n=regime_check_every_n,
            grid_params=grid_params,
            dca_params=dca_params,
            tf_params=tf_params,
            smc_params=smc_params,
        )
        return await self.run(data=data, config=backtest_config)

    @classmethod
    def from_trading_core(
        cls,
        core: TradingCore,
        strategy_factories: dict[str, Any] | None = None,
    ) -> "UnifiedBacktestEngine":
        """
        Convenience factory: create engine pre-loaded with strategy factories.

        Args:
            core:               TradingCore instance (used to configure the run).
            strategy_factories: Dict mapping strategy name → factory callable.

        Returns:
            UnifiedBacktestEngine ready to call run_with_core().
        """
        engine = cls()
        for name, factory in (strategy_factories or {}).items():
            engine.register_strategy_factory(name, factory)
        return engine
