"""
GridEngine - Grid trading strategy implementation
Handles grid level calculation, order placement, execution handling, and rebalancing
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bot.core.trading_core.core_position import CorePosition

from bot.utils.logger import get_logger

logger = get_logger(__name__)


class GridType(str, Enum):
    """Grid type enumeration"""

    STATIC = "static"
    DYNAMIC = "dynamic"


class GridDirection(str, Enum):
    """Grid direction: LONG accumulates on dips; SHORT accumulates on rallies."""

    LONG = "long"
    SHORT = "short"


class GridMode(str, Enum):
    """Grid operating mode."""

    LONG_ONLY = "long_only"
    SHORT_ONLY = "short_only"
    BIDIRECTIONAL = "bidirectional"  # default: hold LONG and SHORT simultaneously


@dataclass
class GridBiConfig:
    """Configuration for bidirectional (market-maker) grid initialization."""

    lower_bound: Decimal
    upper_bound: Decimal
    current_price: Decimal
    step_pct: Decimal
    long_levels: int
    short_levels: int
    order_size_quote: Decimal


class GridOrder:
    """Represents a grid order"""

    def __init__(
        self,
        level: int,
        price: Decimal,
        amount: Decimal,
        side: str,
        order_id: str | None = None,
        filled: bool = False,
    ):
        self.level = level
        self.price = price
        self.amount = amount
        self.side = side  # "buy" or "sell"
        self.order_id = order_id
        self.filled = filled

    def __repr__(self) -> str:
        return (
            f"GridOrder(level={self.level}, price={self.price}, "
            f"amount={self.amount}, side={self.side}, filled={self.filled})"
        )


class GridEngine:
    """
    Grid trading engine implementation.

    Features:
    - Calculate grid levels between upper and lower price boundaries
    - Initialize grid with buy/sell orders at each level
    - Handle order executions and automatic rebalancing
    - Support both static and dynamic grid modes
    - Track grid state and performance
    """

    def __init__(
        self,
        symbol: str,
        upper_price: Decimal,
        lower_price: Decimal,
        grid_levels: int,
        amount_per_grid: Decimal,
        profit_per_grid: Decimal,
        grid_type: GridType = GridType.STATIC,
        direction: GridDirection = GridDirection.LONG,
        mode: GridMode = GridMode.LONG_ONLY,
    ):
        """
        Initialize Grid Engine.

        Args:
            symbol: Trading pair symbol (e.g., 'BTC/USDT')
            upper_price: Upper price boundary for grid
            lower_price: Lower price boundary for grid
            grid_levels: Number of grid levels
            amount_per_grid: Amount to trade per grid level
            profit_per_grid: Profit percentage per grid (0.01 = 1%)
            grid_type: Type of grid (static or dynamic)
            direction: LONG (buy dips / sell rallies) or SHORT (sell rallies / buy dips)
            mode: GridMode controlling unidirectional vs bidirectional operation
        """
        if upper_price <= lower_price:
            raise ValueError("upper_price must be greater than lower_price")
        if grid_levels < 2:
            raise ValueError("grid_levels must be at least 2")
        if amount_per_grid <= 0:
            raise ValueError("amount_per_grid must be positive")
        if profit_per_grid <= 0:
            raise ValueError("profit_per_grid must be positive")

        self.symbol = symbol
        self.upper_price = upper_price
        self.lower_price = lower_price
        self.grid_levels = grid_levels
        self.amount_per_grid = amount_per_grid
        self.profit_per_grid = profit_per_grid
        self.grid_type = grid_type
        self.direction = direction
        self.mode = mode

        # Grid state
        self.grid_orders: list[GridOrder] = []
        self.active_orders: dict[str, GridOrder] = {}  # order_id -> GridOrder
        self.filled_orders: list[GridOrder] = []

        # Position trackers.
        # LONG: filled buy orders awaiting a paired sell (TP).
        # SHORT: filled sell orders awaiting a paired buy (TP).
        self._open_buys: dict[str, GridOrder] = {}  # order_id -> filled buy GridOrder
        self._open_sells: dict[str, GridOrder] = {}  # order_id -> filled sell GridOrder (SHORT)

        # Bidirectional order stacks (BIDIRECTIONAL mode).
        # _long_orders: pending/active LONG-side orders (buy below price + their TP sells).
        # _short_orders: pending/active SHORT-side orders (sell above price + their TP buys).
        self._long_orders: dict[str, GridOrder] = {}
        self._short_orders: dict[str, GridOrder] = {}

        # Statistics
        self.total_profit = Decimal("0")
        self.buy_count = 0
        self.sell_count = 0

        logger.info(
            "GridEngine initialized",
            symbol=symbol,
            upper_price=float(upper_price),
            lower_price=float(lower_price),
            grid_levels=grid_levels,
            grid_type=grid_type,
            direction=direction,
            mode=mode,
        )

    def calculate_grid_levels(self) -> list[Decimal]:
        """
        Calculate grid price levels.

        Returns:
            List of price levels from lower to upper boundary
        """
        price_range = self.upper_price - self.lower_price
        price_step = price_range / (self.grid_levels - 1)

        levels = [self.lower_price + (price_step * i) for i in range(self.grid_levels)]

        logger.debug(
            "Grid levels calculated",
            levels_count=len(levels),
            price_step=float(price_step),
        )

        return levels

    def initialize_grid(
        self,
        current_price: Decimal,
        config: Optional[GridBiConfig] = None,
    ) -> list[GridOrder]:
        """
        Initialize grid orders based on current price.

        When *config* is provided (or ``self.mode == GridMode.BIDIRECTIONAL``),
        the grid is initialized in bidirectional (market-maker) mode:
        - LONG levels are placed **below** current price (buy orders).
        - SHORT levels are placed **above** current price (sell orders).
        Both stacks are active simultaneously.

        Without a config and in LONG_ONLY/SHORT_ONLY mode the legacy
        unidirectional behaviour is preserved.

        Args:
            current_price: Current market price
            config: Optional ``GridBiConfig``; triggers BIDIRECTIONAL mode when supplied.

        Returns:
            List of GridOrder objects to be placed
        """
        self.grid_orders.clear()
        self._long_orders.clear()
        self._short_orders.clear()

        if config is not None or self.mode == GridMode.BIDIRECTIONAL:
            return self._initialize_bidirectional(current_price, config)

        # ── Legacy unidirectional path ────────────────────────────────────
        levels = self.calculate_grid_levels()
        orders_to_place = []

        for level_idx, price in enumerate(levels):
            # Convert amount from quote currency (USD) to base currency
            base_amount = (self.amount_per_grid / price).quantize(Decimal("0.000001"))

            if self.direction == GridDirection.LONG:
                # LONG: buy below current price, sell above
                if price < current_price:
                    order = GridOrder(level=level_idx, price=price, amount=base_amount, side="buy")
                    self.grid_orders.append(order)
                    orders_to_place.append(order)
                elif price > current_price:
                    sell_price = price * (Decimal("1") + self.profit_per_grid)
                    sell_amount = (self.amount_per_grid / sell_price).quantize(Decimal("0.000001"))
                    order = GridOrder(
                        level=level_idx, price=sell_price, amount=sell_amount, side="sell"
                    )
                    self.grid_orders.append(order)
                    orders_to_place.append(order)
            else:
                # SHORT: sell above current price (open short), buy below (TP)
                if price > current_price:
                    order = GridOrder(level=level_idx, price=price, amount=base_amount, side="sell")
                    self.grid_orders.append(order)
                    orders_to_place.append(order)
                elif price < current_price:
                    buy_price = price * (Decimal("1") - self.profit_per_grid)
                    buy_amount = (self.amount_per_grid / buy_price).quantize(Decimal("0.000001"))
                    order = GridOrder(
                        level=level_idx, price=buy_price, amount=buy_amount, side="buy"
                    )
                    self.grid_orders.append(order)
                    orders_to_place.append(order)

        logger.info(
            "Grid initialized",
            total_orders=len(orders_to_place),
            buy_orders=sum(1 for o in orders_to_place if o.side == "buy"),
            sell_orders=sum(1 for o in orders_to_place if o.side == "sell"),
            current_price=float(current_price),
        )

        return orders_to_place

    def _initialize_bidirectional(
        self,
        current_price: Decimal,
        config: Optional[GridBiConfig] = None,
    ) -> list[GridOrder]:
        """
        Build both LONG and SHORT order stacks simultaneously.

        LONG stack: ``config.long_levels`` buy orders spaced by ``step_pct``
        below ``current_price`` (down to ``lower_bound``).
        SHORT stack: ``config.short_levels`` sell orders spaced by ``step_pct``
        above ``current_price`` (up to ``upper_bound``).

        When *config* is None, falls back to the engine's own parameters:
        half the ``grid_levels`` go below (LONG) and half above (SHORT).
        """
        orders_to_place: list[GridOrder] = []

        if config is not None:
            step_pct = config.step_pct
            order_size = config.order_size_quote
            long_levels = config.long_levels
            short_levels = config.short_levels
        else:
            # Derive from engine's existing parameters
            step_pct = self.profit_per_grid
            order_size = self.amount_per_grid
            half = max(1, self.grid_levels // 2)
            long_levels = half
            short_levels = self.grid_levels - half

        # ── LONG stack: buy orders below current price ────────────────────
        for i in range(1, long_levels + 1):
            price = current_price * (Decimal("1") - step_pct * i)
            if config is not None and price < config.lower_bound:
                break
            base_amount = (order_size / price).quantize(Decimal("0.000001"))
            order = GridOrder(level=i, price=price, amount=base_amount, side="buy")
            self.grid_orders.append(order)
            orders_to_place.append(order)
            # Track in LONG stack (will be registered by caller via register_order)
            # We use a temporary key; will be overwritten when exchange order_id arrives.
            self._long_orders[f"_bi_long_{i}"] = order

        # ── SHORT stack: sell orders above current price ──────────────────
        for i in range(1, short_levels + 1):
            price = current_price * (Decimal("1") + step_pct * i)
            if config is not None and price > config.upper_bound:
                break
            base_amount = (order_size / price).quantize(Decimal("0.000001"))
            order = GridOrder(level=i, price=price, amount=base_amount, side="sell")
            self.grid_orders.append(order)
            orders_to_place.append(order)
            self._short_orders[f"_bi_short_{i}"] = order

        logger.info(
            "Grid initialized (bidirectional)",
            total_orders=len(orders_to_place),
            long_orders=long_levels,
            short_orders=short_levels,
            current_price=float(current_price),
        )

        return orders_to_place

    def register_order(self, order: GridOrder, order_id: str) -> None:
        """
        Register an order ID for a grid order.

        Args:
            order: GridOrder object
            order_id: Exchange order ID
        """
        order.order_id = order_id
        self.active_orders[order_id] = order

        logger.debug(
            "Order registered",
            order_id=order_id,
            level=order.level,
            side=order.side,
            price=float(order.price),
        )

    def handle_order_filled(
        self, order_id: str, filled_price: Decimal, filled_amount: Decimal
    ) -> GridOrder | None:
        """
        Handle an order being filled and calculate rebalancing order.

        Args:
            order_id: Exchange order ID that was filled
            filled_price: Price at which order was filled
            filled_amount: Amount that was filled

        Returns:
            New GridOrder to place for rebalancing, or None
        """
        if order_id not in self.active_orders:
            logger.warning("Order not found in active orders", order_id=order_id)
            return None

        filled_order = self.active_orders.pop(order_id)
        filled_order.filled = True
        self.filled_orders.append(filled_order)

        # Update statistics and position tracker
        if self.direction == GridDirection.LONG:
            if filled_order.side == "buy":
                self.buy_count += 1
                self._open_buys[order_id] = filled_order
            else:
                self.sell_count += 1
                profit = (filled_price - filled_order.price) * filled_amount
                self.total_profit += profit
                # Remove paired open buy (same grid level)
                paired_id = next(
                    (bid for bid, bo in self._open_buys.items() if bo.level == filled_order.level),
                    None,
                )
                if paired_id:
                    self._open_buys.pop(paired_id, None)
        else:
            # SHORT direction
            if filled_order.side == "sell":
                self.sell_count += 1
                self._open_sells[order_id] = filled_order
            else:
                self.buy_count += 1
                # TP buy closes a short: profit = sell_price - buy_price
                paired_id = next(
                    (sid for sid, so in self._open_sells.items() if so.level == filled_order.level),
                    None,
                )
                if paired_id:
                    sell_order = self._open_sells.pop(paired_id)
                    profit = (sell_order.price - filled_price) * filled_amount
                    self.total_profit += profit

        logger.info(
            "Order filled",
            order_id=order_id,
            side=filled_order.side,
            level=filled_order.level,
            price=float(filled_price),
            amount=float(filled_amount),
        )

        # Create rebalancing order
        rebalance_order = self._create_rebalance_order(filled_order, filled_price)

        return rebalance_order

    def _create_rebalance_order(self, filled_order: GridOrder, filled_price: Decimal) -> GridOrder:
        """
        Create a rebalancing order after a fill.

        Args:
            filled_order: The order that was filled
            filled_price: Price at which it was filled

        Returns:
            New GridOrder for rebalancing
        """
        if self.direction == GridDirection.LONG:
            if filled_order.side == "buy":
                # After buying long, place TP sell above
                new_price = filled_price * (Decimal("1") + self.profit_per_grid)
                new_side = "sell"
            else:
                # After selling TP, re-enter buy below
                new_price = filled_price * (Decimal("1") - self.profit_per_grid)
                new_side = "buy"
        else:
            if filled_order.side == "sell":
                # After selling short, place TP buy below
                new_price = filled_price * (Decimal("1") - self.profit_per_grid)
                new_side = "buy"
            else:
                # After TP buy, re-enter sell above
                new_price = filled_price * (Decimal("1") + self.profit_per_grid)
                new_side = "sell"

        # Convert amount from quote currency (USD) to base currency (BTC)
        base_amount = (self.amount_per_grid / new_price).quantize(Decimal("0.000001"))

        rebalance_order = GridOrder(
            level=filled_order.level,
            price=new_price,
            amount=base_amount,
            side=new_side,
        )

        logger.debug(
            "Rebalance order created",
            original_side=filled_order.side,
            new_side=new_side,
            new_price=float(new_price),
        )

        return rebalance_order

    def update_grid_bounds(
        self, new_upper: Decimal, new_lower: Decimal, current_price: Decimal
    ) -> tuple[list[str], list[GridOrder]]:
        """
        Update grid boundaries for dynamic grid mode.

        Args:
            new_upper: New upper price boundary
            new_lower: New lower price boundary
            current_price: Current market price

        Returns:
            Tuple of (order_ids_to_cancel, new_orders_to_place)
        """
        if self.grid_type != GridType.DYNAMIC:
            logger.warning("Cannot update bounds for static grid")
            return ([], [])

        logger.info(
            "Updating grid bounds",
            old_upper=float(self.upper_price),
            old_lower=float(self.lower_price),
            new_upper=float(new_upper),
            new_lower=float(new_lower),
        )

        # Cancel all active orders
        orders_to_cancel = list(self.active_orders.keys())

        # Update boundaries
        self.upper_price = new_upper
        self.lower_price = new_lower

        # Reinitialize grid
        new_orders = self.initialize_grid(current_price)

        return (orders_to_cancel, new_orders)

    def set_direction(self, new_direction: GridDirection) -> list[str]:
        """Switch grid direction.  Returns all active order_ids to cancel on exchange.

        .. deprecated::
            Use :meth:`reconfigure` instead for bidirectional grids.  ``set_direction``
            will be removed in a future version.

        Caller is responsible for:
        1. Cancelling returned order IDs on the exchange.
        2. Calling initialize_grid(current_price) to rebuild grid in new direction.
        """
        if new_direction == self.direction:
            return []

        warnings.warn(
            "set_direction() is deprecated and will be removed in a future version. "
            "Use reconfigure(new_config) instead.",
            DeprecationWarning,
            stacklevel=2,
        )

        logger.info(
            "grid_direction_switch",
            symbol=self.symbol,
            old=self.direction,
            new=new_direction,
        )

        order_ids_to_cancel = list(self.active_orders.keys())

        # Reset all state
        self.direction = new_direction
        self.grid_orders.clear()
        self.active_orders.clear()
        self.filled_orders.clear()
        self._open_buys.clear()
        self._open_sells.clear()
        self._long_orders.clear()
        self._short_orders.clear()

        return order_ids_to_cancel

    # ------------------------------------------------------------------
    # Bidirectional helpers
    # ------------------------------------------------------------------

    def get_combined_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Return the combined unrealised PnL across both LONG and SHORT stacks.

        Calculation:
        - LONG stack: each filled buy whose TP sell hasn't triggered yet contributes
          ``(current_price - entry_price) * amount``.  Negative when price < entry.
        - SHORT stack: each filled sell whose TP buy hasn't triggered yet contributes
          ``(entry_price - current_price) * amount``.  Negative when price > entry.

        For unidirectional mode the result is the same as computing PnL from the
        single open-position tracker (``_open_buys`` / ``_open_sells``).
        """
        pnl = Decimal("0")

        # LONG positions: open buys waiting for TP sell
        for order in self._open_buys.values():
            pnl += (current_price - order.price) * order.amount

        # SHORT positions: open sells waiting for TP buy
        for order in self._open_sells.values():
            pnl += (order.price - current_price) * order.amount

        return pnl

    def close_direction(self, direction: GridDirection) -> list[str]:
        """Close one stack and return order_ids to cancel on the exchange.

        In BIDIRECTIONAL mode this lets the caller shut down the LONG or SHORT
        stack independently while leaving the other stack running.

        Behaviour:
        - Collects all ``active_orders`` belonging to the chosen *direction*.
        - Removes them from ``active_orders``, ``_open_buys``/``_open_sells``,
          and the matching ``_long_orders``/``_short_orders`` registry.
        - Returns the list of order IDs for the caller to cancel on the exchange.

        Args:
            direction: ``GridDirection.LONG`` or ``GridDirection.SHORT``.

        Returns:
            List of order IDs that must be cancelled on the exchange.
        """
        ids_to_cancel: list[str] = []

        if direction == GridDirection.LONG:
            # Cancel active buy orders (and any paired TP sells from LONG stack)
            target_sides = {"buy"}
            # Also cancel sell orders that are TP for LONG (tracked in _long_orders)
            long_order_ids = {
                o.order_id for o in self._long_orders.values() if o.order_id is not None
            }
            for order_id, order in list(self.active_orders.items()):
                if order.side in target_sides or order_id in long_order_ids:
                    ids_to_cancel.append(order_id)
                    self.active_orders.pop(order_id)
                    self._open_buys.pop(order_id, None)
            self._long_orders.clear()
        else:
            # Cancel active sell orders (and any paired TP buys from SHORT stack)
            target_sides = {"sell"}
            short_order_ids = {
                o.order_id for o in self._short_orders.values() if o.order_id is not None
            }
            for order_id, order in list(self.active_orders.items()):
                if order.side in target_sides or order_id in short_order_ids:
                    ids_to_cancel.append(order_id)
                    self.active_orders.pop(order_id)
                    self._open_sells.pop(order_id, None)
            self._short_orders.clear()

        logger.info(
            "grid_close_direction",
            symbol=self.symbol,
            direction=direction,
            cancelled_count=len(ids_to_cancel),
        )

        return ids_to_cancel

    def reconfigure(self, new_config: GridBiConfig) -> list[str]:
        """Close all active orders and reinitialise the grid with *new_config*.

        Replaces ``set_direction()`` for bidirectional grids.

        Steps:
        1. Collect all active order IDs to cancel on the exchange.
        2. Reset full grid state (orders, positions, statistics).
        3. Call ``_initialize_bidirectional`` with the new config.

        Args:
            new_config: New ``GridBiConfig`` describing the desired grid layout.

        Returns:
            List of order IDs that must be cancelled on the exchange before
            the new orders (returned by a subsequent ``initialize_grid`` call)
            can be placed.
        """
        ids_to_cancel = list(self.active_orders.keys())

        # Reset all state
        self.grid_orders.clear()
        self.active_orders.clear()
        self.filled_orders.clear()
        self._open_buys.clear()
        self._open_sells.clear()
        self._long_orders.clear()
        self._short_orders.clear()
        self.total_profit = Decimal("0")
        self.buy_count = 0
        self.sell_count = 0

        # Update bounds from config
        self.upper_price = new_config.upper_bound
        self.lower_price = new_config.lower_bound
        self.mode = GridMode.BIDIRECTIONAL

        # Reinitialise with new config
        self._initialize_bidirectional(new_config.current_price, new_config)

        logger.info(
            "grid_reconfigured",
            symbol=self.symbol,
            cancelled_count=len(ids_to_cancel),
            lower_bound=float(new_config.lower_bound),
            upper_bound=float(new_config.upper_bound),
            current_price=float(new_config.current_price),
        )

        return ids_to_cancel

    def get_grid_status(self) -> dict:
        """
        Get current grid status and statistics.

        Returns:
            Dictionary with grid status information
        """
        return {
            "symbol": self.symbol,
            "grid_type": self.grid_type,
            "direction": self.direction,
            "upper_price": float(self.upper_price),
            "lower_price": float(self.lower_price),
            "grid_levels": self.grid_levels,
            "active_orders": len(self.active_orders),
            "filled_orders": len(self.filled_orders),
            "total_profit": float(self.total_profit),
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
        }

    def cancel_order(self, order_id: str) -> bool:
        """
        Mark an order as cancelled.

        Args:
            order_id: Exchange order ID to cancel

        Returns:
            True if order was found and cancelled, False otherwise
        """
        if order_id in self.active_orders:
            order = self.active_orders.pop(order_id)
            self._open_buys.pop(order_id, None)
            self._open_sells.pop(order_id, None)
            logger.info(
                "Order cancelled",
                order_id=order_id,
                level=order.level,
                side=order.side,
            )
            return True

        logger.warning("Order not found for cancellation", order_id=order_id)
        return False

    def get_underwater_positions(self, current_price: Decimal) -> list[dict]:
        """Return positions that are currently losing.

        LONG: filled buys where entry_price > current_price (loss if closed now).
        SHORT: filled sells where entry_price < current_price (price moved against short).

        No exchange queries — uses local position trackers.
        """
        if self.direction == GridDirection.LONG:
            return [
                {"order_id": oid, "entry_price": o.price, "size": o.amount}
                for oid, o in self._open_buys.items()
                if o.price > current_price
            ]
        else:
            return [
                {"order_id": oid, "entry_price": o.price, "size": o.amount}
                for oid, o in self._open_sells.items()
                if o.price < current_price
            ]

    def get_open_positions(self) -> list[dict]:
        """
        Return open buy-side positions in the format expected by
        PortfolioRiskManager for per-symbol exposure aggregation.

        Each dict contains:
          - 'symbol': str
          - 'size': Decimal  (notional value: price × amount)
          - 'atr_stop_distance': Decimal  (profit_per_grid as fractional proxy for stop)
        """
        result = []
        for order in self.active_orders.values():
            if order.side == "buy":
                notional = order.price * order.amount
                result.append(
                    {
                        "symbol": self.symbol,
                        "size": notional,
                        "atr_stop_distance": self.profit_per_grid,
                    }
                )
        return result

    # ------------------------------------------------------------------
    # CorePosition coordination (Hybrid mode)
    # ------------------------------------------------------------------

    def reconcile_grid_orders(self, vpm_positions: list) -> list[str]:
        """
        Find SELL (TP) orders in the active grid that have no paired VPM position.

        Called on bot start and after each recenter to clean up orphaned SELL
        orders whose corresponding BUY fill was never registered in VPM.

        Args:
            vpm_positions: List of VirtualPosition objects currently open
                           for the grid strategy on this symbol.

        Returns:
            List of order_ids for SELL orders that have no matching VPM position
            and should be cancelled on the exchange.
        """
        # Collect grid levels that have a VPM position (entry_price as proxy)
        vpm_entry_prices = {pos.entry_price for pos in vpm_positions}

        orphan_order_ids: list[str] = []
        for order_id, order in list(self.active_orders.items()):
            if order.side != "sell":
                continue
            # A SELL order is "paired" if there is a VPM position whose entry
            # is within one profit_per_grid step of the SELL order price
            # (buy price ≈ sell_price / (1 + profit_per_grid)).
            implied_buy_price = order.price / (Decimal("1") + self.profit_per_grid)
            has_pair = any(
                abs(entry - implied_buy_price) / implied_buy_price <= self.profit_per_grid
                for entry in vpm_entry_prices
            )
            if not has_pair:
                orphan_order_ids.append(order_id)
                logger.info(
                    "grid_orphan_sell_order",
                    symbol=self.symbol,
                    order_id=order_id,
                    sell_price=float(order.price),
                    implied_buy_price=float(implied_buy_price),
                )

        return orphan_order_ids

    def apply_position_constraints(
        self,
        core_position: Optional[CorePosition],
        *,
        dca_protection_levels: int = 2,
        max_sell_ratio: Decimal = Decimal("0.5"),
    ) -> dict:
        """
        Adapt the active grid to avoid conflicting with a DCA CorePosition.

        Rules applied:
        1. **Accumulation zone** — SELL orders whose price falls within
           ``dca_protection_levels`` trigger-% steps below
           ``avg_entry_price`` are cancelled (Grid must not lock profits
           before DCA finishes accumulating).
        2. **Total SELL cap** — sum of all active SELL amounts must not
           exceed ``max_sell_ratio`` × ``core_position.total_amount``.
           Excess SELL orders (cheapest first) are cancelled.
        3. **Safety-order conflict** — when ``core_position.safety_order_active``
           is True, BUY orders within ±1 grid level of the DCA fill price
           (``avg_entry_price``) are cancelled to avoid double-buying.
           The flag is then cleared on ``core_position``.

        When *core_position* is ``None`` or empty, this method is a no-op
        and returns an empty constraint report — full backward compatibility.

        Args:
            core_position: Shared CorePosition from HybridCoordinator.
                If ``None``, no constraints are applied.
            dca_protection_levels: How many DCA trigger steps below avg_entry
                to protect from Grid SELL orders.  Default 2.
            max_sell_ratio: Maximum fraction of CorePosition size that Grid
                may place as SELL orders simultaneously.  Default 0.5 (50 %).

        Returns:
            A dict summarising what was constrained::

                {
                    "sell_cancelled_accumulation_zone": [order_ids],
                    "sell_cancelled_cap": [order_ids],
                    "buy_cancelled_safety_conflict": [order_ids],
                }
        """
        report: dict[str, list[str]] = {
            "sell_cancelled_accumulation_zone": [],
            "sell_cancelled_cap": [],
            "buy_cancelled_safety_conflict": [],
        }

        if core_position is None or core_position.is_empty():
            return report

        avg = core_position.avg_entry_price

        # -----------------------------------------------------------------
        # Rule 1: No SELL orders in DCA accumulation zone
        # (below avg_entry by up to dca_protection_levels × profit_per_grid)
        # -----------------------------------------------------------------
        protection_band = avg * self.profit_per_grid * dca_protection_levels
        accumulation_ceiling = avg - protection_band

        sell_orders_by_price = sorted(
            [o for o in self.active_orders.values() if o.side == "sell"],
            key=lambda o: o.price,
        )

        for order in sell_orders_by_price:
            if order.price <= avg and order.price >= accumulation_ceiling:
                # SELL in the protected DCA zone — cancel it
                if order.order_id is not None:
                    self.cancel_order(order.order_id)
                    report["sell_cancelled_accumulation_zone"].append(
                        order.order_id or f"level-{order.level}"
                    )
                    logger.info(
                        "grid_sell_cancelled_dca_zone",
                        symbol=self.symbol,
                        price=float(order.price),
                        avg_entry=float(avg),
                    )

        # -----------------------------------------------------------------
        # Rule 2: Cap total SELL volume at max_sell_ratio × CorePosition size
        # -----------------------------------------------------------------
        max_sell_amount = core_position.total_amount * max_sell_ratio

        # Collect remaining active SELLs (after rule-1 removals)
        remaining_sells = sorted(
            [o for o in self.active_orders.values() if o.side == "sell"],
            key=lambda o: o.price,  # cancel cheapest (closest to market) first
        )

        cumulative_sell = Decimal("0")
        for order in remaining_sells:
            cumulative_sell += order.amount
            if cumulative_sell > max_sell_amount:
                # This order pushes us over the cap — cancel it
                if order.order_id is not None:
                    self.cancel_order(order.order_id)
                    report["sell_cancelled_cap"].append(order.order_id or f"level-{order.level}")
                    logger.info(
                        "grid_sell_cancelled_cap",
                        symbol=self.symbol,
                        price=float(order.price),
                        cumulative_sell=float(cumulative_sell),
                        max_sell_amount=float(max_sell_amount),
                    )

        # -----------------------------------------------------------------
        # Rule 3: Remove conflicting BUY orders during active DCA safety order
        # -----------------------------------------------------------------
        if core_position.safety_order_active:
            fill_price = avg  # best proxy for the just-completed DCA fill
            # Cancel BUY orders within ±1 profit_per_grid of the DCA fill price
            conflict_band = fill_price * self.profit_per_grid

            for order in list(self.active_orders.values()):
                if order.side == "buy" and abs(order.price - fill_price) <= conflict_band:
                    if order.order_id is not None:
                        self.cancel_order(order.order_id)
                        report["buy_cancelled_safety_conflict"].append(
                            order.order_id or f"level-{order.level}"
                        )
                        logger.info(
                            "grid_buy_cancelled_safety_conflict",
                            symbol=self.symbol,
                            price=float(order.price),
                            fill_price=float(fill_price),
                        )

            core_position.clear_safety_order_flag()

        return report
