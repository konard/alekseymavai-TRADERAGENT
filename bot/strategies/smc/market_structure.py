"""
Market Structure Analysis Module — v2

Backed by bot.core.smc.SMCAnalyzer.
Public API is identical to v1; the smartmoneyconcepts pip dependency
has been removed and replaced by our own vectorised detectors.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

import pandas as pd

from bot.core.smc.analyzer import SMCAnalyzer
from bot.core.smc.models import SMCContext, SMCPhase
from bot.core.smc.models import StructureEvent as CoreStructureEvent
from bot.core.smc.models import SwingPoint as CoreSwingPoint
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class TrendDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGING = "ranging"


class StructureBreak(str, Enum):
    BOS = "break_of_structure"
    CHOCH = "change_of_character"


@dataclass
class SwingPoint:
    index: int
    price: Decimal
    timestamp: pd.Timestamp
    is_high: bool
    strength: int


@dataclass
class StructureEvent:
    event_type: StructureBreak
    index: int
    price: Decimal
    timestamp: pd.Timestamp
    previous_swing: SwingPoint
    current_trend: TrendDirection


class MarketStructureAnalyzer:
    """
    Market structure analyser backed by bot.core.smc.SMCAnalyzer.

    Public API is identical to v1; the smartmoneyconcepts pip dependency
    has been removed and replaced by our own detectors.
    """

    def __init__(
        self,
        swing_length: int = 5,
        trend_period: int = 20,
        close_break: bool = True,
    ) -> None:
        self.swing_length = swing_length
        self.trend_period = trend_period
        self.close_break = close_break

        # min_impulse_atr respects the close_break flag:
        # close_break=True → require ≥0.1 ATR impulse (filters wick-only breaks)
        self._analyzer = SMCAnalyzer(
            swing_strength=swing_length,
            min_warmup_bars=max(50, swing_length * 4),
            min_impulse_atr=0.1 if close_break else 0.0,
        )
        self._smc_context: Optional[SMCContext] = None

        # Public state (populated from context after each analyze() call)
        self.swing_highs: list[SwingPoint] = []
        self.swing_lows: list[SwingPoint] = []
        self.structure_events: list[StructureEvent] = []
        self.current_trend: TrendDirection = TrendDirection.RANGING

        # Kept for interface compatibility — no longer a real DataFrame
        self._swings_df: None = None

        # Log-spam suppression
        self._insufficient_data_count: int = 0

        logger.info(
            "MarketStructureAnalyzer initialized",
            swing_length=swing_length,
            trend_period=trend_period,
            close_break=close_break,
        )

    # ------------------------------------------------------------------
    # Public API (unchanged from v1)
    # ------------------------------------------------------------------

    def analyze(self, df: pd.DataFrame) -> dict:
        required = self.swing_length * 2 + 1
        if len(df) < required:
            self._insufficient_data_count += 1
            if self._insufficient_data_count == 1:
                logger.warning(
                    "Insufficient data for structure analysis",
                    required=required,
                    available=len(df),
                )
            return self.get_current_structure()

        self._smc_context = self._analyzer.analyze(df)
        self._sync_from_context(df)

        logger.debug(
            "Market structure analyzed",
            swing_highs=len(self.swing_highs),
            swing_lows=len(self.swing_lows),
            trend=self.current_trend,
            events=len(self.structure_events),
        )
        return self.get_current_structure()

    def analyze_trend(self, df_d1: pd.DataFrame, df_h4: pd.DataFrame) -> dict:
        result: dict = {
            "d1_trend": TrendDirection.RANGING,
            "h4_trend": TrendDirection.RANGING,
            "trend_strength": 0.0,
            "trend_aligned": False,
        }

        d1_swing = max(5, self.swing_length // 5)
        if len(df_d1) >= self.trend_period:
            d1_ana = MarketStructureAnalyzer(
                swing_length=d1_swing,
                trend_period=self.trend_period,
                close_break=self.close_break,
            )
            d1_ana.analyze(df_d1)
            result["d1_trend"] = d1_ana.current_trend

        h4_swing = max(5, self.swing_length // 2)
        if len(df_h4) >= self.trend_period:
            h4_ana = MarketStructureAnalyzer(
                swing_length=h4_swing,
                trend_period=self.trend_period,
                close_break=self.close_break,
            )
            h4_ana.analyze(df_h4)
            result["h4_trend"] = h4_ana.current_trend

        result["trend_aligned"] = (
            result["d1_trend"] == result["h4_trend"]
            and result["d1_trend"] != TrendDirection.RANGING
        )
        if result["trend_aligned"]:
            result["trend_strength"] = 1.0
        elif (
            result["d1_trend"] != TrendDirection.RANGING
            or result["h4_trend"] != TrendDirection.RANGING
        ):
            result["trend_strength"] = 0.5
        else:
            result["trend_strength"] = 0.0

        logger.info(
            "Multi-timeframe trend analyzed",
            d1_trend=result["d1_trend"],
            h4_trend=result["h4_trend"],
            strength=result["trend_strength"],
            aligned=result["trend_aligned"],
        )
        return result

    def get_swings_df(self) -> Optional[pd.DataFrame]:
        """Return swing highs/lows as a DataFrame with 'HighLow' and 'Level' columns.

        Returns None when no analysis has been run yet (no swing data available).
        Compatible with legacy callers that expected this format from v1.
        """
        if not self.swing_highs and not self.swing_lows:
            return None

        rows = []
        for sp in self.swing_highs:
            rows.append(
                {
                    "HighLow": 1,  # 1 = swing high
                    "Level": float(sp.price),
                    "index": sp.index,
                    "timestamp": sp.timestamp,
                }
            )
        for sp in self.swing_lows:
            rows.append(
                {
                    "HighLow": -1,  # -1 = swing low
                    "Level": float(sp.price),
                    "index": sp.index,
                    "timestamp": sp.timestamp,
                }
            )

        df = pd.DataFrame(rows)
        if df.empty:
            return None
        return df.sort_values("index").reset_index(drop=True)

    def get_smc_context(self) -> Optional[SMCContext]:
        """Return the most recently computed SMCContext."""
        return self._smc_context

    def get_current_structure(self) -> dict:
        return {
            "swing_highs_count": len(self.swing_highs),
            "swing_lows_count": len(self.swing_lows),
            "last_swing_high": self.swing_highs[-1] if self.swing_highs else None,
            "last_swing_low": self.swing_lows[-1] if self.swing_lows else None,
            "current_trend": self.current_trend,
            "structure_events_count": len(self.structure_events),
            "last_structure_event": self.structure_events[-1] if self.structure_events else None,
        }

    def get_recent_swing_high(self) -> Optional[SwingPoint]:
        return self.swing_highs[-1] if self.swing_highs else None

    def get_recent_swing_low(self) -> Optional[SwingPoint]:
        return self.swing_lows[-1] if self.swing_lows else None

    def get_structure_events(self, limit: int = 10) -> list[StructureEvent]:
        return self.structure_events[-limit:] if self.structure_events else []

    def _find_nearest_swing(self, target_index: int, is_high: bool) -> Optional[SwingPoint]:
        swings = self.swing_highs if is_high else self.swing_lows
        if not swings:
            return None
        return min(swings, key=lambda s: abs(s.index - target_index))

    # ------------------------------------------------------------------
    # Internal: sync state from SMCContext
    # ------------------------------------------------------------------

    def _sync_from_context(self, df: pd.DataFrame) -> None:
        ctx = self._smc_context
        if ctx is None:
            return

        self.swing_highs = [self._to_swing(s, df) for s in ctx.swing_highs]
        self.swing_lows = [self._to_swing(s, df) for s in ctx.swing_lows]

        # ctx stores most-recent-first; restore chronological order
        self.structure_events = [self._to_event(e, df) for e in reversed(ctx.structure_events)]

        self.current_trend = _phase_to_trend(ctx.phase, ctx.trend_bias)

        # Fallback: derive trend from swing HH/HL or LH/LL pattern
        if self.current_trend == TrendDirection.RANGING:
            self._determine_trend_from_swings()

    def _to_swing(self, s: CoreSwingPoint, df: pd.DataFrame) -> SwingPoint:
        idx = min(s.index, len(df) - 1)
        ts = _to_ts(df.index[idx])
        return SwingPoint(
            index=s.index,
            price=Decimal(str(round(s.price, 8))),
            timestamp=ts,
            is_high=s.is_high,
            strength=s.strength,
        )

    def _to_event(self, e: CoreStructureEvent, df: pd.DataFrame) -> StructureEvent:
        idx = min(e.index, len(df) - 1)
        ts = _to_ts(df.index[idx])
        is_choch = e.is_choch
        event_type = StructureBreak.CHOCH if is_choch else StructureBreak.BOS
        trend = TrendDirection.BULLISH if e.is_bullish else TrendDirection.BEARISH
        broken = e.broken_swing
        prev_swing = SwingPoint(
            index=broken.index,
            price=Decimal(str(round(broken.price, 8))),
            timestamp=ts,
            is_high=broken.is_high,
            strength=broken.strength,
        )
        return StructureEvent(
            event_type=event_type,
            index=e.index,
            price=Decimal(str(round(e.break_price, 8))),
            timestamp=ts,
            previous_swing=prev_swing,
            current_trend=trend,
        )

    def _determine_trend_from_swings(self) -> None:
        if len(self.swing_highs) < 2 or len(self.swing_lows) < 2:
            self.current_trend = TrendDirection.RANGING
            return
        last_h, prev_h = self.swing_highs[-1], self.swing_highs[-2]
        last_l, prev_l = self.swing_lows[-1], self.swing_lows[-2]
        if last_h.price > prev_h.price and last_l.price > prev_l.price:
            self.current_trend = TrendDirection.BULLISH
        elif last_h.price < prev_h.price and last_l.price < prev_l.price:
            self.current_trend = TrendDirection.BEARISH
        else:
            self.current_trend = TrendDirection.RANGING

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


def _phase_to_trend(phase: SMCPhase, bias: float = 0.0) -> TrendDirection:
    if phase == SMCPhase.BULL_TREND:
        return TrendDirection.BULLISH
    if phase == SMCPhase.BEAR_TREND:
        return TrendDirection.BEARISH
    if phase == SMCPhase.ACCUMULATION:
        return TrendDirection.BULLISH if bias >= 0 else TrendDirection.RANGING
    if phase == SMCPhase.DISTRIBUTION:
        return TrendDirection.BEARISH if bias <= 0 else TrendDirection.RANGING
    return TrendDirection.RANGING
