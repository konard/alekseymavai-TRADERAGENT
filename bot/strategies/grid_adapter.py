"""
Grid Strategy Adapter — Wraps GridEngine to conform to BaseStrategy interface.

Translates grid-level order logic into the unified signal/position lifecycle
for use with BacktestEngine and StrategyComparison.

Phase 2 (issue #379) additions:
- Aggregate SL via VirtualPositionManager instead of raw price-boundary check
- Soft Recenter with cooldown: cancel only pending BUY orders, keep SELL (TP)
- reconcile_grid_orders call on recenter to clean orphaned SELL orders
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import numpy as np
import pandas as pd

from bot.core.grid_engine import GridEngine
from bot.core.virtual_position_manager import VirtualPosition, VirtualPositionManager
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
    - analyze_market: computes price range and volatility; handles soft recenter
    - generate_signal: buys when price near a grid buy level
    - update_positions: delegates SL/TP to VirtualPositionManager (aggregate SL)

    Phase 2 improvements (issue #379):
    - Bug 1 fix: Aggregate SL via VPM replaces raw _grid_lower_price check.
    - Bug 2 fix: Soft recenter with cooldown — never recreates GridEngine,
      only updates levels; does NOT close existing positions.
    - Bug 3 fix: reconcile_grid_orders removes orphaned SELL orders after recenter.
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
        # Phase 2 — Aggregate SL
        max_grid_loss_pct: Decimal = Decimal("0.10"),
        # Phase 2 — Soft Recenter cooldown
        recenter_cooldown_bars: int = 24,
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
        # Directive received from StrategyConductor (None = no conductor active)
        self._directive: StrategyDirective | None = None

        # Position tracking
        self._positions: dict[str, dict[str, Any]] = {}
        self._closed_trades: list[dict[str, Any]] = []
        self._current_price = Decimal("0")

        # Phase 2 — VirtualPositionManager for aggregate SL
        self._vpm = VirtualPositionManager(max_grid_loss_pct=float(max_grid_loss_pct))

        # Phase 2 — Soft Recenter cooldown
        self._recenter_cooldown_bars: int = recenter_cooldown_bars
        self._recenter_countdown: int = 0  # bars remaining before recenter allowed

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

        if self._grid_engine is None:
            # Initialize grid engine once so levels stay fixed initially
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
        else:
            # Phase 2 — Soft Recenter logic
            # Check if price has drifted outside current grid bounds
            needs_recenter = current < self._grid_lower_price or current > (
                self._grid_engine.upper_price
                if self._grid_engine is not None
                else current + Decimal("1")
            )

            if needs_recenter:
                open_count = len(self._positions)
                half_levels = self._num_levels // 2

                if self._recenter_countdown > 0:
                    # Still in cooldown — skip recenter this bar
                    self._recenter_countdown -= 1
                    logger.debug(
                        "grid_recenter_cooldown_skipped",
                        countdown=self._recenter_countdown,
                        open_positions=open_count,
                    )
                elif open_count > half_levels:
                    # Too many open positions — wait for TP fills
                    logger.debug(
                        "grid_recenter_deferred_too_many_positions",
                        open_positions=open_count,
                        half_levels=half_levels,
                    )
                else:
                    # Perform soft recenter: update grid levels WITHOUT closing positions
                    self._soft_recenter(upper, lower)

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

    def _soft_recenter(self, new_upper: Decimal, new_lower: Decimal) -> list[str]:
        """
        Update grid bounds without closing existing positions (soft recenter).

        Cancels only PENDING BUY orders (no fills yet).  Existing SELL (TP)
        orders are left intact so filled positions can still take profit.
        Calls reconcile_grid_orders to remove orphaned SELL orders.

        Returns:
            List of order_ids that should be cancelled on the exchange.
        """
        assert self._grid_engine is not None

        old_lower = self._grid_engine.lower_price
        old_upper = self._grid_engine.upper_price

        # Update engine bounds in-place (no new GridEngine instance)
        self._grid_engine.upper_price = new_upper
        self._grid_engine.lower_price = new_lower
        self._grid_lower_price = new_lower
        self._grid_levels = self._grid_engine.calculate_grid_levels()

        # Reset cooldown
        self._recenter_countdown = self._recenter_cooldown_bars

        # Cancel only pending BUY orders (no fills → no VPM positions)
        buy_order_ids = [
            oid
            for oid, order in list(self._grid_engine.active_orders.items())
            if order.side == "buy"
        ]
        for oid in buy_order_ids:
            self._grid_engine.cancel_order(oid)

        # Reconcile: find orphaned SELL orders (no paired VPM position)
        # vpm_positions is a sync snapshot; we use the internal dict directly
        # because this is called from synchronous analyze_market context.
        vpm_grid_positions = [p for p in self._vpm._positions.values() if p.symbol == self._symbol]
        orphan_ids = self._grid_engine.reconcile_grid_orders(vpm_grid_positions)
        for oid in orphan_ids:
            self._grid_engine.cancel_order(oid)

        logger.info(
            "grid_soft_recenter",
            symbol=self._symbol,
            old_lower=float(old_lower),
            old_upper=float(old_upper),
            new_lower=float(new_lower),
            new_upper=float(new_upper),
            cancelled_buy_orders=len(buy_order_ids),
            cancelled_orphan_sells=len(orphan_ids),
            open_positions=len(self._positions),
            cooldown_bars=self._recenter_cooldown_bars,
        )

        return buy_order_ids + orphan_ids

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
        # Phase 2: SL is managed by VPM aggregate SL; no per-position SL price
        # needed here (VPM tracks positions and fires aggregate_sl at max_grid_loss_pct).
        stop_loss = (
            self._grid_lower_price
            if self._grid_lower_price > 0
            else nearest_buy * (Decimal("1") - self._grid_range_pct)
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

        # Register in VPM for aggregate SL tracking
        # qty approximated as position_size / entry_price (base currency)
        qty = position_size / signal.entry_price if signal.entry_price > 0 else Decimal("0.001")
        vp = VirtualPositionManager.new_position(
            strategy="grid",
            symbol=self._symbol,
            side="long",
            qty=qty,
            entry_price=signal.entry_price,
            tp_price=signal.take_profit,
            sl_price=None,  # individual SL delegated to aggregate SL in VPM
            meta={"adapter_pos_id": pos_id},
        )
        # Store VPM pos_id cross-reference in the local position dict
        self._positions[pos_id]["vpm_pos_id"] = vp.pos_id

        # Register synchronously using the internal dict directly
        # (GridAdapter is synchronous; VPM lock is only needed for async callers)
        self._vpm._positions[vp.pos_id] = vp

        return pos_id

    def update_positions(
        self, current_price: Decimal, df: pd.DataFrame
    ) -> list[tuple[str, ExitReason]]:
        """
        Check for exits using VirtualPositionManager (aggregate SL + individual TP).

        Phase 2 change: replaces raw `current_price < _grid_lower_price` check
        with VPM.check_exits() which fires aggregate_sl at max_grid_loss_pct.
        Individual TP is still fired per-position when price hits take_profit.
        """
        exits: list[tuple[str, ExitReason]] = []
        self._current_price = current_price

        # Update current_price in local position dicts
        for pos in self._positions.values():
            pos["current_price"] = current_price

        # Run VPM exit check synchronously (access internal state directly
        # since GridAdapter operates in synchronous backtest context)
        exits_set: set[str] = set()
        vpm_exits: list[tuple[VirtualPosition, str]] = []

        for vp in list(self._vpm._positions.values()):
            if vp.is_tp_hit(current_price):
                vpm_exits.append((vp, "take_profit"))
                exits_set.add(vp.pos_id)
            elif vp.is_sl_hit(current_price):
                vpm_exits.append((vp, "stop_loss"))
                exits_set.add(vp.pos_id)

        # Aggregate SL for grid
        grid_positions = [p for p in self._vpm._positions.values() if p.strategy == "grid"]
        if grid_positions:
            total_invested = sum(p.entry_price * p.qty for p in grid_positions)
            total_unrealized = sum(p.unrealized_pnl(current_price) for p in grid_positions)
            if total_invested > 0:
                loss_pct = -total_unrealized / total_invested
                if loss_pct >= self._vpm._max_grid_loss_pct:
                    logger.warning(
                        "grid_aggregate_sl_triggered",
                        loss_pct=float(loss_pct),
                        max_loss_pct=float(self._vpm._max_grid_loss_pct),
                        grid_position_count=len(grid_positions),
                    )
                    for vp in grid_positions:
                        if vp.pos_id not in exits_set:
                            vpm_exits.append((vp, "aggregate_sl"))
                            exits_set.add(vp.pos_id)

        # Map VPM exits back to adapter position IDs
        vpm_pos_to_adapter: dict[str, str] = {
            pos["vpm_pos_id"]: pid for pid, pos in self._positions.items() if "vpm_pos_id" in pos
        }

        for vp, reason in vpm_exits:
            adapter_pos_id = vpm_pos_to_adapter.get(vp.pos_id)
            if adapter_pos_id is None:
                continue
            if reason in ("take_profit",):
                exits.append((adapter_pos_id, ExitReason.TAKE_PROFIT))
            elif reason in ("stop_loss", "aggregate_sl"):
                exits.append((adapter_pos_id, ExitReason.STOP_LOSS))

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

        # Remove from VPM
        vpm_pos_id = pos.get("vpm_pos_id")
        if vpm_pos_id and vpm_pos_id in self._vpm._positions:
            vp = self._vpm._positions.pop(vpm_pos_id)
            real_pnl = vp.calc_pnl(exit_price)
            self._vpm._archive.append(
                {
                    "pos_id": vpm_pos_id,
                    "strategy": "grid",
                    "symbol": self._symbol,
                    "side": "long",
                    "qty": vp.qty,
                    "entry_price": vp.entry_price,
                    "exit_price": exit_price,
                    "tp_price": vp.tp_price,
                    "sl_price": vp.sl_price,
                    "opened_at": vp.opened_at,
                    "closed_at": datetime.now(timezone.utc),
                    "reason": exit_reason.value,
                    "pnl": real_pnl,
                    "meta": vp.meta,
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

    def reset(self) -> None:
        self._grid_engine = None
        self._grid_levels = []
        self._grid_lower_price = Decimal("0")
        self._positions.clear()
        self._closed_trades.clear()
        self._last_analysis = None
        self._directive = None
        self._current_price = Decimal("0")
        self._vpm = VirtualPositionManager(max_grid_loss_pct=float(self._vpm._max_grid_loss_pct))
        self._recenter_countdown = 0
