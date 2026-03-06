"""
bot/core/smc/structural_detector.py — BOS and CHoCH detection.

Algorithm:
  - Maintain running series of confirmed swing highs (SH) and swing lows (SL).
  - Trend state starts as UNKNOWN. Once we have at least one HH+HL (bullish) or
    LH+LL (bearish) sequence the state transitions.
  - BOS_BULL  : price closes above the most recent SH while in a bull trend (continuation).
  - BOS_BEAR  : price closes below the most recent SL while in a bear trend.
  - CHOCH_BULL: price closes above the most recent SH while in a bear trend (reversal).
  - CHOCH_BEAR: price closes below the most recent SL while in a bull trend.
"""

from __future__ import annotations

from enum import Enum, auto

import numpy as np

from bot.core.smc.models import (
    StructureEvent,
    StructureType,
    SwingPoint,
    SwingType,
)
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class _TrendState(Enum):
    UNKNOWN = auto()
    BULL = auto()
    BEAR = auto()


class StructuralDetector:
    """
    Stateless-per-call detector: given a full list of confirmed swing points
    and the close price series, returns all structural events in chronological
    order.

    Usage
    -----
    detector = StructuralDetector(min_impulse_atr=0.5)
    events = detector.detect(swing_points, close, atr)
    """

    def __init__(self, min_impulse_atr: float = 0.3) -> None:
        """
        Args:
            min_impulse_atr: Minimum impulse size as a multiple of ATR to
                             qualify a break as a structural event. Filters
                             noise on tiny breaks.
        """
        self.min_impulse_atr = min_impulse_atr

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(
        self,
        swing_points: list[SwingPoint],
        close: np.ndarray,
        atr: np.ndarray | None = None,
    ) -> list[StructureEvent]:
        """
        Scan all swing points and detect BOS / CHoCH events.

        Args:
            swing_points: All confirmed swing points, sorted by index ascending.
            close:        Full close-price array aligned with OHLCV bars.
            atr:          ATR array (same length as close). If None, impulse
                          filtering by ATR is skipped.

        Returns:
            List of StructureEvent, sorted by index ascending (oldest first).
        """
        if len(swing_points) < 2:
            return []

        highs = [sp for sp in swing_points if sp.is_high]
        lows = [sp for sp in swing_points if sp.is_low]

        if not highs or not lows:
            return []

        events: list[StructureEvent] = []
        state = _TrendState.UNKNOWN

        # Pointers into highs / lows lists
        hi_ptr = 0
        lo_ptr = 0

        # Track the "active" reference swing that price must break
        prev_sh: SwingPoint | None = None
        prev_sl: SwingPoint | None = None

        # Walk bar-by-bar from the first confirmed swing onwards
        all_swings = sorted(swing_points, key=lambda s: s.index)
        first_bar = all_swings[0].index
        last_bar = len(close) - 1

        # Seed initial state from first two opposing swings
        if highs and lows:
            prev_sh = highs[hi_ptr]
            prev_sl = lows[lo_ptr]
            # Advance pointers past the first swing on each side
            hi_ptr = 1
            lo_ptr = 1

            # Decide initial bias from which came first
            if prev_sh.index < prev_sl.index:
                state = _TrendState.BULL
            else:
                state = _TrendState.BEAR

        def _atr_at(bar: int) -> float:
            if atr is None:
                return 1.0  # no filter
            idx = min(bar, len(atr) - 1)
            val = float(atr[idx])
            return val if val > 0 else 1.0

        for bar in range(first_bar, last_bar + 1):
            c = close[bar]

            # Advance pointers to keep prev_sh / prev_sl up to date
            while hi_ptr < len(highs) and highs[hi_ptr].index <= bar:
                prev_sh = highs[hi_ptr]
                hi_ptr += 1

            while lo_ptr < len(lows) and lows[lo_ptr].index <= bar:
                prev_sl = lows[lo_ptr]
                lo_ptr += 1

            if prev_sh is None or prev_sl is None:
                continue

            atr_val = _atr_at(bar)

            # --- Check upside break (above prev_sh) ---
            if c > prev_sh.price:
                impulse = c - prev_sh.price
                if impulse < self.min_impulse_atr * atr_val:
                    continue  # too small

                if state == _TrendState.BEAR:
                    stype = StructureType.CHOCH_BULL
                    state = _TrendState.BULL
                elif state == _TrendState.BULL:
                    stype = StructureType.BOS_BULL
                else:
                    stype = StructureType.BOS_BULL
                    state = _TrendState.BULL

                events.append(
                    StructureEvent(
                        index=bar,
                        structure_type=stype,
                        break_price=c,
                        broken_swing=prev_sh,
                        impulse_size=impulse,
                    )
                )
                logger.debug("structure_event", type=stype.value, bar=bar, price=round(c, 4))

                # After a break, the broken swing becomes the new reference
                prev_sh = SwingPoint(index=bar, price=c, swing_type=SwingType.HIGH, strength=0)

            # --- Check downside break (below prev_sl) ---
            elif c < prev_sl.price:
                impulse = prev_sl.price - c
                if impulse < self.min_impulse_atr * atr_val:
                    continue

                if state == _TrendState.BULL:
                    stype = StructureType.CHOCH_BEAR
                    state = _TrendState.BEAR
                elif state == _TrendState.BEAR:
                    stype = StructureType.BOS_BEAR
                else:
                    stype = StructureType.BOS_BEAR
                    state = _TrendState.BEAR

                events.append(
                    StructureEvent(
                        index=bar,
                        structure_type=stype,
                        break_price=c,
                        broken_swing=prev_sl,
                        impulse_size=impulse,
                    )
                )
                logger.debug("structure_event", type=stype.value, bar=bar, price=round(c, 4))

                prev_sl = SwingPoint(index=bar, price=c, swing_type=SwingType.LOW, strength=0)

        logger.debug(
            "structural_detection_complete",
            total=len(events),
            bos=sum(1 for e in events if not e.is_choch),
            choch=sum(1 for e in events if e.is_choch),
        )
        return events

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @staticmethod
    def latest_bias(events: list[StructureEvent]) -> float:
        """
        Derive trend bias from the most recent structural event.
        Returns +1.0 (bull), -1.0 (bear), or 0.0 (no events).
        """
        if not events:
            return 0.0
        last = events[-1]
        return 1.0 if last.is_bullish else -1.0
