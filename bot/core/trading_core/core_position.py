"""
CorePosition — unified base position shared between DCA and Grid strategies.

In Hybrid mode, DCA creates and owns a CorePosition. GridEngine reads it
to avoid placing conflicting orders (SELL in DCA accumulation zone, BUY
during active safety orders, etc.).

Backward compatibility: if CorePosition is None, Grid and DCA work as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CorePosition:
    """
    Unified base position managed by DCA and observed by Grid.

    DCA updates this position on each safety order fill.
    Grid reads it to adapt the order grid — avoiding SELL orders in the
    DCA accumulation zone and limiting total SELL volume above avg entry.

    Args:
        symbol: Trading pair symbol (e.g., 'BTC/USDT').
        side: Position direction, 'long' or 'short'.
        total_amount: Accumulated position size in base currency.
        avg_entry_price: Volume-weighted average entry price.
        dca_steps_taken: Number of DCA fills (safety orders) completed.
        target_exit_price: Optional TP price from SMC key levels.  When
            provided by StrategyConductor, Grid uses it as an upper bound
            for SELL orders rather than its own profit_per_grid projection.
        safety_order_active: True when a DCA safety order was *just* filled
            and Grid should remove conflicting BUY orders one level around
            the fill price.
    """

    symbol: str
    side: str  # "long" or "short"
    total_amount: Decimal = field(default=Decimal("0"))
    avg_entry_price: Decimal = field(default=Decimal("0"))
    dca_steps_taken: int = 0
    target_exit_price: Optional[Decimal] = None
    safety_order_active: bool = False

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def update_from_fill(self, price: Decimal, amount: Decimal) -> None:
        """
        Record a new DCA fill (base order or safety order).

        Recalculates the volume-weighted average entry price and increments
        the step counter.  Sets *safety_order_active* so Grid knows to
        temporarily remove conflicting orders.

        Args:
            price: Fill price.
            amount: Filled amount in base currency.
        """
        if price <= 0 or amount <= 0:
            raise ValueError(f"price and amount must be positive, got price={price} amount={amount}")

        old_cost = self.avg_entry_price * self.total_amount
        new_cost = price * amount
        self.total_amount += amount
        self.avg_entry_price = (old_cost + new_cost) / self.total_amount
        self.dca_steps_taken += 1
        self.safety_order_active = True

        logger.debug(
            "core_position_updated",
            symbol=self.symbol,
            price=float(price),
            amount=float(amount),
            new_avg=float(self.avg_entry_price),
            total_amount=float(self.total_amount),
            steps=self.dca_steps_taken,
        )

    def clear_safety_order_flag(self) -> None:
        """Clear the *safety_order_active* flag after Grid has reacted."""
        self.safety_order_active = False

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    def get_breakeven(self) -> Decimal:
        """
        Return the breakeven price (== avg_entry_price for long positions).

        For 'short' positions the semantics are reversed, but this
        implementation currently focuses on long accumulation.
        """
        return self.avg_entry_price

    def is_empty(self) -> bool:
        """True when no position has been accumulated yet."""
        return self.total_amount == Decimal("0")

    def __repr__(self) -> str:
        return (
            f"CorePosition(symbol={self.symbol!r}, side={self.side!r}, "
            f"total_amount={self.total_amount}, avg_entry={self.avg_entry_price}, "
            f"steps={self.dca_steps_taken})"
        )
