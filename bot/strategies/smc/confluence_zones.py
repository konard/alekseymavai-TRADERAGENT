"""
Confluence Zones Module — v2

Backed by bot.core.smc.SMCContext.
Public API is identical to v1; the smartmoneyconcepts pip dependency
has been removed and replaced by our own detectors.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

import pandas as pd

from bot.core.smc.models import (
    FVGType,
    LiquidityType,
    OBType,
    SMCContext,
)
from bot.strategies.smc.market_structure import MarketStructureAnalyzer, StructureEvent
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class ZoneType(str, Enum):
    ORDER_BLOCK = "order_block"
    FAIR_VALUE_GAP = "fair_value_gap"


class ZoneStatus(str, Enum):
    ACTIVE = "active"
    INVALIDATED = "invalidated"
    FILLED = "filled"
    PARTIAL_FILL = "partial_fill"


@dataclass
class OrderBlock:
    zone_type: ZoneType = field(default=ZoneType.ORDER_BLOCK)
    is_bullish: bool = False
    high: Decimal = Decimal("0")
    low: Decimal = Decimal("0")
    open: Decimal = Decimal("0")
    close: Decimal = Decimal("0")
    index: int = 0
    timestamp: pd.Timestamp = None
    timeframe: str = ""
    volume: float = 0.0
    structure_break: Optional[StructureEvent] = None
    status: ZoneStatus = ZoneStatus.ACTIVE
    strength_score: float = 0.0
    touch_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    invalidated_at: Optional[datetime] = None

    def get_range(self) -> Decimal:
        return self.high - self.low

    def get_midpoint(self) -> Decimal:
        return (self.high + self.low) / Decimal("2")

    def contains_price(self, price: Decimal) -> bool:
        return self.low <= price <= self.high


@dataclass
class FairValueGap:
    zone_type: ZoneType = field(default=ZoneType.FAIR_VALUE_GAP)
    is_bullish: bool = False
    gap_high: Decimal = Decimal("0")
    gap_low: Decimal = Decimal("0")
    candle1_index: int = 0
    candle2_index: int = 0
    candle3_index: int = 0
    timestamp: pd.Timestamp = None
    timeframe: str = ""
    status: ZoneStatus = ZoneStatus.ACTIVE
    fill_percentage: float = 0.0
    filled_at: Optional[datetime] = None
    strength_score: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)

    def get_gap_size(self) -> Decimal:
        return self.gap_high - self.gap_low

    def get_midpoint(self) -> Decimal:
        return (self.gap_high + self.gap_low) / Decimal("2")

    def contains_price(self, price: Decimal) -> bool:
        return self.gap_low <= price <= self.gap_high


@dataclass
class LiquidityZone:
    is_bullish: bool = False
    level: Decimal = Decimal("0")
    end_index: int = 0
    swept: bool = False
    index: int = 0
    timestamp: pd.Timestamp = None
    timeframe: str = ""
    strength_score: float = 0.0


class ConfluenceZoneAnalyzer:
    """
    Analyzes and manages confluence zones using bot.core.smc.SMCContext.

    Public API is identical to v1; the smartmoneyconcepts pip dependency
    has been removed and replaced by our own detectors.
    """

    def __init__(
        self,
        market_structure: MarketStructureAnalyzer,
        timeframe: str = "1h",
        max_active_zones: int = 20,
        close_mitigation: bool = False,
        join_consecutive_fvg: bool = False,
        liquidity_range_percent: float = 0.01,
    ) -> None:
        self.market_structure = market_structure
        self.timeframe = timeframe
        self.max_active_zones = max_active_zones
        self.close_mitigation = close_mitigation
        self.join_consecutive_fvg = join_consecutive_fvg
        self.liquidity_range_percent = liquidity_range_percent

        self.order_blocks: list[OrderBlock] = []
        self.fair_value_gaps: list[FairValueGap] = []
        self.liquidity_zones: list[LiquidityZone] = []

        # Log-spam suppression
        self._insufficient_data_count: int = 0
        self._liquidity_fail_count: int = 0

        logger.info(
            "ConfluenceZoneAnalyzer initialized",
            timeframe=timeframe,
            max_zones=max_active_zones,
            close_mitigation=close_mitigation,
        )

    # ------------------------------------------------------------------
    # Public API (unchanged from v1)
    # ------------------------------------------------------------------

    def analyze(self, df: pd.DataFrame) -> dict:
        if len(df) < 3:
            self._insufficient_data_count += 1
            if self._insufficient_data_count == 1:
                logger.warning("Insufficient data for zone analysis")
            return self.get_zones_summary()

        # Prefer pre-computed context from MarketStructureAnalyzer.
        # If absent (e.g. called before market_structure.analyze()), run it now.
        ctx = self.market_structure.get_smc_context()
        if ctx is None:
            self.market_structure.analyze(df)
            ctx = self.market_structure.get_smc_context()

        if ctx is not None:
            self._sync_order_blocks(ctx, df)
            self._sync_fvgs(ctx, df)
            self._sync_liquidity(ctx, df)
            self._cleanup_zones()

        logger.debug(
            "Confluence zones analyzed",
            order_blocks=len([ob for ob in self.order_blocks if ob.status == ZoneStatus.ACTIVE]),
            fvgs=len([fvg for fvg in self.fair_value_gaps if fvg.status == ZoneStatus.ACTIVE]),
            liquidity_zones=len([lz for lz in self.liquidity_zones if not lz.swept]),
        )
        return self.get_zones_summary()

    def get_zones_summary(self) -> dict:
        active_obs  = [ob  for ob  in self.order_blocks   if ob.status  == ZoneStatus.ACTIVE]
        active_fvgs = [fvg for fvg in self.fair_value_gaps if fvg.status == ZoneStatus.ACTIVE]
        active_liq  = [lz  for lz  in self.liquidity_zones if not lz.swept]
        return {
            "order_blocks": {
                "total":   len(self.order_blocks),
                "active":  len(active_obs),
                "bullish": len([ob for ob in active_obs if ob.is_bullish]),
                "bearish": len([ob for ob in active_obs if not ob.is_bullish]),
            },
            "fair_value_gaps": {
                "total":   len(self.fair_value_gaps),
                "active":  len(active_fvgs),
                "bullish": len([fvg for fvg in active_fvgs if fvg.is_bullish]),
                "bearish": len([fvg for fvg in active_fvgs if not fvg.is_bullish]),
            },
            "liquidity_zones": {
                "total":     len(self.liquidity_zones),
                "active":    len(active_liq),
                "buy_side":  len([lz for lz in active_liq if lz.is_bullish]),
                "sell_side": len([lz for lz in active_liq if not lz.is_bullish]),
            },
        }

    def get_active_order_blocks(self, is_bullish: Optional[bool] = None) -> list[OrderBlock]:
        obs = [ob for ob in self.order_blocks if ob.status == ZoneStatus.ACTIVE]
        if is_bullish is not None:
            obs = [ob for ob in obs if ob.is_bullish == is_bullish]
        obs.sort(key=lambda x: x.strength_score, reverse=True)
        return obs

    def get_active_fvgs(self, is_bullish: Optional[bool] = None) -> list[FairValueGap]:
        fvgs = [fvg for fvg in self.fair_value_gaps if fvg.status == ZoneStatus.ACTIVE]
        if is_bullish is not None:
            fvgs = [fvg for fvg in fvgs if fvg.is_bullish == is_bullish]
        fvgs.sort(key=lambda x: x.strength_score, reverse=True)
        return fvgs

    def get_active_liquidity_zones(
        self, is_bullish: Optional[bool] = None
    ) -> list[LiquidityZone]:
        zones = [lz for lz in self.liquidity_zones if not lz.swept]
        if is_bullish is not None:
            zones = [lz for lz in zones if lz.is_bullish == is_bullish]
        return zones

    def find_confluence_at_price(
        self, price: Decimal, tolerance: Decimal = Decimal("0.5")
    ) -> dict:
        tolerance_range = price * (tolerance / Decimal("100"))
        price_low  = price - tolerance_range
        price_high = price + tolerance_range
        nearby_obs  = [
            ob for ob in self.get_active_order_blocks()
            if ob.low <= price_high and ob.high >= price_low
        ]
        nearby_fvgs = [
            fvg for fvg in self.get_active_fvgs()
            if fvg.gap_low <= price_high and fvg.gap_high >= price_low
        ]
        return {
            "price": float(price),
            "order_blocks": nearby_obs,
            "fair_value_gaps": nearby_fvgs,
            "confluence_count": len(nearby_obs) + len(nearby_fvgs),
        }

    # ------------------------------------------------------------------
    # Internal: sync zones from SMCContext
    # ------------------------------------------------------------------

    def _sync_order_blocks(self, ctx: SMCContext, df: pd.DataFrame) -> None:
        seen = {ob.index for ob in self.order_blocks}
        for core_ob in ctx.order_blocks:
            if core_ob.index in seen:
                # Update invalidation / touched status in place
                for ob in self.order_blocks:
                    if ob.index == core_ob.index:
                        if core_ob.invalidated and ob.status == ZoneStatus.ACTIVE:
                            ob.status = ZoneStatus.INVALIDATED
                            ob.invalidated_at = datetime.now()
                        if core_ob.touched:
                            ob.touch_count = max(ob.touch_count, 1)
                continue

            idx = min(core_ob.index, len(df) - 1)
            ts = _to_ts(df.index[idx])
            status = ZoneStatus.INVALIDATED if core_ob.invalidated else ZoneStatus.ACTIVE
            ob = OrderBlock(
                is_bullish=(core_ob.ob_type == OBType.BULL),
                high=Decimal(str(round(core_ob.high, 8))),
                low=Decimal(str(round(core_ob.low, 8))),
                open=Decimal(str(round(core_ob.open, 8))),
                close=Decimal(str(round(core_ob.close, 8))),
                index=core_ob.index,
                timestamp=ts,
                timeframe=self.timeframe,
                status=status,
                touch_count=1 if core_ob.touched else 0,
            )
            ob.strength_score = self._calculate_zone_strength(ob)
            self.order_blocks.append(ob)
            seen.add(core_ob.index)

    def _sync_fvgs(self, ctx: SMCContext, df: pd.DataFrame) -> None:
        seen = {fvg.candle2_index for fvg in self.fair_value_gaps}
        for core_fvg in ctx.fair_value_gaps:
            if core_fvg.index in seen:
                # Update fill status
                for fvg in self.fair_value_gaps:
                    if fvg.candle2_index == core_fvg.index and core_fvg.filled:
                        if fvg.status != ZoneStatus.FILLED:
                            fvg.status = ZoneStatus.FILLED
                            fvg.fill_percentage = 100.0
                            fvg.filled_at = datetime.now()
                continue

            idx = min(core_fvg.index, len(df) - 1)
            ts = _to_ts(df.index[idx])
            status = ZoneStatus.FILLED if core_fvg.filled else ZoneStatus.ACTIVE
            fvg = FairValueGap(
                is_bullish=(core_fvg.fvg_type == FVGType.BULL),
                gap_high=Decimal(str(round(core_fvg.gap_high, 8))),
                gap_low=Decimal(str(round(core_fvg.gap_low, 8))),
                candle1_index=max(0, core_fvg.index - 1),
                candle2_index=core_fvg.index,
                candle3_index=min(len(df) - 1, core_fvg.index + 1),
                timestamp=ts,
                timeframe=self.timeframe,
                status=status,
                fill_percentage=100.0 if core_fvg.filled else 0.0,
                filled_at=datetime.now() if core_fvg.filled else None,
            )
            fvg.strength_score = self._calculate_zone_strength(fvg)
            self.fair_value_gaps.append(fvg)
            seen.add(core_fvg.index)

    def _sync_liquidity(self, ctx: SMCContext, df: pd.DataFrame) -> None:
        self.liquidity_zones.clear()
        for core_liq in ctx.liquidity_levels:
            # EQH / SUPPLY → buy-side (above price)
            is_bullish = core_liq.liquidity_type in (
                LiquidityType.EQH, LiquidityType.SUPPLY
            )
            idx = min(core_liq.last_touch_index, len(df) - 1)
            ts = _to_ts(df.index[idx])
            self.liquidity_zones.append(LiquidityZone(
                is_bullish=is_bullish,
                level=Decimal(str(round(core_liq.price, 8))),
                end_index=core_liq.last_touch_index,
                swept=core_liq.swept,
                index=core_liq.last_touch_index,
                timestamp=ts,
                timeframe=self.timeframe,
                strength_score=core_liq.strength * 100,
            ))

    # ------------------------------------------------------------------
    # Zone strength scoring (unchanged from v1)
    # ------------------------------------------------------------------

    def _calculate_all_zone_scores(self) -> None:
        for ob in self.order_blocks:
            if ob.status == ZoneStatus.ACTIVE:
                ob.strength_score = self._calculate_zone_strength(ob)
        for fvg in self.fair_value_gaps:
            if fvg.status == ZoneStatus.ACTIVE:
                fvg.strength_score = self._calculate_zone_strength(fvg)

    def _calculate_zone_strength(self, zone) -> float:
        score = 0.0
        tf_scores = {"4h": 30, "1h": 20, "15m": 10, "5m": 5}
        score += tf_scores.get(self.timeframe, 15)
        if isinstance(zone, OrderBlock):
            score += min(20, (float(zone.get_range()) / 10) * 20)
            score += 10 - min(10, zone.touch_count * 2)
        elif isinstance(zone, FairValueGap):
            score += min(30, (float(zone.get_gap_size()) / 5) * 30)
            score -= zone.fill_percentage / 10
        age_hours = (datetime.now() - zone.created_at).total_seconds() / 3600
        score += max(0, 20 - (age_hours / 24) * 20)
        return max(0.0, min(100.0, score))

    def _cleanup_zones(self) -> None:
        self.order_blocks = [
            ob for ob in self.order_blocks
            if ob.status == ZoneStatus.ACTIVE
            or (ob.invalidated_at and (datetime.now() - ob.invalidated_at).days < 1)
        ][: self.max_active_zones]
        self.fair_value_gaps = [
            fvg for fvg in self.fair_value_gaps
            if fvg.status in (ZoneStatus.ACTIVE, ZoneStatus.PARTIAL_FILL)
            or (fvg.filled_at and (datetime.now() - fvg.filled_at).days < 1)
        ][: self.max_active_zones]

    @staticmethod
    def _prepare_ohlc_df(df: pd.DataFrame) -> pd.DataFrame:
        """Kept for compatibility; not used internally any more."""
        ohlc = df[["open", "high", "low", "close"]].copy()
        for col in ohlc.columns:
            ohlc[col] = ohlc[col].astype(float)
        if "volume" in df.columns:
            ohlc["volume"] = df["volume"].astype(float)
        return ohlc


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _to_ts(val) -> pd.Timestamp:
    if isinstance(val, pd.Timestamp):
        return val
    return pd.Timestamp(val)
