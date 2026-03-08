"""
GridEngine - Grid trading strategy implementation
Handles grid level calculation, order placement, execution handling, and rebalancing
"""

from __future__ import annotations

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

        # Grid state
        self.grid_orders: list[GridOrder] = []
        self.active_orders: dict[str, GridOrder] = {}  # order_id -> GridOrder
        self.filled_orders: list[GridOrder] = []

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

    def initialize_grid(self, current_price: Decimal) -> list[GridOrder]:
        """
        Initialize grid orders based on current price.

        Creates buy orders below current price and sell orders above.

        Args:
            current_price: Current market price

        Returns:
            List of GridOrder objects to be placed
        """
        self.grid_orders.clear()
        levels = self.calculate_grid_levels()

        orders_to_place = []

        for level_idx, price in enumerate(levels):
            # Convert amount from quote currency (USD) to base currency (BTC)
            # amount_per_grid is in USDT, but exchange expects BTC quantity
            base_amount = (self.amount_per_grid / price).quantize(Decimal("0.000001"))

            # Create buy orders below current price
            if price < current_price:
                order = GridOrder(
                    level=level_idx,
                    price=price,
                    amount=base_amount,
                    side="buy",
                )
                self.grid_orders.append(order)
                orders_to_place.append(order)

            # Create sell orders above current price
            elif price > current_price:
                # Calculate sell price with profit margin
                sell_price = price * (Decimal("1") + self.profit_per_grid)
                # Convert amount at sell price
                sell_base_amount = (self.amount_per_grid / sell_price).quantize(Decimal("0.000001"))
                order = GridOrder(
                    level=level_idx,
                    price=sell_price,
                    amount=sell_base_amount,
                    side="sell",
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

        # Update statistics
        if filled_order.side == "buy":
            self.buy_count += 1
        else:
            self.sell_count += 1
            # Calculate profit for sell orders
            profit = (filled_price - filled_order.price) * filled_amount
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
        if filled_order.side == "buy":
            # After buying, place a sell order above
            new_price = filled_price * (Decimal("1") + self.profit_per_grid)
            new_side = "sell"
        else:
            # After selling, place a buy order below
            new_price = filled_price * (Decimal("1") - self.profit_per_grid)
            new_side = "buy"

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

    def get_grid_status(self) -> dict:
        """
        Get current grid status and statistics.

        Returns:
            Dictionary with grid status information
        """
        return {
            "symbol": self.symbol,
            "grid_type": self.grid_type,
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
            logger.info(
                "Order cancelled",
                order_id=order_id,
                level=order.level,
                side=order.side,
            )
            return True

        logger.warning("Order not found for cancellation", order_id=order_id)
        return False

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
