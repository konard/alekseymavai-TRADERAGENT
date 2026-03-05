"""
bot/core/smc/imbalance_detector.py — Fair Value Gap and Order Block detection.

Fair Value Gap (FVG)
--------------------
Three-candle pattern:
  BULL FVG: high[i-2] < low[i]   → gap between top of candle i-2 and bottom of candle i
  BEAR FVG: low[i-2]  > high[i]  → gap between bottom of candle i-2 and top of candle i

The gap zone = [low[i], high[i-2]] for BULL, [high[i], low[i-2]] for BEAR.

Order Block (OB)
----------------
The last opposing candle before an impulse move that created a structural event.
  BULL OB: last bearish candle (close < open) before a bullish BOS/CHoCH
  BEAR OB: last bullish candle (close > open) before a bearish BOS/CHoCH
"""
from __future__ import annotations

import numpy as np

from bot.core.smc.models import (
    FairValueGap,
    FVGType,
    OBType,
    OrderBlock,
    StructureEvent,
    StructureType,
)
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class ImbalanceDetector:
    """
    Detects FVGs and Order Blocks from OHLCV data plus structural events.

    Usage
    -----
    detector = ImbalanceDetector(min_fvg_atr=0.2, max_ob_lookback=20)
    fvgs = detector.detect_fvg(open_, high, low, close, atr)
    obs  = detector.detect_ob(open_, high, low, close, atr, structure_events)
    """

    def __init__(
        self,
        min_fvg_atr: float = 0.2,
        max_ob_lookback: int = 20,
        max_fvg_count: int = 10,
        max_ob_count: int = 10,
    ) -> None:
        """
        Args:
            min_fvg_atr:     Minimum FVG size as a multiple of ATR (filter noise).
            max_ob_lookback: How far back (bars) to search for the OB candle.
            max_fvg_count:   Keep at most this many recent FVGs.
            max_ob_count:    Keep at most this many recent OBs.
        """
        self.min_fvg_atr = min_fvg_atr
        self.max_ob_lookback = max_ob_lookback
        self.max_fvg_count = max_fvg_count
        self.max_ob_count = max_ob_count

    # ------------------------------------------------------------------
    # FVG detection
    # ------------------------------------------------------------------

    def detect_fvg(
        self,
        open_: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        atr: np.ndarray | None = None,
    ) -> list[FairValueGap]:
        """
        Scan the full OHLCV series for Fair Value Gaps.

        Returns list sorted by index ascending (oldest first),
        capped at max_fvg_count most recent.
        """
        n = len(close)
        if n < 3:
            return []

        results: list[FairValueGap] = []

        for i in range(2, n):
            atr_val = self._atr_at(atr, i)

            # BULL FVG: candle i-2 high < candle i low
            gap_low  = high[i - 2]
            gap_high = low[i]
            if gap_high > gap_low:
                size = gap_high - gap_low
                if size >= self.min_fvg_atr * atr_val:
                    results.append(FairValueGap(
                        index=i - 1,   # middle candle
                        fvg_type=FVGType.BULL,
                        gap_high=float(gap_high),
                        gap_low=float(gap_low),
                        size=float(size),
                    ))

            # BEAR FVG: candle i-2 low > candle i high
            gap_high2 = low[i - 2]
            gap_low2  = high[i]
            if gap_high2 > gap_low2:
                size = gap_high2 - gap_low2
                if size >= self.min_fvg_atr * atr_val:
                    results.append(FairValueGap(
                        index=i - 1,
                        fvg_type=FVGType.BEAR,
                        gap_high=float(gap_high2),
                        gap_low=float(gap_low2),
                        size=float(size),
                    ))

        # Keep only most recent, sorted ascending
        results.sort(key=lambda f: f.index)
        results = results[-self.max_fvg_count:]

        # Mark filled FVGs (price has re-entered the gap) using last close
        last_close = float(close[-1])
        filled_results: list[FairValueGap] = []
        for fvg in results:
            filled = fvg.contains_price(last_close)
            filled_results.append(fvg.model_copy(update={"filled": filled}))

        logger.debug("fvg_detected",
                     total=len(filled_results),
                     bull=sum(1 for f in filled_results if f.fvg_type == FVGType.BULL),
                     bear=sum(1 for f in filled_results if f.fvg_type == FVGType.BEAR))
        return filled_results

    # ------------------------------------------------------------------
    # Order Block detection
    # ------------------------------------------------------------------

    def detect_ob(
        self,
        open_: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        atr: np.ndarray | None = None,
        structure_events: list[StructureEvent] | None = None,
    ) -> list[OrderBlock]:
        """
        Detect Order Blocks anchored to structural events.

        For each BOS/CHoCH, search backwards up to max_ob_lookback bars
        for the last opposing candle.

        Returns list sorted by index ascending, capped at max_ob_count.
        """
        if not structure_events:
            return []

        results: list[OrderBlock] = []

        for event in structure_events:
            ob = self._find_ob_for_event(event, open_, high, low, close, atr)
            if ob is not None:
                results.append(ob)

        # Deduplicate by candle index (keep last event's OB for each candle)
        seen: dict[int, OrderBlock] = {}
        for ob in results:
            seen[ob.index] = ob
        results = sorted(seen.values(), key=lambda o: o.index)
        results = results[-self.max_ob_count:]

        # Mark invalidated OBs (price has closed through the zone)
        last_close = float(close[-1])
        valid_results: list[OrderBlock] = []
        for ob in results:
            invalidated = (
                (ob.ob_type == OBType.BULL and last_close < ob.low) or
                (ob.ob_type == OBType.BEAR and last_close > ob.high)
            )
            touched = ob.contains_price(last_close)
            valid_results.append(ob.model_copy(
                update={"invalidated": invalidated, "touched": touched}
            ))

        logger.debug("ob_detected", total=len(valid_results),
                     bull=sum(1 for o in valid_results if o.ob_type == OBType.BULL),
                     bear=sum(1 for o in valid_results if o.ob_type == OBType.BEAR))
        return valid_results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_ob_for_event(
        self,
        event: StructureEvent,
        open_: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        atr: np.ndarray | None,
    ) -> OrderBlock | None:
        """Find the last opposing candle before the structural event."""
        bar = event.index
        lookback_start = max(0, bar - self.max_ob_lookback)

        if event.structure_type in (StructureType.BOS_BULL, StructureType.CHOCH_BULL):
            # Bullish event → look for last BEARISH candle (close < open)
            ob_type = OBType.BULL
            for i in range(bar - 1, lookback_start - 1, -1):
                if close[i] < open_[i]:   # bearish candle
                    atr_val = self._atr_at(atr, i)
                    return OrderBlock(
                        index=i,
                        ob_type=ob_type,
                        high=float(high[i]),
                        low=float(low[i]),
                        open=float(open_[i]),
                        close=float(close[i]),
                        atr_at_formation=atr_val,
                        structure_event=event,
                    )
        else:
            # Bearish event → look for last BULLISH candle (close > open)
            ob_type = OBType.BEAR
            for i in range(bar - 1, lookback_start - 1, -1):
                if close[i] > open_[i]:   # bullish candle
                    atr_val = self._atr_at(atr, i)
                    return OrderBlock(
                        index=i,
                        ob_type=ob_type,
                        high=float(high[i]),
                        low=float(low[i]),
                        open=float(open_[i]),
                        close=float(close[i]),
                        atr_at_formation=atr_val,
                        structure_event=event,
                    )
        return None

    @staticmethod
    def _atr_at(atr: np.ndarray | None, i: int) -> float:
        if atr is None:
            return 1.0
        idx = min(i, len(atr) - 1)
        val = float(atr[idx])
        return val if val > 0 else 1.0
