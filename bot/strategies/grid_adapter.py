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
    StrategyDirective,
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
        recenter_cooldown_bars: int = 6,
        adx_filter: int = 25,  # Skip new grid orders when ADX(14) > threshold (trending market)
    ) -> None:

        self._symbol = symbol
        self._name = name
        self._num_levels = num_levels
        self._profit_per_grid = profit_per_grid
        self._grid_range_pct = grid_range_pct
        self._recenter_cooldown_bars = recenter_cooldown_bars
        self._recenter_countdown: int = 0
        self._adx_filter = adx_filter
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
        # Directive received from StrategyConductor (None = no conductor active)
        self._directive: StrategyDirective | None = None

        # Position tracking
        self._positions: dict[str, dict[str, Any]] = {}
        self._closed_trades: list[dict[str, Any]] = []
        self._current_price = Decimal("0")

        # Direction support (LONG for bull/range, SHORT for bear)
        self._direction: SignalDirection = SignalDirection.LONG
        self._grid_upper_price: Decimal = Decimal("0")  # upper boundary of the entire grid

        # Recovery integration
        self._recovery_enabled: bool = False
        self._recovery_triggered: bool = False
        self._paused: bool = False

    def get_strategy_name(self) -> str:
        return self._name

    def get_strategy_type(self) -> str:
        return "grid"

    @property
    def direction(self) -> SignalDirection:
        return self._direction

    def set_direction(self, direction: SignalDirection) -> list[tuple[str, "ExitReason"]]:
        """Switch grid direction. Force-closes existing positions and resets grid.

        Returns list of (pos_id, ExitReason.MANUAL) for positions that need closing.
        """
        if direction == self._direction:
            return []
        forced = self.force_close_all()
        self._grid_engine = None
        self._grid_levels = []
        self._grid_lower_price = Decimal("0")
        self._grid_upper_price = Decimal("0")
        self._direction = direction
        self._recovery_triggered = False
        self._paused = False
        logger.info(
            "grid_direction_switched",
            new_direction=direction.value,
            closed_positions=len(forced),
        )
        return forced

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

        # Create or recenter grid when price escapes the current range.
        # Live bot recenters manually; in backtest we auto-recenter to keep
        # the grid relevant as the market trends.
        _needs_init = self._grid_engine is None
        if not _needs_init:
            # Recenter when price is outside grid bounds (with 1% buffer)
            assert self._grid_engine is not None
            _buf = self._grid_engine.upper_price * Decimal("0.01")
            _outside = (
                current > self._grid_engine.upper_price + _buf
                or current < self._grid_engine.lower_price - _buf
            )
            if _outside:
                if self._recenter_countdown > 0:
                    self._recenter_countdown -= 1
                    _needs_init = False  # cooldown active — skip recenter
                else:
                    _needs_init = True
            else:
                _needs_init = False
        if _needs_init:
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
            self._grid_upper_price = self._grid_engine.upper_price
            self._recenter_countdown = self._recenter_cooldown_bars

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

    @staticmethod
    def _compute_adx(df: pd.DataFrame, period: int = 14) -> float:
        """Compute Wilder's ADX(period). Returns NaN if insufficient data."""
        needed = period * 2 + 1
        if len(df) < needed:
            return float("nan")
        high = df["high"].values[-needed:].astype(float)
        low = df["low"].values[-needed:].astype(float)
        close = df["close"].values[-needed:].astype(float)

        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])),
        )
        up_move = high[1:] - high[:-1]
        dn_move = low[:-1] - low[1:]
        plus_dm = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)

        alpha = 1.0 / period

        def _wilder(arr: np.ndarray) -> np.ndarray:
            # Proper Wilder's smoothing:
            #   out[0] = simple mean of first `period` values
            #   out[i] = out[i-1] * (1 - 1/p) + arr[i] * (1/p)
            # Using sum instead of mean (or omitting the alpha on arr[i]) inflates
            # ATR/DM proportionally — the error cancels in the DI ratio — but
            # ADX = wilder(DX) has no such cancellation, yielding values 14× too
            # large (median ~430 instead of ~30), which blocks ALL grid signals.
            out = np.empty(len(arr))
            out[0] = arr[:period].mean()
            for i in range(1, len(arr)):
                out[i] = out[i - 1] * (1.0 - alpha) + arr[i] * alpha
            return out

        atr_w = _wilder(tr)
        plus_w = _wilder(plus_dm)
        minus_w = _wilder(minus_dm)

        with np.errstate(divide="ignore", invalid="ignore"):
            plus_di = 100.0 * plus_w / atr_w
            minus_di = 100.0 * minus_w / atr_w
            dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
        dx = np.nan_to_num(dx, nan=0.0)
        adx = _wilder(dx)
        return float(adx[-1])

    def generate_signal(self, df: pd.DataFrame, current_balance: Decimal) -> Optional[BaseSignal]:
        """Generate buy signal when price is near a grid buy level."""
        if self._paused:
            return None
        if not self._grid_levels or df.empty:
            return None

        close = Decimal(str(df["close"].iloc[-1]))
        self._current_price = close

        # ADX filter: skip new orders when market is trending (Grid loses in trends)
        if self._adx_filter > 0:
            adx_val = self._compute_adx(df)
            if not (adx_val != adx_val) and adx_val > self._adx_filter:  # NaN-safe check
                return None

        if self._direction == SignalDirection.LONG:
            # LONG: find nearest buy level below current price
            candidate_levels = [lvl for lvl in self._grid_levels if lvl < close]
            if not candidate_levels:
                return None
            nearest = max(candidate_levels)
        else:
            # SHORT: find nearest sell level above current price
            candidate_levels = [lvl for lvl in self._grid_levels if lvl > close]
            if not candidate_levels:
                return None
            nearest = min(candidate_levels)

        distance = abs(close - nearest) / close

        # Only signal if price is very close to a level (within 0.5%)
        if distance > Decimal("0.005"):
            return None

        # Check we don't already have a position at this level
        for pos in self._positions.values():
            if abs(pos["entry_price"] - nearest) / nearest < Decimal("0.003"):
                return None

        # Cost check
        if current_balance < self._amount_per_grid:
            return None

        if self._direction == SignalDirection.LONG:
            take_profit = nearest * (Decimal("1") + self._profit_per_grid)
            stop_loss = (
                self._grid_lower_price
                if self._grid_lower_price > 0
                else nearest * (Decimal("1") - self._grid_range_pct)
            )
            signal_reason = "grid_buy_level"
        else:
            take_profit = nearest * (Decimal("1") - self._profit_per_grid)
            stop_loss = (
                self._grid_upper_price
                if self._grid_upper_price > 0
                else nearest * (Decimal("1") + self._grid_range_pct)
            )
            signal_reason = "grid_sell_level"

        return BaseSignal(
            direction=self._direction,
            entry_price=close,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
            signal_reason=signal_reason,
            risk_reward_ratio=float(self._profit_per_grid / self._grid_range_pct),
            metadata={"grid_level": float(nearest)},
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

        # Boundary check: price exits grid range
        _boundary_breach = False
        if self._direction == SignalDirection.LONG:
            if self._grid_lower_price > 0 and current_price < self._grid_lower_price:
                _boundary_breach = True
        else:  # SHORT
            if self._grid_upper_price > 0 and current_price > self._grid_upper_price:
                _boundary_breach = True

        if _boundary_breach:
            if self._recovery_enabled:
                # Recovery mode: don't close — signal recovery trigger
                self._recovery_triggered = True
                self._paused = True
                return exits  # empty — positions kept alive
            else:
                # Default: close ALL positions at STOP_LOSS
                for pos_id in list(self._positions):
                    exits.append((pos_id, ExitReason.STOP_LOSS))
                return exits

        for pos_id, pos in list(self._positions.items()):
            pos["current_price"] = current_price
            # TP check: direction-aware
            if pos["direction"] == SignalDirection.LONG and current_price >= pos["take_profit"]:
                exits.append((pos_id, ExitReason.TAKE_PROFIT))
            elif pos["direction"] == SignalDirection.SHORT and current_price <= pos["take_profit"]:
                exits.append((pos_id, ExitReason.TAKE_PROFIT))
            elif pos.get("stop_loss"):
                sl = pos["stop_loss"]
                if pos["direction"] == SignalDirection.LONG and current_price <= sl:
                    exits.append((pos_id, ExitReason.STOP_LOSS))
                elif pos["direction"] == SignalDirection.SHORT and current_price >= sl:
                    exits.append((pos_id, ExitReason.STOP_LOSS))

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

        if pos["direction"] == SignalDirection.SHORT:
            pnl = (pos["entry_price"] - exit_price) * pos["size"] / pos["entry_price"]
        else:
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
            if pos["entry_price"] > 0:
                if pos["direction"] == SignalDirection.SHORT:
                    pnl = (
                        (pos["entry_price"] - pos["current_price"])
                        * pos["size"]
                        / pos["entry_price"]
                    )
                else:
                    pnl = (
                        (pos["current_price"] - pos["entry_price"])
                        * pos["size"]
                        / pos["entry_price"]
                    )
            else:
                pnl = Decimal("0")
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

    def set_directive(self, directive: StrategyDirective) -> None:
        """Store conductor directive; grid bounds may be refined by price_range."""
        self._directive = directive
        logger.debug(
            "grid_directive_applied",
            mode=directive.mode.value,
            price_range=directive.price_range,
            key_levels_count=len(directive.key_levels),
            capital_allocation=directive.capital_allocation,
        )

    # =========================================================================
    # Recovery Integration
    # =========================================================================

    def set_recovery_enabled(self, enabled: bool) -> None:
        """Enable/disable recovery mode (changes stop-loss behaviour)."""
        self._recovery_enabled = enabled

    @property
    def recovery_triggered(self) -> bool:
        """True if price broke grid lower and recovery is requested."""
        return self._recovery_triggered

    def clear_recovery_trigger(self) -> None:
        """Clear the recovery trigger flag after coordinator has picked it up."""
        self._recovery_triggered = False

    def get_underwater_snapshot(self) -> list[dict[str, Any]]:
        """Return current positions snapshot without closing them.

        Returns list of dicts with pos_id, entry_price, size.
        """
        return [
            {
                "pos_id": pos_id,
                "entry_price": pos["entry_price"],
                "size": pos["size"],
            }
            for pos_id, pos in self._positions.items()
        ]

    @property
    def grid_lower_price(self) -> Decimal:
        return self._grid_lower_price

    @property
    def grid_upper_price(self) -> Decimal:
        return self._grid_upper_price

    def pause(self) -> None:
        """Pause Grid — stops signal generation."""
        self._paused = True

    def resume(self, new_lower: Decimal, new_upper: Decimal) -> None:
        """Resume Grid with a new price range."""
        self._paused = False
        self._recovery_triggered = False
        self._grid_engine = GridEngine(
            symbol=self._symbol,
            upper_price=new_upper,
            lower_price=new_lower,
            grid_levels=self._num_levels,
            amount_per_grid=self._amount_per_grid,
            profit_per_grid=self._profit_per_grid,
        )
        self._grid_levels = self._grid_engine.calculate_grid_levels()
        self._grid_lower_price = self._grid_engine.lower_price
        self._grid_upper_price = self._grid_engine.upper_price
        self._recenter_countdown = self._recenter_cooldown_bars
        logger.info(
            "grid_resumed",
            new_lower=float(new_lower),
            new_upper=float(new_upper),
            levels=len(self._grid_levels),
        )

    def reset(self) -> None:
        self._grid_engine = None
        self._grid_levels = []
        self._grid_lower_price = Decimal("0")
        self._grid_upper_price = Decimal("0")
        self._positions.clear()
        self._closed_trades.clear()
        self._last_analysis = None
        self._directive = None
        self._current_price = Decimal("0")
        self._recovery_triggered = False
        self._paused = False
        self._direction = SignalDirection.LONG
