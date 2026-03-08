"""
Grid Strategy Adapter — Wraps GridEngine to conform to BaseStrategy interface.

Translates grid-level order logic into the unified signal/position lifecycle
for use with BacktestEngine and StrategyComparison.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import numpy as np
import pandas as pd

from bot.core.grid_engine import GridEngine
from bot.strategies.base import (
    BaseMarketAnalysis,
    BaseSignal,
    BaseStrategy,
    ExitReason,
    PositionInfo,
    SignalDirection,
    StrategyPerformance,
)
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class GridAdapter(BaseStrategy):
    """
    Adapter that wraps GridEngine to conform to BaseStrategy interface.

    For backtesting, simulates grid trading as a series of buy/sell positions:
    - analyze_market: computes price range and volatility
    - generate_signal: buys when price near a grid buy level
    - update_positions: sells when price hits grid sell level (TP)
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        num_levels: int = 6,
        amount_per_grid: Decimal = Decimal("150"),
        profit_per_grid: Decimal = Decimal("0.012"),
        grid_range_pct: Decimal = Decimal("0.05"),
        name: str = "grid-default",
        # Level 1 — Universal parameter (P1.1 unification)
        risk_per_trade_pct: Optional[Decimal] = None,
    ) -> None:

        self._symbol = symbol
        self._name = name
        self._num_levels = num_levels
        self._profit_per_grid = profit_per_grid
        self._grid_range_pct = grid_range_pct
        # risk_per_trade_pct is stored for reference / future position-sizing use.
        self._risk_per_trade_pct: Optional[Decimal] = (
            Decimal(str(risk_per_trade_pct)) if risk_per_trade_pct is not None else None
        )
        # amount_per_grid: accepted as explicit value (backward compat).
        # In the future it will be computed from balance × risk_per_trade_pct.
        self._amount_per_grid = amount_per_grid

        self._grid_engine: GridEngine | None = None
        self._grid_levels: list[Decimal] = []
        self._grid_lower_price: Decimal = Decimal("0")  # lower boundary of the entire grid
        self._last_analysis: BaseMarketAnalysis | None = None

        # Position tracking
        self._positions: dict[str, dict[str, Any]] = {}
        self._closed_trades: list[dict[str, Any]] = []
        self._current_price = Decimal("0")

    def get_strategy_name(self) -> str:
        return self._name

    def get_strategy_type(self) -> str:
        return "grid"

    def analyze_market(self, *dfs: pd.DataFrame) -> BaseMarketAnalysis:
        """Analyze market to set up grid bounds from recent price action."""
        df = dfs[-1] if dfs else pd.DataFrame()

        if df.empty or len(df) < 5:
            return BaseMarketAnalysis(
                trend="unknown",
                trend_strength=0.0,
                volatility=0.0,
                timestamp=datetime.now(timezone.utc),
                strategy_type="grid",
            )

        close = df["close"].values
        self._current_price = Decimal(str(close[-1]))

        # Calculate volatility (ATR-like)
        high = df["high"].values
        low = df["low"].values
        tr = high - low
        atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))
        volatility = atr / float(close[-1]) if close[-1] > 0 else 0.0

        # Determine grid bounds based on recent range
        current = self._current_price
        half_range = current * self._grid_range_pct
        upper = current + half_range
        lower = current - half_range

        # Initialize grid engine only once so levels stay fixed
        if self._grid_engine is None:
            self._grid_engine = GridEngine(
                symbol=self._symbol,
                upper_price=upper,
                lower_price=lower,
                grid_levels=self._num_levels,
                amount_per_grid=self._amount_per_grid,
                profit_per_grid=self._profit_per_grid,
            )
            self._grid_levels = self._grid_engine.calculate_grid_levels()
            self._grid_lower_price = self._grid_engine.lower_price

        # Trend: grid works best in sideways
        price_change = (close[-1] - close[0]) / close[0] if close[0] > 0 else 0.0
        if abs(price_change) < 0.02:
            trend = "sideways"
            trend_strength = 0.3
        elif price_change > 0:
            trend = "bullish"
            trend_strength = min(abs(price_change) * 10, 1.0)
        else:
            trend = "bearish"
            trend_strength = min(abs(price_change) * 10, 1.0)

        self._last_analysis = BaseMarketAnalysis(
            trend=trend,
            trend_strength=trend_strength,
            volatility=volatility,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
            details={
                "upper_price": float(upper),
                "lower_price": float(lower),
                "grid_levels": len(self._grid_levels),
                "atr": atr,
            },
        )
        return self._last_analysis

    def generate_signal(self, df: pd.DataFrame, current_balance: Decimal) -> Optional[BaseSignal]:
        """Generate buy signal when price is near a grid buy level."""
        if not self._grid_levels or df.empty:
            return None

        close = Decimal(str(df["close"].iloc[-1]))
        self._current_price = close

        # Find nearest buy level below current price
        buy_levels = [lvl for lvl in self._grid_levels if lvl < close]
        if not buy_levels:
            return None

        nearest_buy = max(buy_levels)
        distance = abs(close - nearest_buy) / close

        # Only signal if price is very close to a buy level (within 0.5%)
        if distance > Decimal("0.005"):
            return None

        # Check we don't already have a position at this level
        for pos in self._positions.values():
            if abs(pos["entry_price"] - nearest_buy) / nearest_buy < Decimal("0.003"):
                return None

        # Cost check
        if current_balance < self._amount_per_grid:
            return None

        sell_target = nearest_buy * (Decimal("1") + self._profit_per_grid)
        # SL = lower boundary of the entire grid (not per-position -5%).
        # Real grid trading: close ALL positions only when price exits the grid range.
        stop_loss = self._grid_lower_price if self._grid_lower_price > 0 else nearest_buy * (
            Decimal("1") - self._grid_range_pct
        )

        return BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=close,
            stop_loss=stop_loss,
            take_profit=sell_target,
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
            signal_reason="grid_buy_level",
            risk_reward_ratio=float(self._profit_per_grid / self._grid_range_pct),
            metadata={"grid_level": float(nearest_buy)},
        )

    def open_position(self, signal: BaseSignal, position_size: Decimal) -> str:
        pos_id = str(uuid.uuid4())[:8]
        self._positions[pos_id] = {
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "size": position_size,
            "entry_time": datetime.now(timezone.utc),
            "current_price": signal.entry_price,
        }
        return pos_id

    def update_positions(
        self, current_price: Decimal, df: pd.DataFrame
    ) -> list[tuple[str, ExitReason]]:
        exits: list[tuple[str, ExitReason]] = []
        self._current_price = current_price

        # If price breaks below the entire grid's lower boundary → close ALL positions
        if self._grid_lower_price > 0 and current_price < self._grid_lower_price:
            for pos_id in list(self._positions):
                exits.append((pos_id, ExitReason.STOP_LOSS))
            return exits

        for pos_id, pos in list(self._positions.items()):
            pos["current_price"] = current_price
            if current_price >= pos["take_profit"]:
                exits.append((pos_id, ExitReason.TAKE_PROFIT))

        return exits

    def force_close_all(self) -> list[tuple[str, ExitReason]]:
        """Force-close all open positions (called when router deactivates this strategy)."""
        return [(pos_id, ExitReason.MANUAL) for pos_id in list(self._positions)]

    def close_position(
        self, position_id: str, exit_reason: ExitReason, exit_price: Decimal
    ) -> None:
        pos = self._positions.pop(position_id, None)
        if not pos:
            return

        pnl = (exit_price - pos["entry_price"]) * pos["size"] / pos["entry_price"]
        self._closed_trades.append(
            {
                "position_id": position_id,
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "size": pos["size"],
                "pnl": pnl,
                "exit_reason": exit_reason.value,
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(timezone.utc),
            }
        )

    def get_active_positions(self) -> list[PositionInfo]:
        result = []
        for pos_id, pos in self._positions.items():
            pnl = (
                (pos["current_price"] - pos["entry_price"]) * pos["size"] / pos["entry_price"]
                if pos["entry_price"] > 0
                else Decimal("0")
            )
            result.append(
                PositionInfo(
                    position_id=pos_id,
                    direction=pos["direction"],
                    entry_price=pos["entry_price"],
                    current_price=pos["current_price"],
                    size=pos["size"],
                    stop_loss=pos["stop_loss"],
                    take_profit=pos["take_profit"],
                    unrealized_pnl=pnl,
                    entry_time=pos["entry_time"],
                    strategy_type="grid",
                )
            )
        return result

    def get_open_positions(self) -> list[dict]:
        """
        Return open positions in the format expected by PortfolioRiskManager
        for per-symbol exposure aggregation.

        Each dict contains:
          - 'symbol': str
          - 'size': Decimal  (amount_per_grid in quote currency)
          - 'atr_stop_distance': Decimal  (grid_range_pct as fractional stop)
        """
        result = []
        for pos in self._positions.values():
            entry = pos["entry_price"]
            sl = pos["stop_loss"]
            size = pos["size"] * entry if entry > 0 else pos["size"]
            if entry > 0 and sl > 0:
                stop_distance = abs(entry - sl) / entry
            else:
                stop_distance = self._grid_range_pct
            result.append(
                {
                    "symbol": self._symbol,
                    "size": size,
                    "atr_stop_distance": stop_distance,
                }
            )
        return result

    def get_performance(self) -> StrategyPerformance:
        total = len(self._closed_trades)
        if total == 0:
            return StrategyPerformance()

        winners = [t for t in self._closed_trades if t["pnl"] > 0]
        losers = [t for t in self._closed_trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in self._closed_trades)

        return StrategyPerformance(
            total_trades=total,
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=len(winners) / total if total > 0 else 0.0,
            total_pnl=total_pnl,
            avg_trade_pnl=total_pnl / total if total > 0 else Decimal("0"),
        )

    def reset(self) -> None:
        self._grid_engine = None
        self._grid_levels = []
        self._grid_lower_price = Decimal("0")
        self._positions.clear()
        self._closed_trades.clear()
        self._last_analysis = None
        self._current_price = Decimal("0")
