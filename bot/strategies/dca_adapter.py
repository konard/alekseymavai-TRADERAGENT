"""
DCA Strategy Adapter — Wraps DCA logic to conform to BaseStrategy interface.

Translates DCA deal lifecycle into the unified signal/position interface
for use with BacktestEngine and StrategyComparison.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import numpy as np
import pandas as pd

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


class DCAAdapter(BaseStrategy):
    """
    Adapter that implements DCA strategy as BaseStrategy.

    Simplified DCA for backtesting:
    - Buys when price drops by a configurable percentage from recent high
    - Averages down with safety orders at configurable step sizes
    - Takes profit when price recovers by target percentage
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        base_order_size: Decimal = Decimal("100"),
        safety_order_size: Decimal = Decimal("200"),
        max_safety_orders: int = 4,
        price_deviation_pct: Decimal = Decimal("0.04"),
        safety_step_pct: Decimal = Decimal("0.02"),
        take_profit_pct: Decimal = Decimal("0.08"),
        name: str = "dca-default",
        # Level 1 — Universal parameter (P1.1 unification)
        risk_per_trade_pct: Optional[Decimal] = None,
    ) -> None:
        self._symbol = symbol
        self._name = name
        self._base_order_size = base_order_size
        self._safety_order_size = safety_order_size
        self._max_safety_orders = max_safety_orders
        self._price_deviation_pct = price_deviation_pct
        self._safety_step_pct = safety_step_pct
        self._take_profit_pct = take_profit_pct
        # risk_per_trade_pct is stored for reference / future position-sizing use.
        self._risk_per_trade_pct: Optional[Decimal] = (
            Decimal(str(risk_per_trade_pct)) if risk_per_trade_pct is not None else None
        )

        self._last_analysis: BaseMarketAnalysis | None = None

        # DCA deal tracking
        self._positions: dict[str, dict[str, Any]] = {}
        self._closed_trades: list[dict[str, Any]] = []
        self._recent_high = Decimal("0")
        self._current_price = Decimal("0")

    def get_strategy_name(self) -> str:
        return self._name

    def get_strategy_type(self) -> str:
        return "dca"

    def analyze_market(self, *dfs: pd.DataFrame) -> BaseMarketAnalysis:
        """Analyze market for DCA entry conditions."""
        df = dfs[-1] if dfs else pd.DataFrame()

        if df.empty or len(df) < 5:
            return BaseMarketAnalysis(
                trend="unknown",
                trend_strength=0.0,
                volatility=0.0,
                timestamp=datetime.now(timezone.utc),
                strategy_type="dca",
            )

        close = df["close"].values
        self._current_price = Decimal(str(close[-1]))

        # Track recent high for DCA entry
        recent_high = float(max(close[-20:]))
        self._recent_high = Decimal(str(recent_high))

        # Volatility
        high = df["high"].values
        low = df["low"].values
        tr = high - low
        atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))
        volatility = atr / float(close[-1]) if close[-1] > 0 else 0.0

        # Trend
        price_change = (close[-1] - close[0]) / close[0] if close[0] > 0 else 0.0
        if price_change < -0.02:
            trend = "bearish"
            trend_strength = min(abs(price_change) * 10, 1.0)
        elif price_change > 0.02:
            trend = "bullish"
            trend_strength = min(abs(price_change) * 10, 1.0)
        else:
            trend = "sideways"
            trend_strength = 0.3

        self._last_analysis = BaseMarketAnalysis(
            trend=trend,
            trend_strength=trend_strength,
            volatility=volatility,
            timestamp=datetime.now(timezone.utc),
            strategy_type="dca",
            details={
                "recent_high": float(self._recent_high),
                "deviation_from_high": (
                    float((self._recent_high - self._current_price) / self._recent_high)
                    if self._recent_high > 0
                    else 0.0
                ),
            },
        )
        return self._last_analysis

    def generate_signal(self, df: pd.DataFrame, current_balance: Decimal) -> Optional[BaseSignal]:
        """Generate DCA entry signal when price drops from recent high."""
        if df.empty or self._recent_high <= 0:
            return None

        close = Decimal(str(df["close"].iloc[-1]))
        self._current_price = close

        # Check if we already have an active deal
        if self._positions:
            return None

        # Check if price has dropped enough from recent high
        deviation = (self._recent_high - close) / self._recent_high
        if deviation < self._price_deviation_pct:
            return None

        # Cost check
        if current_balance < self._base_order_size:
            return None

        # Calculate TP and SL
        take_profit = close * (Decimal("1") + self._take_profit_pct)
        # SL after all safety orders would be hit
        total_drop = self._price_deviation_pct + (self._safety_step_pct * self._max_safety_orders)
        stop_loss = close * (Decimal("1") - total_drop)

        return BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=close,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=0.7,
            timestamp=datetime.now(timezone.utc),
            strategy_type="dca",
            signal_reason="dca_price_drop",
            risk_reward_ratio=float(self._take_profit_pct / total_drop),
            metadata={
                "deviation_pct": float(deviation),
                "safety_orders_available": self._max_safety_orders,
            },
        )

    def open_position(self, signal: BaseSignal, position_size: Decimal) -> str:
        pos_id = str(uuid.uuid4())[:8]
        self._positions[pos_id] = {
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "avg_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "size": position_size,
            "total_invested": position_size * signal.entry_price,
            "safety_orders_filled": 0,
            "entry_time": datetime.now(timezone.utc),
            "current_price": signal.entry_price,
        }
        return pos_id

    def update_positions(
        self, current_price: Decimal, df: pd.DataFrame
    ) -> list[tuple[str, ExitReason]]:
        exits: list[tuple[str, ExitReason]] = []
        self._current_price = current_price

        for pos_id, pos in list(self._positions.items()):
            pos["current_price"] = current_price

            # Check take profit using stored TP (set at open, updated after safety orders)
            if current_price >= pos["take_profit"]:
                exits.append((pos_id, ExitReason.TAKE_PROFIT))
                continue

            # Check stop loss
            if current_price <= pos["stop_loss"]:
                exits.append((pos_id, ExitReason.STOP_LOSS))
                continue

            # Check safety order triggers (DCA averaging down)
            safety_filled = pos["safety_orders_filled"]
            if safety_filled < self._max_safety_orders:
                next_level_drop = self._safety_step_pct * (safety_filled + 1)
                trigger_price = pos["entry_price"] * (Decimal("1") - next_level_drop)
                if current_price <= trigger_price:
                    # Fill safety order: average down
                    old_total = pos["total_invested"]
                    safety_invest = self._safety_order_size * current_price
                    new_total = old_total + safety_invest
                    old_qty = pos["size"]
                    new_qty = old_qty + self._safety_order_size
                    pos["size"] = new_qty
                    pos["total_invested"] = new_total
                    pos["avg_price"] = new_total / new_qty if new_qty > 0 else pos["avg_price"]
                    pos["safety_orders_filled"] = safety_filled + 1
                    # Update TP based on new average
                    pos["take_profit"] = pos["avg_price"] * (Decimal("1") + self._take_profit_pct)

        return exits

    def close_position(
        self, position_id: str, exit_reason: ExitReason, exit_price: Decimal
    ) -> None:
        pos = self._positions.pop(position_id, None)
        if not pos:
            return

        pnl = (exit_price - pos["avg_price"]) * pos["size"]
        self._closed_trades.append(
            {
                "position_id": position_id,
                "entry_price": pos["entry_price"],
                "avg_price": pos["avg_price"],
                "exit_price": exit_price,
                "size": pos["size"],
                "pnl": pnl,
                "exit_reason": exit_reason.value,
                "safety_orders_filled": pos["safety_orders_filled"],
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now(timezone.utc),
            }
        )

    def get_active_positions(self) -> list[PositionInfo]:
        result = []
        for pos_id, pos in self._positions.items():
            pnl = (pos["current_price"] - pos["avg_price"]) * pos["size"]
            result.append(
                PositionInfo(
                    position_id=pos_id,
                    direction=pos["direction"],
                    entry_price=pos["avg_price"],
                    current_price=pos["current_price"],
                    size=pos["size"],
                    stop_loss=pos["stop_loss"],
                    take_profit=pos["take_profit"],
                    unrealized_pnl=pnl,
                    entry_time=pos["entry_time"],
                    strategy_type="dca",
                    metadata={
                        "safety_orders_filled": pos["safety_orders_filled"],
                    },
                )
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
            metadata={
                "avg_safety_orders": (
                    sum(t["safety_orders_filled"] for t in self._closed_trades) / total
                    if total > 0
                    else 0
                ),
            },
        )

    def reset(self) -> None:
        self._positions.clear()
        self._closed_trades.clear()
        self._last_analysis = None
        self._recent_high = Decimal("0")
        self._current_price = Decimal("0")
