"""
SMC Strategy Adapter - Wraps SMCStrategy to conform to BaseStrategy interface.

Translates between SMC-specific types (SMCSignal, SignalDirection) and
unified types (BaseSignal, SignalDirection) without modifying internal SMC code.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import pandas as pd

from bot.strategies.base import (
    BaseMarketAnalysis,
    BaseSignal,
    BaseStrategy,
    ExitReason,
    PositionInfo,
    SignalDirection,
    StrategyDirective,
    StrategyPerformance,
)
from bot.strategies.smc.config import SMCConfig
from bot.strategies.smc.entry_signals import (
    SignalDirection as SMCSignalDirection,
)
from bot.strategies.smc.entry_signals import (
    SMCSignal,
)
from bot.strategies.smc.smc_strategy import SMCStrategy
from bot.utils.logger import get_logger

logger = get_logger(__name__)


def _smc_direction_to_base(direction: SMCSignalDirection) -> SignalDirection:
    """Convert SMC SignalDirection to unified SignalDirection."""
    if direction == SMCSignalDirection.LONG:
        return SignalDirection.LONG
    return SignalDirection.SHORT


class SMCStrategyAdapter(BaseStrategy):
    """
    Adapter that wraps SMCStrategy to conform to BaseStrategy interface.

    Provides unified signal generation and position management on top of
    the existing SMC multi-timeframe analysis engine.
    """

    def __init__(
        self,
        config: Optional[SMCConfig] = None,
        account_balance: Decimal = Decimal("10000"),
        name: str = "smc-default",
    ):
        self._strategy = SMCStrategy(
            config=config,
            account_balance=account_balance,
        )
        self._name = name
        self._config = config or SMCConfig()

        # Track positions locally for unified interface
        self._positions: dict[str, dict[str, Any]] = {}
        self._closed_trades: list[dict[str, Any]] = []
        self._last_analysis: BaseMarketAnalysis | None = None

        # Cache dataframes for multi-timeframe
        self._cached_dfs: dict[str, pd.DataFrame] = {}
        # Directive received from StrategyConductor (None = no conductor active)
        self._directive: StrategyDirective | None = None

    def get_strategy_name(self) -> str:
        return self._name

    def get_strategy_type(self) -> str:
        return "smc"

    def analyze_market(self, *dfs: pd.DataFrame) -> BaseMarketAnalysis:
        """
        Analyze market using SMC multi-timeframe analysis.

        Expects 1-5 DataFrames in order: [df_d1, df_h4, df_h1, df_m15, df_m5].
        If fewer are provided, the last one is reused for missing timeframes.
        """
        # Pad DataFrames if fewer than 5 provided
        df_list = list(dfs)
        m5_explicitly_provided = len(df_list) >= 5
        while len(df_list) < 5:
            df_list.append(df_list[-1])

        df_d1, df_h4, df_h1, df_m15, df_m5 = (
            df_list[0],
            df_list[1],
            df_list[2],
            df_list[3],
            df_list[4],
        )

        # Cache for signal generation.
        # Only store real M5 data when it was explicitly passed (5th argument).
        # Padded copies must NOT trigger the M5 entry path in generate_signal().
        self._cached_dfs = {
            "d1": df_d1,
            "h4": df_h4,
            "h1": df_h1,
            "m15": df_m15,
            "m5": df_m5 if m5_explicitly_provided else pd.DataFrame(),
        }

        # SMCStrategy.analyze_market expects 4 DFs (d1, h4, h1, m15)
        analysis = self._strategy.analyze_market(df_d1, df_h4, df_h1, df_m15)

        trend = analysis.get("current_trend", "unknown")
        trend_str = "unknown"
        if isinstance(trend, str):
            trend_str = trend.lower()
        elif hasattr(trend, "value"):
            trend_str = trend.value.lower()

        self._last_analysis = BaseMarketAnalysis(
            trend=trend_str,
            trend_strength=float(analysis.get("trend_strength", 0.0)),
            volatility=float(analysis.get("volatility", 0.0)),
            timestamp=datetime.now(timezone.utc),
            strategy_type="smc",
            details=analysis,
        )

        return self._last_analysis

    def generate_signal(self, df: pd.DataFrame, current_balance: Decimal) -> Optional[BaseSignal]:
        """
        Generate signal using SMC strategy.

        Uses cached multi-timeframe data from analyze_market() for H1/H4/D1.
        Always uses the provided `df` as fresh M5 data (not the stale cache),
        so signals reflect the current bar's M5 context.

        When M5 data is available, uses the two-level H1→M5 entry logic
        (generate_signals_m5).  Falls back to the legacy H1/M15 approach
        when M5 data is absent.
        """
        df_h1 = self._cached_dfs.get("h1", df)
        # Use M5 path only when M5 data was explicitly cached via analyze_market().
        # The passed df may be M15 or another timeframe — treat it as fresh context
        # for signal generation but only route to generate_signals_m5 when the
        # "m5" key exists in the cache.
        cached_m5 = self._cached_dfs.get("m5", pd.DataFrame())
        # Always use the passed df as the most recent bar data for the M5 path.
        df_m5 = df if not cached_m5.empty else pd.DataFrame()

        # Two-level M5 entry: H1 context + M5 precision
        if not df_m5.empty and hasattr(self._strategy, "generate_signals_m5"):
            signals = self._strategy.generate_signals_m5(df_h1, df_m5)
        else:
            df_m15 = self._cached_dfs.get("m15", df)
            signals = self._strategy.generate_signals(df_h1, df_m15)

        if not signals:
            return None

        # Take the highest confidence signal
        best_signal: SMCSignal = max(signals, key=lambda s: s.confidence)

        return BaseSignal(
            direction=_smc_direction_to_base(best_signal.direction),
            entry_price=best_signal.entry_price,
            stop_loss=best_signal.stop_loss,
            take_profit=best_signal.take_profit,
            confidence=best_signal.confidence,
            timestamp=datetime.now(timezone.utc),
            strategy_type="smc",
            signal_reason=f"{best_signal.pattern.pattern_type.value}",
            risk_reward_ratio=best_signal.risk_reward_ratio,
            metadata={
                "confluence_score": best_signal.confluence_score,
                "confluence_zones": best_signal.confluence_zones,
                "trend_aligned": best_signal.trend_aligned,
                "trend_direction": best_signal.trend_direction.value,
                "pattern_quality": best_signal.pattern.quality_score,
                "liquidity_tp": any("LIQ_" in z for z in best_signal.confluence_zones),
            },
        )

    def open_position(self, signal: BaseSignal, position_size: Decimal) -> str:
        """Open a position tracked by the adapter."""
        position_id = str(uuid.uuid4())[:8]

        self._positions[position_id] = {
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "size": position_size,
            "entry_time": datetime.now(timezone.utc),
            "current_price": signal.entry_price,
            "signal": signal,
        }

        logger.info(
            "smc_position_opened",
            position_id=position_id,
            direction=signal.direction.value,
            entry_price=str(signal.entry_price),
            size=str(position_size),
        )

        return position_id

    def update_positions(
        self, current_price: Decimal, df: pd.DataFrame
    ) -> list[tuple[str, ExitReason]]:
        """Update all positions and check TP/SL."""
        exits: list[tuple[str, ExitReason]] = []

        for pos_id, pos in list(self._positions.items()):
            pos["current_price"] = current_price

            # Check stop loss
            if pos["direction"] == SignalDirection.LONG:
                if current_price <= pos["stop_loss"]:
                    exits.append((pos_id, ExitReason.STOP_LOSS))
                    continue
                if current_price >= pos["take_profit"]:
                    exits.append((pos_id, ExitReason.TAKE_PROFIT))
                    continue
            else:  # SHORT
                if current_price >= pos["stop_loss"]:
                    exits.append((pos_id, ExitReason.STOP_LOSS))
                    continue
                if current_price <= pos["take_profit"]:
                    exits.append((pos_id, ExitReason.TAKE_PROFIT))
                    continue

        return exits

    def force_close_all(self) -> list[tuple[str, ExitReason]]:
        """Force-close all open positions (called when router deactivates this strategy)."""
        return [(pos_id, ExitReason.MANUAL) for pos_id in list(self._positions.keys())]

    def close_position(
        self, position_id: str, exit_reason: ExitReason, exit_price: Decimal
    ) -> None:
        """Close a position and record the trade."""
        pos = self._positions.pop(position_id, None)
        if not pos:
            logger.warning("smc_position_not_found", position_id=position_id)
            return

        # Calculate PnL
        if pos["direction"] == SignalDirection.LONG:
            pnl = (exit_price - pos["entry_price"]) * pos["size"] / pos["entry_price"]
        else:
            pnl = (pos["entry_price"] - exit_price) * pos["size"] / pos["entry_price"]

        self._closed_trades.append(
            {
                "position_id": position_id,
                "direction": pos["direction"].value,
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "size": pos["size"],
                "pnl": pnl,
                "exit_reason": exit_reason.value,
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(timezone.utc),
            }
        )

        logger.info(
            "smc_position_closed",
            position_id=position_id,
            exit_reason=exit_reason.value,
            pnl=str(pnl),
        )

    def get_active_positions(self) -> list[PositionInfo]:
        """Get all active positions."""
        result = []
        for pos_id, pos in self._positions.items():
            entry_price = pos["entry_price"]
            current = pos["current_price"]
            if pos["direction"] == SignalDirection.LONG:
                pnl = (current - entry_price) * pos["size"] / entry_price
            else:
                pnl = (entry_price - current) * pos["size"] / entry_price

            result.append(
                PositionInfo(
                    position_id=pos_id,
                    direction=pos["direction"],
                    entry_price=entry_price,
                    current_price=current,
                    size=pos["size"],
                    stop_loss=pos["stop_loss"],
                    take_profit=pos["take_profit"],
                    unrealized_pnl=pnl,
                    entry_time=pos["entry_time"],
                    strategy_type="smc",
                )
            )
        return result

    def get_performance(self) -> StrategyPerformance:
        """Get performance based on closed trades."""
        total = len(self._closed_trades)
        if total == 0:
            return StrategyPerformance()

        winners = [t for t in self._closed_trades if t["pnl"] > 0]
        losers = [t for t in self._closed_trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in self._closed_trades)
        gross_profit = sum(t["pnl"] for t in winners) if winners else Decimal("0")
        gross_loss = abs(sum(t["pnl"] for t in losers)) if losers else Decimal("0")

        return StrategyPerformance(
            total_trades=total,
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=len(winners) / total if total > 0 else 0.0,
            total_pnl=total_pnl,
            profit_factor=float(gross_profit / gross_loss) if gross_loss > 0 else 0.0,
            avg_trade_pnl=total_pnl / total if total > 0 else Decimal("0"),
        )

    def set_directive(self, directive: StrategyDirective) -> None:
        """Store conductor directive; SMC uses key_levels for SL/TP alignment."""
        self._directive = directive
        logger.debug(
            "smc_directive_applied",
            mode=directive.mode.value,
            price_range=directive.price_range,
            key_levels_count=len(directive.key_levels),
            capital_allocation=directive.capital_allocation,
        )

    def reset(self) -> None:
        """Reset adapter and underlying strategy."""
        self._strategy.reset()
        self._positions.clear()
        self._closed_trades.clear()
        self._cached_dfs.clear()
        self._last_analysis = None
        self._directive = None
