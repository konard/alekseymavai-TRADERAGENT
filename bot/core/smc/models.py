"""
bot/core/smc/models.py — Pydantic data models for SMC analysis.

All domain objects are immutable value objects. Price fields use float
for numpy/pandas interop; Decimal conversion happens at the broker boundary.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SwingType(str, Enum):
    HIGH = "high"
    LOW = "low"


class StructureType(str, Enum):
    BOS_BULL = "bos_bull"    # Break of Structure — bullish continuation
    BOS_BEAR = "bos_bear"    # Break of Structure — bearish continuation
    CHOCH_BULL = "choch_bull"  # Change of Character — bullish reversal
    CHOCH_BEAR = "choch_bear"  # Change of Character — bearish reversal


class OBType(str, Enum):
    BULL = "bull"   # Demand zone — last bearish candle before bullish impulse
    BEAR = "bear"   # Supply zone  — last bullish candle before bearish impulse


class FVGType(str, Enum):
    BULL = "bull"   # Bullish imbalance: high[i-2] < low[i]
    BEAR = "bear"   # Bearish imbalance: low[i-2] > high[i]


class LiquidityType(str, Enum):
    EQH = "eqh"   # Equal Highs — liquidity pool above price
    EQL = "eql"   # Equal Lows  — liquidity pool below price
    SUPPLY = "supply"
    DEMAND = "demand"


class SMCPhase(str, Enum):
    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    ACCUMULATION = "accumulation"
    DISTRIBUTION = "distribution"
    RANGING = "ranging"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Core structures
# ---------------------------------------------------------------------------

class SwingPoint(BaseModel):
    """A confirmed pivot high or low."""
    model_config = {"frozen": True}

    index: int = Field(..., description="Bar index in the OHLCV array")
    price: float
    swing_type: SwingType
    strength: int = Field(default=5, description="Bars left/right confirming this swing")
    timestamp: datetime | None = None

    @property
    def is_high(self) -> bool:
        return self.swing_type == SwingType.HIGH

    @property
    def is_low(self) -> bool:
        return self.swing_type == SwingType.LOW


class StructureEvent(BaseModel):
    """A BOS or CHoCH event."""
    model_config = {"frozen": True}

    index: int            # Bar index where break occurred
    structure_type: StructureType
    break_price: float    # Price at which structure was broken
    broken_swing: SwingPoint   # The swing point that was broken
    impulse_size: float        # Size of the impulse (break_price - swing.price)
    timestamp: datetime | None = None

    @property
    def is_bullish(self) -> bool:
        return self.structure_type in (StructureType.BOS_BULL, StructureType.CHOCH_BULL)

    @property
    def is_choch(self) -> bool:
        return self.structure_type in (StructureType.CHOCH_BULL, StructureType.CHOCH_BEAR)


class OrderBlock(BaseModel):
    """Institutional order zone — last opposing candle before structural impulse."""
    model_config = {"frozen": True}

    index: int              # Bar index of the OB candle
    ob_type: OBType
    high: float
    low: float
    open: float
    close: float
    atr_at_formation: float = 0.0
    structure_event: StructureEvent | None = None   # The BOS/CHoCH that confirmed this OB
    touched: bool = False   # Has price returned to this zone?
    invalidated: bool = False   # Has price closed through OB_Low (bull) / OB_High (bear)?
    timestamp: datetime | None = None

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0

    def contains_price(self, price: float) -> bool:
        return self.low <= price <= self.high

    def is_valid(self) -> bool:
        return not self.invalidated


class FairValueGap(BaseModel):
    """Three-candle imbalance zone."""
    model_config = {"frozen": True}

    index: int              # Middle candle index (i-1)
    fvg_type: FVGType
    gap_high: float         # Upper boundary
    gap_low: float          # Lower boundary
    size: float             # gap_high - gap_low
    filled: bool = False    # Has price re-entered the gap?
    timestamp: datetime | None = None

    @model_validator(mode="after")
    def compute_size(self) -> "FairValueGap":
        # size is set externally; just validate
        if self.gap_high < self.gap_low:
            raise ValueError("gap_high must be >= gap_low")
        return self

    def contains_price(self, price: float) -> bool:
        return self.gap_low <= price <= self.gap_high


class LiquidityLevel(BaseModel):
    """Price level where liquidity (stop orders) is pooled."""
    model_config = {"frozen": True}

    price: float
    liquidity_type: LiquidityType
    strength: float = Field(default=1.0, ge=0.0, le=1.0,
                            description="Relative importance 0–1")
    touch_count: int = Field(default=1, description="How many times price touched this level")
    last_touch_index: int = 0
    swept: bool = False     # Has price moved through and taken the liquidity?
    timestamp: datetime | None = None


# ---------------------------------------------------------------------------
# Aggregate context
# ---------------------------------------------------------------------------

class SMCContext(BaseModel):
    """
    Complete SMC picture at a given bar.

    Contains all detected structures, zones, and the derived phase.
    This is what gets passed to MarketRegimeDetector and SMCStrategy.
    """
    model_config = {"frozen": True}

    # Raw structures
    swing_highs: list[SwingPoint] = Field(default_factory=list)
    swing_lows: list[SwingPoint] = Field(default_factory=list)

    # Recent structural events (most recent first)
    structure_events: list[StructureEvent] = Field(default_factory=list)

    # Active zones (not invalidated)
    order_blocks: list[OrderBlock] = Field(default_factory=list)
    fair_value_gaps: list[FairValueGap] = Field(default_factory=list)
    liquidity_levels: list[LiquidityLevel] = Field(default_factory=list)

    # Derived phase
    phase: SMCPhase = SMCPhase.UNKNOWN
    trend_bias: float = Field(
        default=0.0, ge=-1.0, le=1.0,
        description="+1.0 = strong bull, -1.0 = strong bear, 0 = neutral"
    )

    # Bar metadata
    bar_index: int = 0
    current_price: float = 0.0
    timestamp: datetime | None = None

    # Analysis metadata
    warmup_complete: bool = False
    bars_analyzed: int = 0

    # -----------------------------------------------------------------------
    # Convenience helpers
    # -----------------------------------------------------------------------

    @property
    def last_structure(self) -> StructureEvent | None:
        return self.structure_events[0] if self.structure_events else None

    @property
    def last_choch(self) -> StructureEvent | None:
        for ev in self.structure_events:
            if ev.is_choch:
                return ev
        return None

    @property
    def last_bos(self) -> StructureEvent | None:
        for ev in self.structure_events:
            if not ev.is_choch:
                return ev
        return None

    def nearest_bull_ob(self, price: float) -> OrderBlock | None:
        """Return the nearest valid bullish OB at or below current price."""
        candidates = [
            ob for ob in self.order_blocks
            if ob.ob_type == OBType.BULL and ob.is_valid() and ob.high <= price * 1.001
        ]
        return max(candidates, key=lambda ob: ob.high, default=None)

    def nearest_bear_ob(self, price: float) -> OrderBlock | None:
        """Return the nearest valid bearish OB at or above current price."""
        candidates = [
            ob for ob in self.order_blocks
            if ob.ob_type == OBType.BEAR and ob.is_valid() and ob.low >= price * 0.999
        ]
        return min(candidates, key=lambda ob: ob.low, default=None)

    def in_demand_zone(self, price: float) -> bool:
        ob = self.nearest_bull_ob(price)
        return ob.contains_price(price) if ob else False

    def in_supply_zone(self, price: float) -> bool:
        ob = self.nearest_bear_ob(price)
        return ob.contains_price(price) if ob else False

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "trend_bias": round(self.trend_bias, 3),
            "bar_index": self.bar_index,
            "current_price": self.current_price,
            "warmup_complete": self.warmup_complete,
            "bars_analyzed": self.bars_analyzed,
            "swing_highs": len(self.swing_highs),
            "swing_lows": len(self.swing_lows),
            "structure_events": len(self.structure_events),
            "order_blocks": len(self.order_blocks),
            "fair_value_gaps": len(self.fair_value_gaps),
            "liquidity_levels": len(self.liquidity_levels),
            "last_structure": (
                self.last_structure.structure_type.value if self.last_structure else None
            ),
            "last_choch": (
                self.last_choch.structure_type.value if self.last_choch else None
            ),
        }
