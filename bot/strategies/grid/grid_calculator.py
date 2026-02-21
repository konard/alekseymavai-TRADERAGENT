"""
GridCalculator — Grid level calculation engine for TRADERAGENT v2.0.

Supports:
- Arithmetic grids (evenly spaced price levels)
- Geometric grids (percentage-spaced / ratio-based levels)
- ATR-based dynamic adjustment of grid bounds
- Optimal grid count calculation based on volatility
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# =============================================================================
# Enums & Data Structures
# =============================================================================


class GridSpacing(str, Enum):
    """Grid spacing type."""

    ARITHMETIC = "arithmetic"
    GEOMETRIC = "geometric"


@dataclass
class GridLevel:
    """A single grid level with price, side, and order amount."""

    index: int
    price: Decimal
    side: str  # "buy" or "sell"
    amount: Decimal  # base currency amount

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "price": str(self.price),
            "side": self.side,
            "amount": str(self.amount),
        }


@dataclass
class GridConfig:
    """Configuration for grid calculation."""

    upper_price: Decimal
    lower_price: Decimal
    num_levels: int
    spacing: GridSpacing = GridSpacing.ARITHMETIC
    amount_per_grid: Decimal = Decimal("100")  # quote currency per level
    profit_per_grid: Decimal = Decimal("0.005")  # 0.5% default

    def validate(self) -> None:
        """Validate config values. Raises ValueError on invalid config."""
        if self.upper_price <= self.lower_price:
            raise ValueError("upper_price must be greater than lower_price")
        if self.num_levels < 2:
            raise ValueError("num_levels must be at least 2")
        if self.amount_per_grid <= 0:
            raise ValueError("amount_per_grid must be positive")
        if self.profit_per_grid < 0:
            raise ValueError("profit_per_grid must be non-negative")


# =============================================================================
# Grid Calculator
# =============================================================================


class GridCalculator:
    """
    Calculates optimal grid levels for grid trading strategy.

    Supports arithmetic (evenly spaced) and geometric (ratio-based) grids,
    ATR-based dynamic bound adjustment, and optimal grid count estimation.
    """

    # Precision for price rounding
    PRICE_PRECISION = Decimal("0.01")
    AMOUNT_PRECISION = Decimal("0.001")

    # =================================================================
    # Core Level Calculation
    # =================================================================

    @staticmethod
    def calculate_arithmetic_levels(
        upper_price: Decimal,
        lower_price: Decimal,
        num_levels: int,
    ) -> list[Decimal]:
        """
        Calculate evenly spaced grid levels.

        The step between each level is constant:
            step = (upper - lower) / (num_levels - 1)

        Args:
            upper_price: Upper grid boundary.
            lower_price: Lower grid boundary.
            num_levels: Total number of levels (including boundaries).

        Returns:
            Sorted list of Decimal prices from lower to upper.
        """
        if num_levels < 2:
            raise ValueError("num_levels must be at least 2")
        if upper_price <= lower_price:
            raise ValueError("upper_price must be greater than lower_price")

        step = (upper_price - lower_price) / (num_levels - 1)
        levels = [
            (lower_price + step * i).quantize(
                GridCalculator.PRICE_PRECISION, rounding=ROUND_HALF_UP
            )
            for i in range(num_levels)
        ]
        return levels

    @staticmethod
    def calculate_geometric_levels(
        upper_price: Decimal,
        lower_price: Decimal,
        num_levels: int,
    ) -> list[Decimal]:
        """
        Calculate ratio-based (geometric) grid levels.

        Each level is a constant ratio apart:
            ratio = (upper / lower) ^ (1 / (num_levels - 1))
            level_i = lower * ratio^i

        This means each step is the same percentage increase,
        resulting in denser levels at lower prices and wider at higher prices.

        Args:
            upper_price: Upper grid boundary.
            lower_price: Lower grid boundary.
            num_levels: Total number of levels (including boundaries).

        Returns:
            Sorted list of Decimal prices from lower to upper.
        """
        if num_levels < 2:
            raise ValueError("num_levels must be at least 2")
        if upper_price <= lower_price:
            raise ValueError("upper_price must be greater than lower_price")
        if lower_price <= 0:
            raise ValueError("lower_price must be positive for geometric grid")

        # Use float for power calculation, then convert back
        ratio = float(upper_price / lower_price) ** (1.0 / (num_levels - 1))

        levels = []
        for i in range(num_levels):
            price = lower_price * Decimal(str(ratio**i))
            levels.append(price.quantize(GridCalculator.PRICE_PRECISION, rounding=ROUND_HALF_UP))
        return levels

    @staticmethod
    def calculate_levels(
        upper_price: Decimal,
        lower_price: Decimal,
        num_levels: int,
        spacing: GridSpacing = GridSpacing.ARITHMETIC,
    ) -> list[Decimal]:
        """
        Calculate grid levels using the specified spacing type.

        Args:
            upper_price: Upper grid boundary.
            lower_price: Lower grid boundary.
            num_levels: Total number of levels.
            spacing: ARITHMETIC or GEOMETRIC.

        Returns:
            Sorted list of Decimal prices from lower to upper.
        """
        if spacing == GridSpacing.ARITHMETIC:
            return GridCalculator.calculate_arithmetic_levels(upper_price, lower_price, num_levels)
        elif spacing == GridSpacing.GEOMETRIC:
            return GridCalculator.calculate_geometric_levels(upper_price, lower_price, num_levels)
        else:
            raise ValueError(f"Unknown spacing type: {spacing}")

    # =================================================================
    # ATR Calculation & Dynamic Bounds
    # =================================================================

    @staticmethod
    def calculate_atr(
        highs: list[Decimal],
        lows: list[Decimal],
        closes: list[Decimal],
        period: int = 14,
    ) -> Decimal:
        """
        Calculate Average True Range (ATR).

        True Range = max(high - low, |high - prev_close|, |low - prev_close|)
        ATR = SMA of True Range over `period` bars.

        Args:
            highs: List of high prices.
            lows: List of low prices.
            closes: List of close prices.
            period: ATR lookback period (default 14).

        Returns:
            ATR value as Decimal.

        Raises:
            ValueError: If input data is insufficient.
        """
        n = len(highs)
        if n < 2 or len(lows) != n or len(closes) != n:
            raise ValueError("highs, lows, closes must have equal length >= 2")
        if period < 1:
            raise ValueError("period must be >= 1")

        # Calculate True Range for each bar (starting from index 1)
        true_ranges: list[Decimal] = []
        for i in range(1, n):
            high_low = highs[i] - lows[i]
            high_prev_close = abs(highs[i] - closes[i - 1])
            low_prev_close = abs(lows[i] - closes[i - 1])
            tr = max(high_low, high_prev_close, low_prev_close)
            true_ranges.append(tr)

        # Use the last `period` true ranges (or all if less)
        use_count = min(period, len(true_ranges))
        recent_tr = true_ranges[-use_count:]
        atr = sum(recent_tr, Decimal(0)) / Decimal(use_count)

        return atr.quantize(GridCalculator.PRICE_PRECISION, rounding=ROUND_HALF_UP)

    @staticmethod
    def adjust_bounds_by_atr(
        current_price: Decimal,
        atr: Decimal,
        atr_multiplier: Decimal = Decimal("3"),
    ) -> tuple[Decimal, Decimal]:
        """
        Calculate grid boundaries based on ATR.

        upper = current_price + atr * multiplier
        lower = current_price - atr * multiplier

        Args:
            current_price: Current market price.
            atr: Average True Range value.
            atr_multiplier: How many ATR units to use (default 3).

        Returns:
            Tuple of (upper_price, lower_price).

        Raises:
            ValueError: If resulting lower_price <= 0.
        """
        if atr <= 0:
            raise ValueError("atr must be positive")
        if atr_multiplier <= 0:
            raise ValueError("atr_multiplier must be positive")

        offset = atr * atr_multiplier
        upper = (current_price + offset).quantize(
            GridCalculator.PRICE_PRECISION, rounding=ROUND_HALF_UP
        )
        lower = (current_price - offset).quantize(
            GridCalculator.PRICE_PRECISION, rounding=ROUND_HALF_UP
        )

        if lower <= 0:
            raise ValueError(
                f"ATR-based lower bound is non-positive ({lower}). "
                f"Reduce atr_multiplier or check price data."
            )

        return upper, lower

    # =================================================================
    # Grid Order Generation
    # =================================================================

    @staticmethod
    def calculate_grid_orders(
        levels: list[Decimal],
        current_price: Decimal,
        amount_per_grid: Decimal,
        profit_per_grid: Decimal = Decimal("0"),
    ) -> list[GridLevel]:
        """
        Generate buy/sell grid orders from price levels.

        Buy orders are placed at levels below current_price.
        Sell orders are placed at levels above current_price.
        Levels equal to current_price are skipped.

        Args:
            levels: Sorted list of grid price levels.
            current_price: Current market price.
            amount_per_grid: Amount in quote currency per grid level.
            profit_per_grid: Profit margin for sell orders (e.g., 0.005 = 0.5%).

        Returns:
            List of GridLevel objects.
        """
        orders: list[GridLevel] = []
        precision = GridCalculator.AMOUNT_PRECISION

        for idx, price in enumerate(levels):
            if price == current_price:
                continue

            if price < current_price:
                # Buy order: convert quote amount to base
                base_amount = (amount_per_grid / price).quantize(precision, rounding=ROUND_HALF_UP)
                orders.append(GridLevel(index=idx, price=price, side="buy", amount=base_amount))
            else:
                # Sell order: apply profit margin
                sell_price = price * (Decimal("1") + profit_per_grid)
                sell_price = sell_price.quantize(
                    GridCalculator.PRICE_PRECISION, rounding=ROUND_HALF_UP
                )
                base_amount = (amount_per_grid / sell_price).quantize(
                    precision, rounding=ROUND_HALF_UP
                )
                orders.append(
                    GridLevel(index=idx, price=sell_price, side="sell", amount=base_amount)
                )

        logger.debug(
            "Grid orders calculated",
            total=len(orders),
            buys=sum(1 for o in orders if o.side == "buy"),
            sells=sum(1 for o in orders if o.side == "sell"),
        )

        return orders

    # =================================================================
    # Optimal Grid Count
    # =================================================================

    @staticmethod
    def optimal_grid_count(
        upper_price: Decimal,
        lower_price: Decimal,
        atr: Decimal,
        min_levels: int = 5,
        max_levels: int = 50,
    ) -> int:
        """
        Estimate optimal number of grid levels based on volatility.

        The idea: grid spacing should be roughly 1 ATR so that normal
        price movements trigger grid fills without excessive empty gaps.

        optimal = (upper - lower) / atr + 1, clamped to [min, max].

        Args:
            upper_price: Upper grid boundary.
            lower_price: Lower grid boundary.
            atr: Average True Range value.
            min_levels: Minimum allowed grid levels (default 5).
            max_levels: Maximum allowed grid levels (default 50).

        Returns:
            Optimal number of grid levels.
        """
        if atr <= 0:
            raise ValueError("atr must be positive")
        if upper_price <= lower_price:
            raise ValueError("upper_price must be greater than lower_price")

        price_range = upper_price - lower_price
        raw_count = int(price_range / atr) + 1

        return max(min_levels, min(max_levels, raw_count))

    # =================================================================
    # Full Grid Calculation (convenience)
    # =================================================================

    @staticmethod
    def calculate_full_grid(config: GridConfig, current_price: Decimal) -> list[GridLevel]:
        """
        Calculate a complete grid from config and current price.

        Convenience method combining level calculation and order generation.

        Args:
            config: Grid configuration.
            current_price: Current market price.

        Returns:
            List of GridLevel orders.
        """
        config.validate()

        levels = GridCalculator.calculate_levels(
            config.upper_price,
            config.lower_price,
            config.num_levels,
            config.spacing,
        )

        orders = GridCalculator.calculate_grid_orders(
            levels,
            current_price,
            config.amount_per_grid,
            config.profit_per_grid,
        )

        logger.info(
            "Full grid calculated",
            spacing=config.spacing.value,
            num_levels=config.num_levels,
            orders=len(orders),
            upper=float(config.upper_price),
            lower=float(config.lower_price),
        )

        return orders

    @staticmethod
    def calculate_atr_grid(
        current_price: Decimal,
        highs: list[Decimal],
        lows: list[Decimal],
        closes: list[Decimal],
        atr_period: int = 14,
        atr_multiplier: Decimal = Decimal("3"),
        spacing: GridSpacing = GridSpacing.ARITHMETIC,
        amount_per_grid: Decimal = Decimal("100"),
        profit_per_grid: Decimal = Decimal("0.005"),
        num_levels: int | None = None,
        min_levels: int = 5,
        max_levels: int = 50,
    ) -> tuple[list[GridLevel], dict[str, Any]]:
        """
        Calculate a full ATR-based dynamic grid.

        Steps:
        1. Calculate ATR from price data
        2. Derive upper/lower bounds from ATR
        3. Determine optimal grid count (if not specified)
        4. Generate grid levels and orders

        Args:
            current_price: Current market price.
            highs: List of high prices for ATR.
            lows: List of low prices for ATR.
            closes: List of close prices for ATR.
            atr_period: ATR lookback period.
            atr_multiplier: ATR multiplier for bounds.
            spacing: Grid spacing type.
            amount_per_grid: Quote currency amount per grid level.
            profit_per_grid: Profit margin for sell orders.
            num_levels: Override grid count (None = auto from ATR).
            min_levels: Minimum levels for auto count.
            max_levels: Maximum levels for auto count.

        Returns:
            Tuple of (grid_orders, metadata_dict).
        """
        atr = GridCalculator.calculate_atr(highs, lows, closes, atr_period)
        upper, lower = GridCalculator.adjust_bounds_by_atr(current_price, atr, atr_multiplier)

        if num_levels is None:
            num_levels = GridCalculator.optimal_grid_count(
                upper, lower, atr, min_levels, max_levels
            )

        config = GridConfig(
            upper_price=upper,
            lower_price=lower,
            num_levels=num_levels,
            spacing=spacing,
            amount_per_grid=amount_per_grid,
            profit_per_grid=profit_per_grid,
        )

        orders = GridCalculator.calculate_full_grid(config, current_price)

        metadata = {
            "atr": str(atr),
            "atr_period": atr_period,
            "atr_multiplier": str(atr_multiplier),
            "upper_price": str(upper),
            "lower_price": str(lower),
            "num_levels": num_levels,
            "spacing": spacing.value,
            "total_orders": len(orders),
            "buy_orders": sum(1 for o in orders if o.side == "buy"),
            "sell_orders": sum(1 for o in orders if o.side == "sell"),
        }

        logger.info(
            "ATR-based grid calculated",
            atr=float(atr),
            upper=float(upper),
            lower=float(lower),
            num_levels=num_levels,
        )

        return orders, metadata

    # =================================================================
    # Utility Methods
    # =================================================================

    @staticmethod
    def grid_spacing_pct(levels: list[Decimal]) -> list[Decimal]:
        """
        Calculate percentage spacing between consecutive grid levels.

        Useful for verifying geometric grids have constant ratio.

        Args:
            levels: Sorted list of grid levels.

        Returns:
            List of percentage gaps between consecutive levels.
        """
        if len(levels) < 2:
            return []

        pcts = []
        for i in range(1, len(levels)):
            pct = ((levels[i] - levels[i - 1]) / levels[i - 1]) * 100
            pcts.append(pct.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        return pcts

    @staticmethod
    def total_investment(orders: list[GridLevel]) -> Decimal:
        """
        Calculate total quote currency needed for all buy orders.

        Args:
            orders: List of grid orders.

        Returns:
            Total investment in quote currency.
        """
        total = Decimal("0")
        for order in orders:
            if order.side == "buy":
                total += order.price * order.amount
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
