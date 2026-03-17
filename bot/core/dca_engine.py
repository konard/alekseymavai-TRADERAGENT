"""
DCAEngine - Dollar Cost Averaging strategy implementation
Handles DCA trigger monitoring, position averaging, and take profit management

Bidirectional DCA:
- LONG stack: averages DOWN on price drops (buy more as price falls)
- SHORT stack: averages UP on price rises (sell more as price rises)
Both stacks are independent and can be operated simultaneously (hedged mode).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from bot.utils.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# Bidirectional DCA data structures
# =============================================================================


@dataclass
class DCALevel:
    """A single entry level in a DCA stack (LONG or SHORT)."""

    price: Decimal
    amount: Decimal  # base asset quantity


@dataclass
class CloseOrder:
    """
    Instruction to close (exit) one DCA stack level on the exchange.

    For LONG levels: place a market/limit sell order.
    For SHORT levels: place a market/limit buy order (to cover).
    """

    price: Decimal  # reference price at which to close
    amount: Decimal  # base asset quantity to trade
    side: str  # "sell" (LONG close) | "buy" (SHORT close)


class DCAPosition:
    """Represents a DCA position"""

    def __init__(
        self,
        symbol: str,
        entry_price: Decimal,
        amount: Decimal,
        step_number: int = 0,
    ):
        self.symbol = symbol
        self.entry_price = entry_price
        self.amount = amount
        self.step_number = step_number
        self.total_cost = entry_price * amount
        self.average_entry_price = entry_price

    def add_position(self, price: Decimal, amount: Decimal) -> None:
        """
        Add to position (DCA step).

        Args:
            price: Entry price for this step
            amount: Amount to add
        """
        self.total_cost += price * amount
        self.amount += amount
        self.average_entry_price = self.total_cost / self.amount
        self.step_number += 1

    def get_pnl(self, current_price: Decimal) -> Decimal:
        """
        Calculate current profit/loss.

        Args:
            current_price: Current market price

        Returns:
            PnL amount
        """
        current_value = current_price * self.amount
        return current_value - self.total_cost

    def get_pnl_percentage(self, current_price: Decimal) -> Decimal:
        """
        Calculate current profit/loss percentage.

        Args:
            current_price: Current market price

        Returns:
            PnL percentage
        """
        if self.total_cost == 0:
            return Decimal("0")
        return (current_price - self.average_entry_price) / self.average_entry_price

    def __repr__(self) -> str:
        return (
            f"DCAPosition(symbol={self.symbol}, "
            f"avg_entry={self.average_entry_price}, "
            f"amount={self.amount}, steps={self.step_number})"
        )


class DCAEngine:
    """
    Dollar Cost Averaging engine implementation.

    Features:
    - Monitor price triggers for DCA execution
    - Calculate average entry price across multiple buys
    - Manage DCA steps with configurable limits
    - Calculate take profit levels based on average entry
    - Integration with GridEngine for hybrid strategies
    """

    def __init__(
        self,
        symbol: str,
        trigger_percentage: Decimal,
        amount_per_step: Decimal,
        max_steps: int,
        take_profit_percentage: Decimal,
    ):
        """
        Initialize DCA Engine.

        Args:
            symbol: Trading pair symbol (e.g., 'BTC/USDT')
            trigger_percentage: Price drop to trigger DCA (0.05 = 5%)
            amount_per_step: Amount to buy per DCA step
            max_steps: Maximum number of DCA steps
            take_profit_percentage: Take profit percentage (0.1 = 10%)
        """
        if trigger_percentage <= 0 or trigger_percentage > 1:
            raise ValueError("trigger_percentage must be between 0 and 1")
        if amount_per_step <= 0:
            raise ValueError("amount_per_step must be positive")
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if take_profit_percentage <= 0:
            raise ValueError("take_profit_percentage must be positive")

        self.symbol = symbol
        self.trigger_percentage = trigger_percentage
        self.amount_per_step = amount_per_step
        self.max_steps = max_steps
        self.take_profit_percentage = take_profit_percentage

        # Position tracking
        self.position: DCAPosition | None = None
        self.last_buy_price: Decimal | None = None
        self.highest_price_since_entry: Decimal | None = None

        # Optional shared CorePosition for Hybrid mode coordination.
        # When set, each DCA fill is mirrored into this shared object so
        # GridEngine can adapt its order grid accordingly.
        # When None (default), DCAEngine behaves exactly as before.
        self._core_position: Optional[CorePosition] = None  # type: ignore[name-defined]  # noqa: F821

        # ------------------------------------------------------------------
        # Bidirectional stacks (Phase 3 — issue #400)
        # ------------------------------------------------------------------
        # LONG stack: each DCALevel was bought as price fell.
        # Converted from the existing `position` when needed, but also
        # tracked explicitly for the new API.
        self._long_levels: list[DCALevel] = []

        # SHORT stack: each DCALevel was sold as price rose.
        self._short_levels: list[DCALevel] = []
        self._last_short_price: Decimal | None = None  # last SHORT entry price

        # Statistics
        self.total_dca_steps = 0
        self.total_invested = Decimal("0")
        self.realized_profit = Decimal("0")

        logger.info(
            "DCAEngine initialized",
            symbol=symbol,
            trigger_percentage=float(trigger_percentage),
            max_steps=max_steps,
        )

    def check_dca_trigger(self, current_price: Decimal) -> bool:
        """
        Check if DCA should be triggered.

        Args:
            current_price: Current market price

        Returns:
            True if DCA should be triggered, False otherwise
        """
        # No position yet - can start DCA
        if self.position is None:
            return True

        # Check if max steps reached
        if self.position.step_number >= self.max_steps:
            logger.debug(
                "Max DCA steps reached",
                current_steps=self.position.step_number,
                max_steps=self.max_steps,
            )
            return False

        # Check if price dropped enough from last buy
        if self.last_buy_price is not None:
            price_drop = (self.last_buy_price - current_price) / self.last_buy_price
            if price_drop >= self.trigger_percentage:
                logger.info(
                    "DCA trigger activated",
                    current_price=float(current_price),
                    last_buy_price=float(self.last_buy_price),
                    price_drop=float(price_drop),
                )
                return True

        return False

    def execute_dca_step(self, current_price: Decimal) -> bool:
        """
        Execute a DCA step.

        Args:
            current_price: Current market price

        Returns:
            True if DCA step was executed, False otherwise
        """
        if not self.check_dca_trigger(current_price):
            return False

        if self.position is None:
            # First entry
            self.position = DCAPosition(
                symbol=self.symbol,
                entry_price=current_price,
                amount=self.amount_per_step,
                step_number=1,
            )
            logger.info(
                "Initial DCA position opened",
                price=float(current_price),
                amount=float(self.amount_per_step),
            )
        else:
            # Additional DCA step
            self.position.add_position(current_price, self.amount_per_step)
            logger.info(
                "DCA step executed",
                step=self.position.step_number,
                price=float(current_price),
                avg_entry=float(self.position.average_entry_price),
            )

        self.last_buy_price = current_price
        self.highest_price_since_entry = current_price
        self.total_dca_steps += 1
        self.total_invested += current_price * self.amount_per_step

        # Track in the explicit LONG stack as well.
        self._long_levels.append(DCALevel(price=current_price, amount=self.amount_per_step))

        # Mirror fill into the shared CorePosition so Grid can adapt.
        if self._core_position is not None:
            self._core_position.update_from_fill(current_price, self.amount_per_step)

        return True

    def check_take_profit(self, current_price: Decimal) -> bool:
        """
        Check if take profit level is reached.

        Args:
            current_price: Current market price

        Returns:
            True if take profit should be executed, False otherwise
        """
        if self.position is None:
            return False

        # Update highest price tracking
        if self.highest_price_since_entry is None or current_price > self.highest_price_since_entry:
            self.highest_price_since_entry = current_price

        # Calculate current profit percentage
        profit_pct = self.position.get_pnl_percentage(current_price)

        if profit_pct >= self.take_profit_percentage:
            logger.info(
                "Take profit triggered",
                current_price=float(current_price),
                avg_entry=float(self.position.average_entry_price),
                profit_pct=float(profit_pct),
            )
            return True

        return False

    def close_position(self, exit_price: Decimal) -> Decimal:
        """
        Close the current position.

        Args:
            exit_price: Exit price for position

        Returns:
            Realized profit/loss
        """
        if self.position is None:
            logger.warning("No position to close")
            return Decimal("0")

        pnl = self.position.get_pnl(exit_price)
        self.realized_profit += pnl

        logger.info(
            "Position closed",
            exit_price=float(exit_price),
            avg_entry=float(self.position.average_entry_price),
            amount=float(self.position.amount),
            pnl=float(pnl),
            pnl_pct=float(self.position.get_pnl_percentage(exit_price)),
        )

        # Reset position
        self.position = None
        self.last_buy_price = None
        self.highest_price_since_entry = None
        self._long_levels.clear()

        return pnl

    def get_target_sell_price(self) -> Decimal | None:
        """
        Calculate target sell price based on average entry.

        Returns:
            Target sell price, or None if no position
        """
        if self.position is None:
            return None

        target_price = self.position.average_entry_price * (
            Decimal("1") + self.take_profit_percentage
        )
        return target_price

    def get_next_dca_trigger_price(self) -> Decimal | None:
        """
        Calculate next DCA trigger price.

        Returns:
            Next trigger price, or None if no position or max steps reached
        """
        if self.position is None:
            return None

        if self.position.step_number >= self.max_steps:
            return None

        if self.last_buy_price is None:
            return None

        trigger_price = self.last_buy_price * (Decimal("1") - self.trigger_percentage)
        return trigger_price

    def update_price(self, current_price: Decimal) -> dict:
        """
        Update engine with current price and check triggers.

        Args:
            current_price: Current market price

        Returns:
            Dictionary with actions to take
        """
        actions = {
            "execute_dca": False,
            "take_profit": False,
            "dca_triggered": False,
            "tp_triggered": False,
        }

        # Check take profit first (higher priority)
        if self.check_take_profit(current_price):
            actions["tp_triggered"] = True
            actions["take_profit"] = True
            return actions

        # Check DCA trigger
        if self.check_dca_trigger(current_price):
            actions["dca_triggered"] = True
            actions["execute_dca"] = True

        return actions

    def get_total_invested(self) -> Decimal:
        """
        Get total capital currently invested in the open DCA position.

        Returns the ``total_cost`` of the active position (quote currency),
        which represents live exposure.  Returns ``Decimal("0")`` when no
        position is open.
        """
        if self.position is None:
            return Decimal("0")
        return self.position.total_cost

    def get_position_status(self) -> dict:
        """
        Get current position status and statistics.

        Returns:
            Dictionary with position status information
        """
        if self.position is None:
            return {
                "has_position": False,
                "symbol": self.symbol,
                "total_dca_steps": self.total_dca_steps,
                "total_invested": float(self.total_invested),
                "realized_profit": float(self.realized_profit),
            }

        return {
            "has_position": True,
            "symbol": self.symbol,
            "average_entry_price": float(self.position.average_entry_price),
            "position_amount": float(self.position.amount),
            "current_step": self.position.step_number,
            "max_steps": self.max_steps,
            "total_cost": float(self.position.total_cost),
            "target_sell_price": float(self.get_target_sell_price() or 0),
            "next_dca_trigger": float(self.get_next_dca_trigger_price() or 0),
            "total_dca_steps": self.total_dca_steps,
            "total_invested": float(self.total_invested),
            "realized_profit": float(self.realized_profit),
        }

    # ------------------------------------------------------------------
    # SHORT stack — bidirectional DCA (Phase 3, issue #400)
    # ------------------------------------------------------------------

    def check_short_dca_trigger(self, current_price: Decimal) -> bool:
        """
        Check if a new SHORT DCA level should be added.

        Trigger condition (mirror of LONG):
        - No SHORT levels yet → first SHORT entry allowed.
        - Otherwise: price has RISEN by at least ``trigger_percentage``
          from the last SHORT entry price.

        Args:
            current_price: Current market price.

        Returns:
            True if a SHORT DCA step should be executed.
        """
        if not self._short_levels:
            # No SHORT position yet — open the first level.
            return True

        if len(self._short_levels) >= self.max_steps:
            logger.debug(
                "Max SHORT DCA steps reached",
                current_steps=len(self._short_levels),
                max_steps=self.max_steps,
            )
            return False

        if self._last_short_price is not None:
            price_rise = (current_price - self._last_short_price) / self._last_short_price
            if price_rise >= self.trigger_percentage:
                logger.info(
                    "SHORT DCA trigger activated",
                    current_price=float(current_price),
                    last_short_price=float(self._last_short_price),
                    price_rise=float(price_rise),
                )
                return True

        return False

    def execute_short_dca_step(self, current_price: Decimal) -> bool:
        """
        Execute one SHORT DCA step if the trigger condition is met.

        Adds a ``DCALevel`` to ``_short_levels`` and updates
        ``_last_short_price``.

        Args:
            current_price: Current market price.

        Returns:
            True if the SHORT step was executed, False otherwise.
        """
        if not self.check_short_dca_trigger(current_price):
            return False

        self._short_levels.append(DCALevel(price=current_price, amount=self.amount_per_step))
        self._last_short_price = current_price

        logger.info(
            "SHORT DCA step executed",
            step=len(self._short_levels),
            price=float(current_price),
            avg_short_entry=float(self._short_avg_entry()),
        )
        return True

    def _short_avg_entry(self) -> Decimal:
        """Weighted average entry price across all SHORT levels."""
        if not self._short_levels:
            return Decimal("0")
        total_cost = sum(lvl.price * lvl.amount for lvl in self._short_levels)
        total_amount = sum(lvl.amount for lvl in self._short_levels)
        return total_cost / total_amount if total_amount else Decimal("0")

    def _long_avg_entry(self) -> Decimal:
        """Weighted average entry price across all LONG levels."""
        if not self._long_levels:
            return Decimal("0")
        total_cost = sum(lvl.price * lvl.amount for lvl in self._long_levels)
        total_amount = sum(lvl.amount for lvl in self._long_levels)
        return total_cost / total_amount if total_amount else Decimal("0")

    def check_short_take_profit(self, current_price: Decimal) -> bool:
        """
        Check if the SHORT stack's take-profit is reached.

        TP for SHORT: price falls back to ``short_avg_entry × (1 - tp_pct)``.

        Args:
            current_price: Current market price.

        Returns:
            True if SHORT TP should be triggered.
        """
        if not self._short_levels:
            return False
        tp_price = self._short_avg_entry() * (Decimal("1") - self.take_profit_percentage)
        if current_price <= tp_price:
            logger.info(
                "SHORT take profit triggered",
                current_price=float(current_price),
                short_avg_entry=float(self._short_avg_entry()),
                tp_price=float(tp_price),
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Combined PnL and stack-close helpers (Phase 3, issue #400)
    # ------------------------------------------------------------------

    def _long_pnl(self, current_price: Decimal) -> Decimal:
        """
        Unrealized PnL for the LONG stack at *current_price*.

        For each LONG level we bought at *level.price* and now the price
        is *current_price*, so PnL = (current_price - level.price) × amount.
        """
        return sum((current_price - lvl.price) * lvl.amount for lvl in self._long_levels)

    def _short_pnl(self, current_price: Decimal) -> Decimal:
        """
        Unrealized PnL for the SHORT stack at *current_price*.

        For each SHORT level we sold at *level.price* and the price is now
        *current_price*, so PnL = (level.price - current_price) × amount.
        """
        return sum((lvl.price - current_price) * lvl.amount for lvl in self._short_levels)

    def get_combined_pnl(self, current_price: Decimal) -> Decimal:
        """
        Combined unrealized PnL of LONG stack + SHORT stack.

        Args:
            current_price: Current market price.

        Returns:
            Sum of LONG PnL and SHORT PnL (can be negative).
        """
        return self._long_pnl(current_price) + self._short_pnl(current_price)

    def close_long_stack(self) -> list[CloseOrder]:
        """
        Build close orders for all LONG levels and clear the stack.

        Does NOT touch the SHORT stack.

        Returns:
            List of ``CloseOrder`` (sell orders) — one per LONG level.
        """
        orders: list[CloseOrder] = [
            CloseOrder(price=lvl.price, amount=lvl.amount, side="sell") for lvl in self._long_levels
        ]
        self._long_levels.clear()
        # Keep backward-compat: also clear the legacy position object.
        self.position = None
        self.last_buy_price = None
        self.highest_price_since_entry = None
        logger.info("LONG stack closed", orders_count=len(orders), symbol=self.symbol)
        return orders

    def close_short_stack(self) -> list[CloseOrder]:
        """
        Build close orders for all SHORT levels and clear the stack.

        Does NOT touch the LONG stack.

        Returns:
            List of ``CloseOrder`` (buy-to-cover orders) — one per SHORT level.
        """
        orders: list[CloseOrder] = [
            CloseOrder(price=lvl.price, amount=lvl.amount, side="buy") for lvl in self._short_levels
        ]
        self._short_levels.clear()
        self._last_short_price = None
        logger.info("SHORT stack closed", orders_count=len(orders), symbol=self.symbol)
        return orders

    def get_open_positions(self) -> list[dict]:
        """
        Return open positions in the format expected by PortfolioRiskManager
        for per-symbol exposure aggregation.

        Each dict contains:
          - 'symbol': str
          - 'size': Decimal  (total invested in quote currency)
          - 'atr_stop_distance': Decimal  (trigger_percentage as fractional stop proxy)
        """
        if self.position is None:
            return []
        return [
            {
                "symbol": self.symbol,
                "size": self.position.total_cost,
                "atr_stop_distance": self.trigger_percentage,
            }
        ]

    # ------------------------------------------------------------------
    # CorePosition integration (Hybrid mode)
    # ------------------------------------------------------------------

    def attach_core_position(self, core_position: CorePosition) -> None:  # type: ignore[name-defined]  # noqa: F821
        """
        Attach a shared CorePosition for Hybrid-mode coordination.

        Once attached, every DCA fill is mirrored into *core_position* so
        GridEngine can read it and skip conflicting orders.

        Pass ``None`` to detach and revert to standalone DCA behaviour.

        Args:
            core_position: Shared position object created by HybridCoordinator.
        """
        self._core_position = core_position

    def detach_core_position(self) -> None:
        """Detach the shared CorePosition (standalone DCA behaviour)."""
        self._core_position = None

    @property
    def core_position(self) -> Optional[CorePosition]:  # type: ignore[name-defined]  # noqa: F821
        """Read-only access to the attached CorePosition (may be None)."""
        return self._core_position

    def reset(self) -> None:
        """Reset engine state (for testing or reinitialization)"""
        self.position = None
        self.last_buy_price = None
        self.highest_price_since_entry = None
        self.total_dca_steps = 0
        self.total_invested = Decimal("0")
        self.realized_profit = Decimal("0")
        self._long_levels.clear()
        self._short_levels.clear()
        self._last_short_price = None

        logger.info("DCAEngine reset", symbol=self.symbol)
