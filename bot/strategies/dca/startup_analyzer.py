"""
DCA Startup Analyzer — catch-up mode for smart bot startup.

On startup, analyzes historical OHLCV data to identify which DCA levels
have already been passed (price is below them) without an existing open order,
then builds a plan to place the missing orders.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DCALevel:
    """A single DCA price level."""

    level_num: int
    price: Decimal
    amount_usd: Decimal
    has_open_order: bool = False


@dataclass
class CatchUpPlan:
    """Result of the catch-up analysis."""

    base_price: Decimal
    reference_type: str
    all_levels: list[DCALevel] = field(default_factory=list)
    orders_to_place: list[DCALevel] = field(default_factory=list)
    skipped_covered: int = 0  # levels that already have an open order
    skipped_above: int = 0  # levels above current price


class DCAStartupAnalyzer:
    """
    Analyzes market history to identify missing DCA levels on startup.

    Parameters come from ``DCAConfig`` fields:
    - ``trigger_percentage`` — drop % between DCA levels
    - ``max_steps`` — how many levels to compute
    - ``amount_per_step`` — USD per DCA step
    - ``catch_up_max_orders`` — maximum catch-up orders to place
    - ``catch_up_reference`` — "current_price" | "last_high"
    - ``catch_up_lookback_bars`` — bars for rolling-high lookback
    """

    def __init__(
        self,
        trigger_pct: Decimal,
        amount_per_step: Decimal,
        max_steps: int,
        catch_up_max_orders: int = 3,
        catch_up_reference: str = "current_price",
        catch_up_lookback_bars: int = 500,
    ) -> None:
        self.trigger_pct = trigger_pct
        self.amount_per_step = amount_per_step
        self.max_steps = max_steps
        self.catch_up_max_orders = catch_up_max_orders
        self.catch_up_reference = catch_up_reference
        self.catch_up_lookback_bars = catch_up_lookback_bars

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        ohlcv: list[list],
        current_price: Decimal,
        open_orders: list[dict],
        min_order_size: Decimal = Decimal("10"),
    ) -> CatchUpPlan:
        """
        Build a catch-up plan.

        Args:
            ohlcv: Raw OHLCV list [[ts, open, high, low, close, volume], ...]
                   ordered oldest→newest. May be empty.
            current_price: Latest ticker price.
            open_orders: List of exchange open orders (each has ``price`` key).
            min_order_size: Minimum order notional in USD (lot-size filter).

        Returns:
            CatchUpPlan with levels to place.
        """
        if not ohlcv:
            base_price = current_price
            reference_type = "current_price_fallback"
        elif self.catch_up_reference == "last_high":
            base_price = self._find_last_high(ohlcv, current_price)
            reference_type = "last_high"
        else:
            base_price = current_price
            reference_type = "current_price"

        # Build all levels
        all_levels: list[DCALevel] = []
        for n in range(1, self.max_steps + 1):
            level_price = base_price * (1 - self.trigger_pct * n)
            all_levels.append(
                DCALevel(
                    level_num=n,
                    price=level_price,
                    amount_usd=self.amount_per_step,
                )
            )

        # Mark levels that have a matching open order (±0.1%)
        open_prices = [Decimal(str(o.get("price", 0))) for o in open_orders if o.get("price")]
        for level in all_levels:
            for op in open_prices:
                if op > 0 and abs(level.price - op) / op <= Decimal("0.001"):
                    level.has_open_order = True
                    break

        # Split into categories
        skipped_above = 0
        skipped_covered = 0
        candidates: list[DCALevel] = []

        for level in all_levels:
            if level.price >= current_price:
                skipped_above += 1
                continue
            if level.has_open_order:
                skipped_covered += 1
                continue
            # Lot-size check
            if self.amount_per_step < min_order_size:
                logger.debug(
                    "catchup_level_too_small",
                    level=level.level_num,
                    amount_usd=float(self.amount_per_step),
                    min_order_size=float(min_order_size),
                )
                continue
            candidates.append(level)

        # Sort by distance: closest to current price first (ascending abs diff)
        candidates.sort(key=lambda lv: abs(lv.price - current_price))

        # Limit to max_catch_up_orders
        orders_to_place = candidates[: self.catch_up_max_orders]

        plan = CatchUpPlan(
            base_price=base_price,
            reference_type=reference_type,
            all_levels=all_levels,
            orders_to_place=orders_to_place,
            skipped_covered=skipped_covered,
            skipped_above=skipped_above,
        )

        logger.info(
            "catchup_plan_built",
            base_price=float(base_price),
            reference=reference_type,
            total_levels=len(all_levels),
            orders_to_place=len(orders_to_place),
            skipped_covered=skipped_covered,
            skipped_above=skipped_above,
        )
        return plan

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_last_high(self, ohlcv: list[list], current_price: Decimal) -> Decimal:
        """
        Find the rolling high over the last ``catch_up_lookback_bars`` candles,
        confirmed by a pullback of at least ``trigger_pct`` from that high.

        Falls back to ``current_price`` if no confirmed high is found.
        """
        bars = ohlcv[-self.catch_up_lookback_bars :]
        if not bars:
            return current_price

        closes = [Decimal(str(row[4])) for row in bars]  # index 4 = close
        if not closes:
            return current_price

        rolling_max = max(closes)
        pullback_threshold = rolling_max * (1 - self.trigger_pct)

        if current_price <= pullback_threshold:
            logger.debug(
                "catchup_last_high_confirmed",
                rolling_max=float(rolling_max),
                current_price=float(current_price),
                pullback_pct=float(self.trigger_pct),
            )
            return rolling_max

        # Not enough pullback yet — fall back to current price
        logger.debug(
            "catchup_last_high_not_confirmed",
            rolling_max=float(rolling_max),
            current_price=float(current_price),
            required_pullback=float(pullback_threshold),
        )
        return current_price
