"""
bot/core/smc/supply_demand_detector.py — Liquidity level detection.

Detects:
  - Equal Highs (EQH): two or more swing highs within `tolerance_pct` of each other
  - Equal Lows  (EQL): two or more swing lows  within `tolerance_pct` of each other
  - Supply zones derived from bearish order blocks (already in OB list)
  - Demand zones derived from bullish order blocks (already in OB list)

Liquidity is where stop orders cluster — primarily at EQH/EQL and at
previous swing extremes that have been tested multiple times.
"""
from __future__ import annotations

import numpy as np

from bot.core.smc.models import (
    LiquidityLevel,
    LiquidityType,
    OrderBlock,
    OBType,
    SwingPoint,
)
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class SupplyDemandDetector:
    """
    Detects liquidity pools (EQH/EQL) and supply/demand zones.

    Usage
    -----
    detector = SupplyDemandDetector(tolerance_pct=0.002, min_touches=2)
    levels = detector.detect(swing_highs, swing_lows, order_blocks, close)
    """

    def __init__(
        self,
        tolerance_pct: float = 0.002,
        min_touches: int = 2,
        max_levels: int = 20,
    ) -> None:
        """
        Args:
            tolerance_pct: Price tolerance for grouping equal highs/lows
                           (e.g. 0.002 = 0.2%).
            min_touches:   Minimum number of touches to create an EQH/EQL.
            max_levels:    Cap on number of returned levels.
        """
        self.tolerance_pct = tolerance_pct
        self.min_touches = min_touches
        self.max_levels = max_levels

    def detect(
        self,
        swing_highs: list[SwingPoint],
        swing_lows: list[SwingPoint],
        order_blocks: list[OrderBlock],
        close: np.ndarray,
    ) -> list[LiquidityLevel]:
        """
        Detect all active liquidity levels.

        Returns list sorted by price ascending.
        """
        levels: list[LiquidityLevel] = []

        last_close = float(close[-1]) if len(close) > 0 else 0.0

        # 1. Equal Highs
        levels.extend(self._detect_equal_levels(
            swing_highs, LiquidityType.EQH, last_close
        ))

        # 2. Equal Lows
        levels.extend(self._detect_equal_levels(
            swing_lows, LiquidityType.EQL, last_close
        ))

        # 3. Supply zones from bearish OBs
        for ob in order_blocks:
            if ob.ob_type == OBType.BEAR and not ob.invalidated:
                levels.append(LiquidityLevel(
                    price=ob.high,
                    liquidity_type=LiquidityType.SUPPLY,
                    strength=0.7,
                    touch_count=1,
                    last_touch_index=ob.index,
                    swept=ob.invalidated,
                ))

        # 4. Demand zones from bullish OBs
        for ob in order_blocks:
            if ob.ob_type == OBType.BULL and not ob.invalidated:
                levels.append(LiquidityLevel(
                    price=ob.low,
                    liquidity_type=LiquidityType.DEMAND,
                    strength=0.7,
                    touch_count=1,
                    last_touch_index=ob.index,
                    swept=ob.invalidated,
                ))

        # Sort by price, cap
        levels.sort(key=lambda lv: lv.price)
        levels = levels[-self.max_levels:]

        logger.debug(
            "liquidity_levels_detected",
            total=len(levels),
            eqh=sum(1 for lv in levels if lv.liquidity_type == LiquidityType.EQH),
            eql=sum(1 for lv in levels if lv.liquidity_type == LiquidityType.EQL),
            supply=sum(1 for lv in levels if lv.liquidity_type == LiquidityType.SUPPLY),
            demand=sum(1 for lv in levels if lv.liquidity_type == LiquidityType.DEMAND),
        )
        return levels

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _detect_equal_levels(
        self,
        swing_points: list[SwingPoint],
        liq_type: LiquidityType,
        last_close: float,
    ) -> list[LiquidityLevel]:
        """
        Group swing points that cluster within tolerance_pct of each other.
        Groups with >= min_touches become liquidity levels.
        """
        if not swing_points:
            return []

        # Sort by price
        sorted_pts = sorted(swing_points, key=lambda s: s.price)

        groups: list[list[SwingPoint]] = []
        current_group: list[SwingPoint] = [sorted_pts[0]]

        for pt in sorted_pts[1:]:
            ref_price = current_group[0].price
            if ref_price == 0:
                current_group = [pt]
                continue
            if abs(pt.price - ref_price) / ref_price <= self.tolerance_pct:
                current_group.append(pt)
            else:
                groups.append(current_group)
                current_group = [pt]
        groups.append(current_group)

        result: list[LiquidityLevel] = []
        for group in groups:
            if len(group) < self.min_touches:
                continue
            avg_price = np.mean([pt.price for pt in group])
            touch_count = len(group)
            last_index = max(pt.index for pt in group)
            # strength proportional to touches (capped at 1.0)
            strength = min(1.0, touch_count / 5.0)
            # swept if last close has moved through the level
            swept = (
                (liq_type == LiquidityType.EQH and last_close > avg_price * 1.001) or
                (liq_type == LiquidityType.EQL and last_close < avg_price * 0.999)
            )
            result.append(LiquidityLevel(
                price=float(avg_price),
                liquidity_type=liq_type,
                strength=strength,
                touch_count=touch_count,
                last_touch_index=last_index,
                swept=swept,
            ))

        return result

    # ------------------------------------------------------------------
    # Helpers for downstream consumers
    # ------------------------------------------------------------------

    @staticmethod
    def nearest_resistance(
        levels: list[LiquidityLevel], price: float
    ) -> LiquidityLevel | None:
        """Nearest EQH or SUPPLY level above the current price."""
        candidates = [
            lv for lv in levels
            if lv.price > price and not lv.swept
            and lv.liquidity_type in (LiquidityType.EQH, LiquidityType.SUPPLY)
        ]
        return min(candidates, key=lambda lv: lv.price, default=None)

    @staticmethod
    def nearest_support(
        levels: list[LiquidityLevel], price: float
    ) -> LiquidityLevel | None:
        """Nearest EQL or DEMAND level below the current price."""
        candidates = [
            lv for lv in levels
            if lv.price < price and not lv.swept
            and lv.liquidity_type in (LiquidityType.EQL, LiquidityType.DEMAND)
        ]
        return max(candidates, key=lambda lv: lv.price, default=None)
