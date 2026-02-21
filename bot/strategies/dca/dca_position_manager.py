"""
DCA Position Manager — v2.0.

Manages DCA deal lifecycle:
- Open deal with base order
- Calculate and track safety orders (volume multiplier + price step)
- Track average entry price across all fills
- Calculate take-profit targets
- Close deal with profit/loss accounting

Exchange-agnostic: all exchange operations are performed by the caller.
This module provides pure calculation and state management.

Usage:
    config = DCAOrderConfig(base_order_volume=Decimal("100"), ...)
    manager = DCAPositionManager(symbol="BTC/USDT", config=config)
    deal = manager.open_deal(entry_price=Decimal("3100"))
    safety_orders = manager.get_safety_orders(deal.id)
    # When price drops to SO level:
    manager.fill_safety_order(deal.id, level=1, fill_price=Decimal("3038"))
    # When exiting:
    result = manager.close_deal(deal.id, exit_price=Decimal("3300"), reason="take_profit")
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Any

# =============================================================================
# Enums
# =============================================================================


class DealStatus(str, Enum):
    """Status of a DCA deal."""

    ACTIVE = "active"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class DCAOrderType(str, Enum):
    """Type of DCA order."""

    BASE_ORDER = "base_order"
    SAFETY_ORDER = "safety_order"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    STOP_LOSS = "stop_loss"


class DCAOrderStatus(str, Enum):
    """Status of an individual DCA order."""

    PENDING = "pending"
    PLACED = "placed"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class SafetyOrderLevel:
    """Pre-calculated safety order configuration for a specific level."""

    level: int
    price: Decimal  # Trigger price
    volume: Decimal  # Asset volume to buy
    cost: Decimal  # Quote currency cost (volume * price)
    price_deviation_pct: Decimal  # Total % deviation from base order price


@dataclass
class DCAOrder:
    """Record of a single order within a deal."""

    id: str
    deal_id: str
    order_type: DCAOrderType
    side: str  # "buy" or "sell"
    price: Decimal
    volume: Decimal
    cost: Decimal
    status: DCAOrderStatus = DCAOrderStatus.PENDING
    exchange_order_id: str | None = None
    filled_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DCADeal:
    """
    Full state of a DCA deal.

    Tracks base order, safety orders, average entry, and profit.
    """

    id: str
    symbol: str
    status: DealStatus = DealStatus.ACTIVE

    # Base order
    base_order_price: Decimal = Decimal("0")
    base_order_volume: Decimal = Decimal("0")
    base_order_cost: Decimal = Decimal("0")

    # Aggregated position
    average_entry_price: Decimal = Decimal("0")
    total_volume: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")

    # Safety orders
    safety_orders_filled: int = 0
    max_safety_orders: int = 0
    next_safety_order_price: Decimal | None = None

    # Trailing stop tracking
    highest_price_since_entry: Decimal = Decimal("0")
    trailing_stop_activated: bool = False
    trailing_activation_price: Decimal | None = None

    # PnL
    current_profit_pct: Decimal = Decimal("0")
    realized_profit: Decimal = Decimal("0")
    realized_profit_pct: Decimal = Decimal("0")
    close_reason: str | None = None

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "status": self.status.value,
            "base_order_price": str(self.base_order_price),
            "average_entry_price": str(self.average_entry_price),
            "total_volume": str(self.total_volume),
            "total_cost": str(self.total_cost),
            "safety_orders_filled": self.safety_orders_filled,
            "max_safety_orders": self.max_safety_orders,
            "highest_price_since_entry": str(self.highest_price_since_entry),
            "trailing_stop_activated": self.trailing_stop_activated,
            "current_profit_pct": str(self.current_profit_pct),
            "realized_profit": str(self.realized_profit),
            "close_reason": self.close_reason,
        }


@dataclass
class CloseResult:
    """Result of closing a DCA deal."""

    deal_id: str
    exit_price: Decimal
    reason: str
    realized_profit: Decimal
    realized_profit_pct: Decimal
    total_volume_sold: Decimal
    sell_value: Decimal


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class DCAOrderConfig:
    """
    Configuration for DCA deal orders.

    Safety order volume grows exponentially:
        SO_n volume = base_order_volume * volume_multiplier^n
    Safety order price drops linearly:
        SO_n price = base_price * (1 - price_step_pct * n / 100)
    """

    # Base order
    base_order_volume: Decimal = Decimal("100")  # Quote currency (e.g., USDT)

    # Safety orders
    max_safety_orders: int = 5
    volume_multiplier: Decimal = Decimal("1.5")  # Each SO is 1.5x the previous
    price_step_pct: Decimal = Decimal("2.0")  # % drop between safety orders

    # Take profit
    take_profit_pct: Decimal = Decimal("3.0")  # % from average entry

    # Stop loss
    stop_loss_pct: Decimal = Decimal("10.0")  # % from base order price
    stop_loss_from_average: bool = False  # If True, measure from avg entry

    # Max position
    max_position_cost: Decimal = Decimal("5000")  # Maximum total cost in quote

    def validate(self) -> None:
        """Validate configuration values."""
        if self.base_order_volume <= 0:
            raise ValueError("base_order_volume must be positive")
        if self.max_safety_orders < 0:
            raise ValueError("max_safety_orders must be >= 0")
        if self.volume_multiplier <= 0:
            raise ValueError("volume_multiplier must be positive")
        if self.price_step_pct <= 0:
            raise ValueError("price_step_pct must be positive")
        if self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be positive")
        if self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be positive")
        if self.max_position_cost <= 0:
            raise ValueError("max_position_cost must be positive")

    def total_required_capital(self, base_price: Decimal) -> Decimal:
        """
        Calculate total capital needed for base + all safety orders.

        Args:
            base_price: Expected base order price.

        Returns:
            Total quote currency required.
        """
        total = self.base_order_volume
        for level in range(1, self.max_safety_orders + 1):
            so_cost = self.base_order_volume * self.volume_multiplier**level
            total += so_cost
        return total


# =============================================================================
# DCA Position Manager
# =============================================================================


class DCAPositionManager:
    """
    Manages DCA deal positions: base orders, safety orders, and exits.

    All state is maintained in-memory. The caller is responsible for
    exchange interaction and database persistence.
    """

    def __init__(
        self,
        symbol: str,
        config: DCAOrderConfig | None = None,
    ):
        self._symbol = symbol
        self._config = config or DCAOrderConfig()
        self._config.validate()
        self._deals: dict[str, DCADeal] = {}
        self._orders: dict[str, list[DCAOrder]] = {}  # deal_id → orders
        self._deal_counter = 0

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def config(self) -> DCAOrderConfig:
        return self._config

    # -----------------------------------------------------------------
    # Deal Lifecycle
    # -----------------------------------------------------------------

    def open_deal(self, entry_price: Decimal) -> DCADeal:
        """
        Open a new DCA deal with a base order.

        Args:
            entry_price: Price at which the base order was filled.

        Returns:
            New DCADeal instance.
        """
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")

        self._deal_counter += 1
        deal_id = f"DCA-{self._deal_counter:04d}"

        # Calculate base order
        volume = (self._config.base_order_volume / entry_price).quantize(
            Decimal("0.00000001"), rounding=ROUND_DOWN
        )
        cost = volume * entry_price

        deal = DCADeal(
            id=deal_id,
            symbol=self._symbol,
            base_order_price=entry_price,
            base_order_volume=volume,
            base_order_cost=cost,
            average_entry_price=entry_price,
            total_volume=volume,
            total_cost=cost,
            max_safety_orders=self._config.max_safety_orders,
            highest_price_since_entry=entry_price,
        )

        # Calculate next safety order price
        safety_orders = self.calculate_safety_orders(deal)
        if safety_orders:
            deal.next_safety_order_price = safety_orders[0].price

        # Store
        self._deals[deal_id] = deal
        self._orders[deal_id] = []

        # Record base order
        base_order = DCAOrder(
            id=f"{deal_id}-BO",
            deal_id=deal_id,
            order_type=DCAOrderType.BASE_ORDER,
            side="buy",
            price=entry_price,
            volume=volume,
            cost=cost,
            status=DCAOrderStatus.FILLED,
            filled_at=datetime.now(timezone.utc),
        )
        self._orders[deal_id].append(base_order)

        return deal

    def fill_safety_order(
        self,
        deal_id: str,
        level: int,
        fill_price: Decimal,
    ) -> DCADeal:
        """
        Record a safety order fill and update deal state.

        Args:
            deal_id: The deal ID.
            level: Safety order level (1-based).
            fill_price: Actual fill price.

        Returns:
            Updated DCADeal.
        """
        deal = self._get_deal(deal_id)

        if deal.status != DealStatus.ACTIVE:
            raise ValueError(f"Deal {deal_id} is not active")
        if level != deal.safety_orders_filled + 1:
            raise ValueError(f"Expected SO level {deal.safety_orders_filled + 1}, got {level}")
        if level > deal.max_safety_orders:
            raise ValueError(f"Max safety orders ({deal.max_safety_orders}) exceeded")

        # Calculate this SO's volume
        so_cost = self._config.base_order_volume * (self._config.volume_multiplier**level)
        so_volume = (so_cost / fill_price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        actual_cost = so_volume * fill_price

        # Check max position cost
        if deal.total_cost + actual_cost > self._config.max_position_cost:
            raise ValueError(f"Position cost would exceed max ({self._config.max_position_cost})")

        # Update deal
        deal.total_volume += so_volume
        deal.total_cost += actual_cost
        deal.average_entry_price = deal.total_cost / deal.total_volume
        deal.safety_orders_filled = level

        # Update next SO price
        safety_orders = self.calculate_safety_orders(deal)
        if level < len(safety_orders):
            deal.next_safety_order_price = safety_orders[level].price
        else:
            deal.next_safety_order_price = None

        # Record order
        order = DCAOrder(
            id=f"{deal_id}-SO{level}",
            deal_id=deal_id,
            order_type=DCAOrderType.SAFETY_ORDER,
            side="buy",
            price=fill_price,
            volume=so_volume,
            cost=actual_cost,
            status=DCAOrderStatus.FILLED,
            filled_at=datetime.now(timezone.utc),
        )
        self._orders[deal_id].append(order)

        return deal

    def close_deal(
        self,
        deal_id: str,
        exit_price: Decimal,
        reason: str,
    ) -> CloseResult:
        """
        Close a deal at the given exit price.

        Args:
            deal_id: The deal to close.
            exit_price: Price at which position is sold.
            reason: Close reason (e.g. "take_profit", "trailing_stop", "stop_loss").

        Returns:
            CloseResult with profit details.
        """
        deal = self._get_deal(deal_id)

        if deal.status != DealStatus.ACTIVE:
            raise ValueError(f"Deal {deal_id} is not active")
        if exit_price <= 0:
            raise ValueError("exit_price must be positive")

        sell_value = exit_price * deal.total_volume
        profit = sell_value - deal.total_cost
        profit_pct = (profit / deal.total_cost) * 100 if deal.total_cost > 0 else Decimal("0")

        # Update deal
        deal.status = DealStatus.CLOSED
        deal.closed_at = datetime.now(timezone.utc)
        deal.close_reason = reason
        deal.realized_profit = profit
        deal.realized_profit_pct = profit_pct

        # Record sell order
        order = DCAOrder(
            id=f"{deal_id}-EXIT",
            deal_id=deal_id,
            order_type=(
                DCAOrderType(reason)
                if reason in DCAOrderType.__members__.values()
                else DCAOrderType.TAKE_PROFIT
            ),
            side="sell",
            price=exit_price,
            volume=deal.total_volume,
            cost=sell_value,
            status=DCAOrderStatus.FILLED,
            filled_at=datetime.now(timezone.utc),
        )
        self._orders[deal_id].append(order)

        return CloseResult(
            deal_id=deal_id,
            exit_price=exit_price,
            reason=reason,
            realized_profit=profit,
            realized_profit_pct=profit_pct,
            total_volume_sold=deal.total_volume,
            sell_value=sell_value,
        )

    def cancel_deal(self, deal_id: str) -> DCADeal:
        """Cancel an active deal without selling."""
        deal = self._get_deal(deal_id)
        if deal.status != DealStatus.ACTIVE:
            raise ValueError(f"Deal {deal_id} is not active")
        deal.status = DealStatus.CANCELLED
        deal.closed_at = datetime.now(timezone.utc)
        deal.close_reason = "cancelled"
        return deal

    # -----------------------------------------------------------------
    # Safety Order Calculations
    # -----------------------------------------------------------------

    def calculate_safety_orders(self, deal: DCADeal) -> list[SafetyOrderLevel]:
        """
        Calculate all safety order levels for a deal.

        Each SO has:
        - Price: base_price * (1 - price_step_pct * level / 100)
        - Volume: base_volume_cost * volume_multiplier^level / price

        Args:
            deal: The DCA deal.

        Returns:
            List of SafetyOrderLevel for each level.
        """
        cfg = self._config
        result: list[SafetyOrderLevel] = []
        base_price = deal.base_order_price

        for level in range(1, cfg.max_safety_orders + 1):
            deviation_pct = cfg.price_step_pct * level
            so_price = base_price * (1 - deviation_pct / 100)

            if so_price <= 0:
                break

            so_cost = cfg.base_order_volume * (cfg.volume_multiplier**level)
            so_volume = (so_cost / so_price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

            result.append(
                SafetyOrderLevel(
                    level=level,
                    price=so_price,
                    volume=so_volume,
                    cost=so_cost,
                    price_deviation_pct=deviation_pct,
                )
            )

        return result

    def get_take_profit_price(self, deal_id: str) -> Decimal:
        """Calculate take-profit price based on average entry."""
        deal = self._get_deal(deal_id)
        return deal.average_entry_price * (1 + self._config.take_profit_pct / 100)

    def get_stop_loss_price(self, deal_id: str) -> Decimal:
        """Calculate stop-loss price."""
        deal = self._get_deal(deal_id)
        if self._config.stop_loss_from_average:
            ref_price = deal.average_entry_price
        else:
            ref_price = deal.base_order_price
        return ref_price * (1 - self._config.stop_loss_pct / 100)

    def check_safety_order_trigger(
        self, deal_id: str, current_price: Decimal
    ) -> SafetyOrderLevel | None:
        """
        Check if current price triggers the next safety order.

        Returns the SafetyOrderLevel if triggered, else None.
        """
        deal = self._get_deal(deal_id)

        if deal.status != DealStatus.ACTIVE:
            return None
        if deal.safety_orders_filled >= deal.max_safety_orders:
            return None
        if deal.next_safety_order_price is None:
            return None

        if current_price <= deal.next_safety_order_price:
            safety_orders = self.calculate_safety_orders(deal)
            next_level = deal.safety_orders_filled + 1
            if next_level <= len(safety_orders):
                return safety_orders[next_level - 1]

        return None

    # -----------------------------------------------------------------
    # Profit Calculation
    # -----------------------------------------------------------------

    def calculate_current_profit(
        self, deal_id: str, current_price: Decimal
    ) -> tuple[Decimal, Decimal]:
        """
        Calculate unrealized profit for a deal.

        Returns (profit_amount, profit_pct).
        """
        deal = self._get_deal(deal_id)
        current_value = current_price * deal.total_volume
        profit = current_value - deal.total_cost
        profit_pct = (profit / deal.total_cost) * 100 if deal.total_cost > 0 else Decimal("0")
        deal.current_profit_pct = profit_pct
        return profit, profit_pct

    def update_highest_price(self, deal_id: str, current_price: Decimal) -> bool:
        """
        Update highest price since entry. Returns True if new high set.

        Important: highest is NOT reset on safety order fills.
        """
        deal = self._get_deal(deal_id)
        if current_price > deal.highest_price_since_entry:
            deal.highest_price_since_entry = current_price
            return True
        return False

    # -----------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------

    def get_deal(self, deal_id: str) -> DCADeal:
        """Get deal by ID."""
        return self._get_deal(deal_id)

    def get_active_deals(self) -> list[DCADeal]:
        """Get all active deals."""
        return [d for d in self._deals.values() if d.status == DealStatus.ACTIVE]

    def get_closed_deals(self) -> list[DCADeal]:
        """Get all closed deals."""
        return [d for d in self._deals.values() if d.status == DealStatus.CLOSED]

    def get_deal_orders(self, deal_id: str) -> list[DCAOrder]:
        """Get all orders for a deal."""
        return self._orders.get(deal_id, [])

    @property
    def total_realized_pnl(self) -> Decimal:
        """Total realized profit across all closed deals."""
        return sum(
            (d.realized_profit for d in self._deals.values() if d.status == DealStatus.CLOSED),
            Decimal(0),
        )

    # -----------------------------------------------------------------
    # Statistics
    # -----------------------------------------------------------------

    def get_statistics(self) -> dict[str, Any]:
        """Return comprehensive statistics."""
        active = self.get_active_deals()
        closed = self.get_closed_deals()
        winning = [d for d in closed if d.realized_profit > 0]
        losing = [d for d in closed if d.realized_profit < 0]

        return {
            "symbol": self._symbol,
            "total_deals": len(self._deals),
            "active_deals": len(active),
            "closed_deals": len(closed),
            "winning_deals": len(winning),
            "losing_deals": len(losing),
            "win_rate": (f"{len(winning) / len(closed) * 100:.1f}%" if closed else "N/A"),
            "total_realized_pnl": str(self.total_realized_pnl),
            "config": {
                "base_order_volume": str(self._config.base_order_volume),
                "max_safety_orders": self._config.max_safety_orders,
                "volume_multiplier": str(self._config.volume_multiplier),
                "price_step_pct": str(self._config.price_step_pct),
                "take_profit_pct": str(self._config.take_profit_pct),
                "stop_loss_pct": str(self._config.stop_loss_pct),
            },
        }

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    def _get_deal(self, deal_id: str) -> DCADeal:
        if deal_id not in self._deals:
            raise KeyError(f"Deal {deal_id} not found")
        return self._deals[deal_id]
