"""
PriceZoneAllocator — vertical price zone calculator for strategy separation.

Each strategy operates in its own price zone to avoid conflicts:
- Grid:    ±10% around current price (±10.5% with 5% buffer overlap)
- DCA:     −10% to −30% below current price
- TF Long: +10% to +35% above current price
- SMC:     overlay (structural entries at any level)

The 5% overlap buffer at zone boundaries prevents gaps where price movement
would be missed between adjacent zones.

Usage::

    allocator = PriceZoneAllocator()
    zones = allocator.get_zones(Decimal("50000"), Decimal("1000"))
    in_grid = allocator.is_price_in_zone(Decimal("50500"), "grid")
"""

from __future__ import annotations

from decimal import Decimal

from bot.utils.logger import get_logger

logger = get_logger(__name__)

# 5% overlap buffer (half applied on each side of the boundary)
_OVERLAP = Decimal("0.025")  # 2.5% on each boundary side → 5% total overlap

# Zone boundary multipliers (relative to reference price)
_GRID_LOW_MULT = Decimal("0.90")
_GRID_HIGH_MULT = Decimal("1.10")
_DCA_LOW_MULT = Decimal("0.70")
_DCA_HIGH_MULT = Decimal("0.90")
_TF_LOW_MULT = Decimal("1.10")
_TF_HIGH_MULT = Decimal("1.35")

# Recalculation threshold: update zones when price moves more than this % from ref
_RECALC_THRESHOLD = Decimal("0.05")  # 5%


class PriceZoneAllocator:
    """
    Manages vertical price zones for each trading strategy.

    Each strategy is assigned a distinct price range to operate in.
    Zones slightly overlap (5% buffer) at boundaries to avoid missing
    price movements at zone edges.

    Parameters
    ----------
    reference_price:
        The price around which zones are initially calculated.
        Defaults to ``None`` (must call ``get_zones`` with a price to initialise).
    """

    def __init__(self, reference_price: Decimal | None = None) -> None:
        self._ref_price: Decimal | None = reference_price
        self._zones: dict[str, tuple[Decimal, Decimal] | None] = {}
        if reference_price is not None:
            self._compute_zones(reference_price)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_zones(self, price: Decimal, atr: Decimal) -> dict[str, tuple[Decimal, Decimal] | None]:
        """
        Return price zones for each strategy, computed from *price*.

        Zone boundaries include a 5% overlap buffer so that price movements
        near boundaries are captured by both adjacent strategies:

        - ``grid``:    (price × 0.875, price × 1.125)   — ±10% with ±2.5% buffer
        - ``dca``:     (price × 0.675, price × 0.925)   — −30%..−7.5% with buffer
        - ``tf_long``: (price × 1.075, price × 1.375)   — +7.5%..+37.5% with buffer
        - ``smc``:     ``None``                          — overlay, no fixed zone

        The *atr* parameter is accepted for API compatibility and future use
        (e.g. ATR-scaled zone widths) but is not currently applied to the
        fixed-multiplier boundaries.

        Parameters
        ----------
        price:
            Current market price (reference point for zone calculation).
        atr:
            Average True Range (reserved for future ATR-scaled zones).

        Returns
        -------
        dict[str, tuple[Decimal, Decimal] | None]
            Mapping of strategy name → (low, high) price bounds, or None for SMC.
        """
        self._compute_zones(price)
        return dict(self._zones)

    def is_price_in_zone(self, price: Decimal, zone_name: str) -> bool:
        """
        Return True if *price* falls within the named zone.

        Parameters
        ----------
        price:
            The price to check.
        zone_name:
            Strategy zone name: ``"grid"``, ``"dca"``, ``"tf_long"``, or ``"smc"``.

        Returns
        -------
        bool
            True when *price* is within the zone bounds (inclusive).
            SMC always returns True (overlay zone, no restriction).
            Returns False when zones have not been initialised yet.
        """
        if zone_name == "smc":
            return True  # SMC is an overlay — active on any price level

        if not self._zones:
            logger.warning("price_zone_not_initialised", zone=zone_name)
            return False

        zone = self._zones.get(zone_name)
        if zone is None:
            return True  # None means no restriction

        low, high = zone
        return low <= price <= high

    def recalculate(self, new_price: Decimal) -> bool:
        """
        Recalculate zones when price has moved more than 5% from the reference.

        Call this method when ATR is refreshed (every 14 bars) or when a
        significant price move is detected.

        Parameters
        ----------
        new_price:
            Latest market price.

        Returns
        -------
        bool
            True if zones were recalculated, False if the move was below threshold.
        """
        if self._ref_price is None:
            self._compute_zones(new_price)
            return True

        move = abs(new_price - self._ref_price) / self._ref_price
        if move > _RECALC_THRESHOLD:
            logger.info(
                "price_zone_recalculated",
                old_ref=float(self._ref_price),
                new_ref=float(new_price),
                move_pct=float(move * 100),
            )
            self._compute_zones(new_price)
            return True

        return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_zones(self, price: Decimal) -> None:
        """Recompute all zone boundaries using *price* as the reference."""
        self._ref_price = price

        # Core boundaries (without overlap)
        grid_low_base = price * _GRID_LOW_MULT  # price × 0.90
        grid_high_base = price * _GRID_HIGH_MULT  # price × 1.10
        dca_low_base = price * _DCA_LOW_MULT  # price × 0.70
        dca_high_base = price * _DCA_HIGH_MULT  # price × 0.90
        tf_low_base = price * _TF_LOW_MULT  # price × 1.10
        tf_high_base = price * _TF_HIGH_MULT  # price × 1.35

        # Apply 5% overlap buffer at boundaries (2.5% on each side)
        # Grid: expand both sides by price × 2.5%
        grid_buffer = price * _OVERLAP
        grid_low = grid_low_base - grid_buffer  # price × 0.875
        grid_high = grid_high_base + grid_buffer  # price × 1.125

        # DCA: expand both sides
        dca_buffer = price * _OVERLAP
        dca_low = dca_low_base - dca_buffer  # price × 0.675
        dca_high = dca_high_base + dca_buffer  # price × 0.925

        # TF Long: expand both sides
        tf_buffer = price * _OVERLAP
        tf_low = tf_low_base - tf_buffer  # price × 1.075
        tf_high = tf_high_base + tf_buffer  # price × 1.375

        self._zones = {
            "grid": (grid_low, grid_high),
            "dca": (dca_low, dca_high),
            "tf_long": (tf_low, tf_high),
            "smc": None,  # overlay — operates on structural levels at any price
        }

        logger.debug(
            "price_zones_computed",
            ref_price=float(price),
            grid=(float(grid_low), float(grid_high)),
            dca=(float(dca_low), float(dca_high)),
            tf_long=(float(tf_low), float(tf_high)),
        )

    @property
    def reference_price(self) -> Decimal | None:
        """Return the current reference price for zone calculation."""
        return self._ref_price

    @property
    def zones(self) -> dict[str, tuple[Decimal, Decimal] | None]:
        """Return a snapshot of the current price zones."""
        return dict(self._zones)
