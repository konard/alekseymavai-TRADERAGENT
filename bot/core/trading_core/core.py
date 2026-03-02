"""
TradingCore — assembles shared components for live bot and backtesting.

Both BotOrchestrator and BacktestOrchestratorEngine create a TradingCore
instance from the same TradingCoreConfig so that strategy parameters,
cooldowns, and risk thresholds are guaranteed to be identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bot.core.trading_core.config import TradingCoreConfig
from bot.core.trading_core.hybrid_coordinator import HybridCoordinator


@dataclass
class TradingCore:
    """
    Assembled trading kernel: config + shared strategy components.

    Usage (bot)::

        core = TradingCore.from_config(TradingCoreConfig(symbol="BTC/USDT"))
        decision = core.hybrid_coordinator.evaluate(adx=28.5)

    Usage (backtest)::

        core = TradingCore.from_config(config)
        cooldown_bars = core.config.cooldown_bars(bar_duration_seconds=300)
        router = StrategyRouter(cooldown_bars=cooldown_bars, ...)
    """

    config: TradingCoreConfig
    hybrid_coordinator: HybridCoordinator

    @classmethod
    def from_config(cls, config: TradingCoreConfig) -> "TradingCore":
        """
        Build a TradingCore from a TradingCoreConfig.

        Creates all shared components with parameters derived from *config*
        so that every consumer (bot, backtest, test) gets a consistent kernel.
        """
        hybrid_coordinator = HybridCoordinator(
            adx_dca_threshold=25.0,  # TODO: promote to TradingCoreConfig if needed
        )
        return cls(config=config, hybrid_coordinator=hybrid_coordinator)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def cooldown_bars(self, bar_duration_seconds: int = 300) -> int:
        """Convert config.cooldown_seconds to bars for a given bar duration."""
        return self.config.cooldown_bars(bar_duration_seconds)

    def regime_check_bars(self, bar_duration_seconds: int = 300) -> int:
        """Convert config.regime_check_interval_seconds to bars."""
        return self.config.regime_check_bars(bar_duration_seconds)
