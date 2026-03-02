"""Unified TradingCore — shared kernel for live bot and backtesting."""

from bot.core.trading_core.config import TradingCoreConfig
from bot.core.trading_core.core import TradingCore
from bot.core.trading_core.execution_layer import (
    BacktestExecutionLayer,
    ExecutionLayer,
    LiveExecutionLayer,
)
from bot.core.trading_core.hybrid_coordinator import CoordinatedDecision, HybridCoordinator
from bot.core.trading_core.time_provider import (
    BacktestTimeProvider,
    LiveTimeProvider,
    TimeProvider,
)

__all__ = [
    "TradingCoreConfig",
    "TradingCore",
    "HybridCoordinator",
    "CoordinatedDecision",
    "TimeProvider",
    "LiveTimeProvider",
    "BacktestTimeProvider",
    "ExecutionLayer",
    "LiveExecutionLayer",
    "BacktestExecutionLayer",
]
