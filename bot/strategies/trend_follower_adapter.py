"""
Trend-Follower Strategy Adapter - Wraps TrendFollowerStrategy to conform to BaseStrategy.

Translates between TF-specific types (EntrySignal, SignalType) and
unified types (BaseSignal, SignalDirection) without modifying internal TF code.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import pandas as pd

from bot.strategies.base import (
    BaseMarketAnalysis,
    BaseSignal,
    BaseStrategy,
    PositionInfo,
    SignalDirection,
    StrategyPerformance,
)
from bot.strategies.base import (
    ExitReason as BaseExitReason,
)
from bot.strategies.trend_follower.config import TrendFollowerConfig
from bot.strategies.trend_follower.entry_logic import SignalType
from bot.strategies.trend_follower.position_manager import ExitReason as TFExitReason
from bot.strategies.trend_follower.trend_follower_strategy import TrendFollowerStrategy
from bot.utils.logger import get_logger

logger = get_logger(__name__)


def _tf_signal_to_direction(signal_type: SignalType) -> SignalDirection:
    """Convert TF SignalType to unified SignalDirection."""
    if signal_type == SignalType.LONG:
        return SignalDirection.LONG
    return SignalDirection.SHORT


def _map_exit_reason(reason: str) -> BaseExitReason:
    """Map TF exit reason string to unified ExitReason."""
    mapping = {
        "take_profit": BaseExitReason.TAKE_PROFIT,
        "stop_loss": BaseExitReason.STOP_LOSS,
        "trailing_stop": BaseExitReason.TRAILING_STOP,
        "breakeven": BaseExitReason.BREAKEVEN,
        "partial_close": BaseExitReason.PARTIAL_CLOSE,
        "manual": BaseExitReason.MANUAL,
    }
    return mapping.get(reason.lower(), BaseExitReason.MANUAL)


class TrendFollowerAdapter(BaseStrategy):
    """
    Adapter that wraps TrendFollowerStrategy to conform to BaseStrategy interface.

    Delegates all core logic to the underlying TrendFollowerStrategy while
    translating signals and positions to the unified format.
    """

    def __init__(
        self,
        config: Optional[TrendFollowerConfig] = None,
        initial_capital: Decimal = Decimal("10000"),
        name: str = "trend-follower-default",
        log_trades: bool = True,
    ):
        self._strategy = TrendFollowerStrategy(
            config=config,
            initial_capital=initial_capital,
            log_trades=log_trades,
        )
        self._name = name
        self._config = config or TrendFollowerConfig()
        self._last_analysis: BaseMarketAnalysis | None = None
        self._last_df: pd.DataFrame | None = None

        # Cache the most recent entry signal for open_position
        self._pending_signal: Any = None
        self._pending_metrics: Any = None

    def get_strategy_name(self) -> str:
        return self._name

    def get_strategy_type(self) -> str:
        return "trend_follower"

    def analyze_market(self, *dfs: pd.DataFrame) -> BaseMarketAnalysis:
        """
        Analyze market using Trend-Follower strategy.

        Uses the first DataFrame provided.
        """
        df = dfs[0] if dfs else pd.DataFrame()
        self._last_df = df

        conditions = self._strategy.analyze_market(df)

        trend_str = "unknown"
        trend_strength_val = 0.0
        volatility = 0.0
        details: dict[str, Any] = {}

        if conditions:
            phase = (
                conditions.phase.value
                if hasattr(conditions.phase, "value")
                else str(conditions.phase)
            )
            trend_str = phase.lower()

            strength = conditions.trend_strength
            if hasattr(strength, "value"):
                strength_map = {"weak": 0.3, "moderate": 0.6, "strong": 0.9}
                trend_strength_val = strength_map.get(strength.value.lower(), 0.5)
            else:
                trend_strength_val = float(strength) if strength else 0.0

            volatility = (
                float(conditions.atr) if hasattr(conditions, "atr") and conditions.atr else 0.0
            )

            details = {
                "phase": phase,
                "ema_fast": float(conditions.ema_fast) if hasattr(conditions, "ema_fast") else None,
                "ema_slow": float(conditions.ema_slow) if hasattr(conditions, "ema_slow") else None,
                "rsi": float(conditions.rsi) if hasattr(conditions, "rsi") else None,
                "ema_divergence": (
                    float(conditions.ema_divergence)
                    if hasattr(conditions, "ema_divergence")
                    else None
                ),
            }

        self._last_analysis = BaseMarketAnalysis(
            trend=trend_str,
            trend_strength=trend_strength_val,
            volatility=volatility,
            timestamp=datetime.now(timezone.utc),
            strategy_type="trend_follower",
            details=details,
        )

        return self._last_analysis

    def generate_signal(self, df: pd.DataFrame, current_balance: Decimal) -> Optional[BaseSignal]:
        """
        Generate entry signal using Trend-Follower strategy.
        """
        self._last_df = df
        entry_data = self._strategy.check_entry_signal(df, current_balance)

        if not entry_data:
            self._pending_signal = None
            self._pending_metrics = None
            return None

        signal, metrics, position_size = entry_data

        if signal.signal_type == SignalType.NONE:
            return None

        # Cache for open_position
        self._pending_signal = signal
        self._pending_metrics = metrics

        # Estimate TP/SL from the strategy's position manager logic
        # These are approximate — actual values are set when open_position is called
        entry_price = signal.entry_price
        conditions = signal.market_conditions
        atr = conditions.atr if hasattr(conditions, "atr") and conditions.atr else Decimal("0")

        if signal.signal_type == SignalType.LONG:
            estimated_sl = entry_price - atr
            estimated_tp = entry_price + atr * Decimal("2")
        else:
            estimated_sl = entry_price + atr
            estimated_tp = entry_price - atr * Decimal("2")

        return BaseSignal(
            direction=_tf_signal_to_direction(signal.signal_type),
            entry_price=entry_price,
            stop_loss=estimated_sl,
            take_profit=estimated_tp,
            confidence=float(signal.confidence),
            timestamp=datetime.now(timezone.utc),
            strategy_type="trend_follower",
            signal_reason=signal.entry_reason.value,
            risk_reward_ratio=(
                float(abs(estimated_tp - entry_price) / abs(entry_price - estimated_sl))
                if abs(entry_price - estimated_sl) > 0
                else 0.0
            ),
            metadata={
                "volume_confirmed": signal.volume_confirmed,
                "position_size": str(position_size),
                "market_phase": (
                    conditions.phase.value
                    if hasattr(conditions.phase, "value")
                    else str(conditions.phase)
                ),
            },
        )

    def open_position(self, signal: BaseSignal, position_size: Decimal) -> str:
        """
        Open position via underlying TrendFollowerStrategy.

        Uses the cached pending signal from generate_signal().
        """
        if self._pending_signal:
            position_id = self._strategy.open_position(self._pending_signal, position_size)
            self._pending_signal = None
            self._pending_metrics = None
            return position_id

        # Fallback: create a position ID if no cached signal
        logger.warning("tf_adapter_no_cached_signal")
        return f"tf-{id(signal)}"

    def update_positions(
        self, current_price: Decimal, df: pd.DataFrame
    ) -> list[tuple[str, BaseExitReason]]:
        """Update all active positions."""
        exits: list[tuple[str, BaseExitReason]] = []

        active_ids = list(self._strategy.position_manager.active_positions.keys())

        for position_id in active_ids:
            exit_reason = self._strategy.update_position(position_id, current_price, df)
            if exit_reason:
                reason_str = exit_reason if isinstance(exit_reason, str) else str(exit_reason)
                exits.append((position_id, _map_exit_reason(reason_str)))

        return exits

    def close_position(
        self, position_id: str, exit_reason: BaseExitReason, exit_price: Decimal
    ) -> None:
        """Close position via underlying strategy."""
        self._strategy.close_position(position_id, TFExitReason(exit_reason.value), exit_price)

    def get_active_positions(self) -> list[PositionInfo]:
        """Get active positions from underlying strategy."""
        result = []

        positions = self._strategy.position_manager.active_positions
        for pos_id, pos in positions.items():
            direction = SignalDirection.LONG
            if hasattr(pos, "signal_type"):
                if pos.signal_type == SignalType.SHORT:
                    direction = SignalDirection.SHORT

            entry_price = pos.entry_price if hasattr(pos, "entry_price") else Decimal("0")
            current = pos.current_price if hasattr(pos, "current_price") else entry_price
            pnl = pos.current_profit if hasattr(pos, "current_profit") else Decimal("0")

            result.append(
                PositionInfo(
                    position_id=pos_id,
                    direction=direction,
                    entry_price=entry_price,
                    current_price=current,
                    size=pos.size if hasattr(pos, "size") else Decimal("0"),
                    stop_loss=pos.levels.stop_loss if hasattr(pos, "levels") else Decimal("0"),
                    take_profit=pos.levels.take_profit if hasattr(pos, "levels") else Decimal("0"),
                    unrealized_pnl=pnl,
                    entry_time=(
                        pos.entry_time if hasattr(pos, "entry_time") else datetime.now(timezone.utc)
                    ),
                    strategy_type="trend_follower",
                )
            )

        return result

    def get_performance(self) -> StrategyPerformance:
        """Get performance from underlying strategy statistics."""
        stats = self._strategy.get_statistics()

        risk_metrics = stats.get("risk_metrics", {})
        trade_stats = stats.get("trade_statistics", {})

        return StrategyPerformance(
            total_trades=trade_stats.get("total_trades", 0),
            winning_trades=trade_stats.get("winning_trades", 0),
            losing_trades=trade_stats.get("losing_trades", 0),
            win_rate=trade_stats.get("win_rate", 0.0),
            total_pnl=Decimal(str(trade_stats.get("total_pnl", "0"))),
            profit_factor=trade_stats.get("profit_factor", 0.0),
            sharpe_ratio=risk_metrics.get("sharpe_ratio", 0.0),
            max_drawdown=risk_metrics.get("max_drawdown_pct", 0.0),
            avg_trade_pnl=Decimal(str(trade_stats.get("avg_pnl", "0"))),
            metadata={"full_stats": stats},
        )

    def reset(self) -> None:
        """Reset underlying strategy state."""
        self._pending_signal = None
        self._pending_metrics = None
        self._last_analysis = None
        self._last_df = None
