"""
DCA Strategy Adapter — Wraps DCA logic to conform to BaseStrategy interface.

Translates DCA deal lifecycle into the unified signal/position interface
for use with BacktestEngine and StrategyComparison.

Bidirectional support (Phase 3, issue #400):
- LONG deals: enter when price drops from recent high (unchanged).
- SHORT deals: enter when price rises from recent low.
Both stacks are tracked independently via _short_positions.
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
    StrategyDirective,
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
        max_safety_orders: int = 3,
        price_deviation_pct: Decimal = Decimal("0.03"),
        safety_step_pct: Decimal = Decimal("0.02"),
        take_profit_pct: Decimal = Decimal("0.06"),
        name: str = "dca-default",
        # Level 1 — Universal parameter (P1.1 unification)
        risk_per_trade_pct: Optional[Decimal] = None,
        # P0 parametric fixes (issue #377)
        martingale_multiplier: Decimal = Decimal("1.3"),
        hard_stop_loss_pct: Optional[Decimal] = Decimal("0.15"),
        max_holding_bars: Optional[int] = 288,
        trend_filter_adx: Optional[float] = 30.0,
    ) -> None:
        self._symbol = symbol
        self._name = name
        self._base_order_size = base_order_size
        self._safety_order_size = safety_order_size
        self._max_safety_orders = max_safety_orders
        self._price_deviation_pct = price_deviation_pct
        self._safety_step_pct = safety_step_pct
        self._take_profit_pct = take_profit_pct
        self._martingale_multiplier = martingale_multiplier
        self._hard_stop_loss_pct = hard_stop_loss_pct
        self._max_holding_bars = max_holding_bars
        self._trend_filter_adx = trend_filter_adx
        # risk_per_trade_pct is stored for reference / future position-sizing use.
        self._risk_per_trade_pct: Optional[Decimal] = (
            Decimal(str(risk_per_trade_pct)) if risk_per_trade_pct is not None else None
        )

        self._last_analysis: BaseMarketAnalysis | None = None
        # Directive received from StrategyConductor (None = no conductor active)
        self._directive: StrategyDirective | None = None

        # DCA deal tracking
        self._positions: dict[str, dict[str, Any]] = {}
        self._closed_trades: list[dict[str, Any]] = []
        self._recent_high = Decimal("0")
        self._current_price = Decimal("0")

        # SHORT stack tracking (Phase 3, issue #400)
        # Mirrors LONG logic but averages UP on price rises.
        self._short_positions: dict[str, dict[str, Any]] = {}
        self._recent_low = Decimal("0")

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

        # Track recent high/low for DCA entry — use wider window (100 bars)
        # to avoid resetting the reference too quickly in sideways markets.
        _lookback = min(100, len(close))
        recent_high = float(max(close[-_lookback:]))
        self._recent_high = Decimal(str(recent_high))
        recent_low = float(min(close[-_lookback:]))
        self._recent_low = Decimal(str(recent_low))

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
                "recent_low": float(self._recent_low),
                "deviation_from_high": (
                    float((self._recent_high - self._current_price) / self._recent_high)
                    if self._recent_high > 0
                    else 0.0
                ),
                "deviation_from_low": (
                    float((self._current_price - self._recent_low) / self._recent_low)
                    if self._recent_low > 0
                    else 0.0
                ),
            },
        )
        return self._last_analysis

    def generate_signal(self, df: pd.DataFrame, current_balance: Decimal) -> Optional[BaseSignal]:
        """
        Generate DCA entry signal.

        LONG: when price drops by ``price_deviation_pct`` from recent high.
        SHORT (Phase 3, issue #400): when price rises by ``price_deviation_pct``
        from recent low — mirrors LONG logic.

        Only one LONG and one SHORT deal is open at any time.
        """
        if df.empty:
            return None

        close = Decimal(str(df["close"].iloc[-1]))
        self._current_price = close

        # --- LONG signal ---
        if not self._positions and self._recent_high > 0:
            deviation = (self._recent_high - close) / self._recent_high
            if deviation >= self._price_deviation_pct and current_balance >= self._base_order_size:
                take_profit = close * (Decimal("1") + self._take_profit_pct)
                total_drop = self._price_deviation_pct + (
                    self._safety_step_pct * self._max_safety_orders
                )
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

        # --- SHORT signal (mirror of LONG, Phase 3, issue #400) ---
        if not self._short_positions and self._recent_low > 0:
            deviation = (close - self._recent_low) / self._recent_low
            if deviation >= self._price_deviation_pct and current_balance >= self._base_order_size:
                take_profit = close * (Decimal("1") - self._take_profit_pct)
                total_rise = self._price_deviation_pct + (
                    self._safety_step_pct * self._max_safety_orders
                )
                stop_loss = close * (Decimal("1") + total_rise)
                return BaseSignal(
                    direction=SignalDirection.SHORT,
                    entry_price=close,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    confidence=0.7,
                    timestamp=datetime.now(timezone.utc),
                    strategy_type="dca",
                    signal_reason="dca_price_rise",
                    risk_reward_ratio=float(self._take_profit_pct / total_rise),
                    metadata={
                        "deviation_pct": float(deviation),
                        "safety_orders_available": self._max_safety_orders,
                    },
                )

        return None

    def open_position(self, signal: BaseSignal, position_size: Decimal) -> str:
        pos_id = str(uuid.uuid4())[:8]
        pos_data = {
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "avg_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "size": position_size,
            "total_invested": position_size * signal.entry_price,
            "safety_orders_filled": 0,
            "bars_held": 0,
            "entry_time": datetime.now(timezone.utc),
            "current_price": signal.entry_price,
        }
        if signal.direction == SignalDirection.SHORT:
            self._short_positions[pos_id] = pos_data
        else:
            self._positions[pos_id] = pos_data
        return pos_id

    def update_positions(
        self, current_price: Decimal, df: pd.DataFrame
    ) -> list[tuple[str, ExitReason]]:
        exits: list[tuple[str, ExitReason]] = []
        self._current_price = current_price

        # --- LONG positions ---
        for pos_id, pos in list(self._positions.items()):
            pos["current_price"] = current_price
            pos["bars_held"] = pos.get("bars_held", 0) + 1

            # Check take profit (uses stored TP; updated when safety orders average down)
            if current_price >= pos["take_profit"]:
                exits.append((pos_id, ExitReason.TAKE_PROFIT))
                continue

            # Check stop loss
            if current_price <= pos["stop_loss"]:
                exits.append((pos_id, ExitReason.STOP_LOSS))
                continue

            # Hard stop loss on full DCA position (issue #377 P0)
            if self._hard_stop_loss_pct is not None:
                loss_pct = (pos["avg_price"] - current_price) / pos["avg_price"]
                if loss_pct >= self._hard_stop_loss_pct:
                    exits.append((pos_id, ExitReason.STOP_LOSS))
                    continue

            # Max holding bars — force exit after N bars (issue #377 P0)
            if self._max_holding_bars is not None and pos["bars_held"] >= self._max_holding_bars:
                exits.append((pos_id, ExitReason.MANUAL))
                continue

            # Check safety order triggers (DCA averaging down)
            safety_filled = pos["safety_orders_filled"]
            if safety_filled < self._max_safety_orders:
                next_level_drop = self._safety_step_pct * (safety_filled + 1)
                trigger_price = pos["entry_price"] * (Decimal("1") - next_level_drop)
                if current_price <= trigger_price:
                    # Fill safety order: size grows by martingale_multiplier
                    safety_size = self._safety_order_size * (
                        self._martingale_multiplier**safety_filled
                    )
                    old_total = pos["total_invested"]
                    safety_invest = safety_size * current_price
                    new_total = old_total + safety_invest
                    old_qty = pos["size"]
                    new_qty = old_qty + safety_size
                    pos["size"] = new_qty
                    pos["total_invested"] = new_total
                    pos["avg_price"] = new_total / new_qty if new_qty > 0 else pos["avg_price"]
                    pos["safety_orders_filled"] = safety_filled + 1
                    # Update TP based on new average
                    pos["take_profit"] = pos["avg_price"] * (Decimal("1") + self._take_profit_pct)

        # --- SHORT positions (Phase 3, issue #400) ---
        for pos_id, pos in list(self._short_positions.items()):
            pos["current_price"] = current_price
            pos["bars_held"] = pos.get("bars_held", 0) + 1

            # SHORT TP: price returns to avg_entry × (1 - tp_pct)
            if current_price <= pos["take_profit"]:
                exits.append((pos_id, ExitReason.TAKE_PROFIT))
                continue

            # SHORT stop loss: price rises above stop
            if current_price >= pos["stop_loss"]:
                exits.append((pos_id, ExitReason.STOP_LOSS))
                continue

            # Hard stop loss for SHORT
            if self._hard_stop_loss_pct is not None:
                loss_pct = (current_price - pos["avg_price"]) / pos["avg_price"]
                if loss_pct >= self._hard_stop_loss_pct:
                    exits.append((pos_id, ExitReason.STOP_LOSS))
                    continue

            # Max holding bars
            if self._max_holding_bars is not None and pos["bars_held"] >= self._max_holding_bars:
                exits.append((pos_id, ExitReason.MANUAL))
                continue

            # Safety orders: averaging UP (price rises by safety_step_pct)
            safety_filled = pos["safety_orders_filled"]
            if safety_filled < self._max_safety_orders:
                next_level_rise = self._safety_step_pct * (safety_filled + 1)
                trigger_price = pos["entry_price"] * (Decimal("1") + next_level_rise)
                if current_price >= trigger_price:
                    safety_size = self._safety_order_size * (
                        self._martingale_multiplier**safety_filled
                    )
                    old_total = pos["total_invested"]
                    safety_invest = safety_size * current_price
                    new_total = old_total + safety_invest
                    old_qty = pos["size"]
                    new_qty = old_qty + safety_size
                    pos["size"] = new_qty
                    pos["total_invested"] = new_total
                    pos["avg_price"] = new_total / new_qty if new_qty > 0 else pos["avg_price"]
                    pos["safety_orders_filled"] = safety_filled + 1
                    # Update TP: price needs to fall back to avg × (1 - tp_pct)
                    pos["take_profit"] = pos["avg_price"] * (Decimal("1") - self._take_profit_pct)

        return exits

    def close_position(
        self, position_id: str, exit_reason: ExitReason, exit_price: Decimal
    ) -> None:
        # Try LONG positions first, then SHORT.
        pos = self._positions.pop(position_id, None)
        direction = SignalDirection.LONG
        if pos is None:
            pos = self._short_positions.pop(position_id, None)
            direction = SignalDirection.SHORT
        if not pos:
            return

        if direction == SignalDirection.LONG:
            pnl = (exit_price - pos["avg_price"]) * pos["size"]
        else:
            # SHORT: profit when price falls below avg_entry
            pnl = (pos["avg_price"] - exit_price) * pos["size"]

        self._closed_trades.append(
            {
                "position_id": position_id,
                "direction": direction.value,
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
        # Include SHORT positions (Phase 3, issue #400)
        for pos_id, pos in self._short_positions.items():
            pnl = (pos["avg_price"] - pos["current_price"]) * pos["size"]
            result.append(
                PositionInfo(
                    position_id=pos_id,
                    direction=SignalDirection.SHORT,
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

    def get_open_positions(self) -> list[dict]:
        """
        Return open positions in the format expected by PortfolioRiskManager
        for per-symbol exposure aggregation.

        Each dict contains:
          - 'symbol': str
          - 'size': Decimal  (total invested in quote currency)
          - 'atr_stop_distance': Decimal  (fractional stop distance from avg_price)
        """
        result = []
        for pos in list(self._positions.values()) + list(self._short_positions.values()):
            avg = pos["avg_price"]
            sl = pos["stop_loss"]
            size = pos["total_invested"]
            # atr_stop_distance as fraction of avg_price
            if avg > 0 and sl > 0:
                stop_distance = abs(avg - sl) / avg
            else:
                stop_distance = Decimal("0")
            result.append(
                {
                    "symbol": self._symbol,
                    "size": size,
                    "atr_stop_distance": stop_distance,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Stack-level close helpers (Phase 3, issue #400)
    # ------------------------------------------------------------------

    def close_long_stack(self, exit_reason: ExitReason, exit_price: Decimal) -> None:
        """
        Close all LONG positions without touching SHORT positions.

        Args:
            exit_reason: Reason for exit (e.g. ExitReason.MANUAL).
            exit_price: Price at which all LONG positions are closed.
        """
        for pos_id in list(self._positions.keys()):
            self.close_position(pos_id, exit_reason, exit_price)

    def close_short_stack(self, exit_reason: ExitReason, exit_price: Decimal) -> None:
        """
        Close all SHORT positions without touching LONG positions.

        Args:
            exit_reason: Reason for exit (e.g. ExitReason.MANUAL).
            exit_price: Price at which all SHORT positions are closed.
        """
        for pos_id in list(self._short_positions.keys()):
            self.close_position(pos_id, exit_reason, exit_price)

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

    def set_directive(self, directive: StrategyDirective) -> None:
        """Store conductor directive for use in signal generation."""
        self._directive = directive
        logger.debug(
            "dca_directive_applied",
            mode=directive.mode.value,
            price_range=directive.price_range,
            key_levels_count=len(directive.key_levels),
            capital_allocation=directive.capital_allocation,
        )

    def reset(self) -> None:
        self._positions.clear()
        self._short_positions.clear()
        self._closed_trades.clear()
        self._last_analysis = None
        self._directive = None
        self._recent_high = Decimal("0")
        self._recent_low = Decimal("0")
        self._current_price = Decimal("0")
