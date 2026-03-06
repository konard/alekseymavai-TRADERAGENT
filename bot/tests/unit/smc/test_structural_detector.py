"""
Tests for bot/core/smc/structural_detector.py

Seeding rule:
  If SH[0].index < SL[0].index → initial state = BULL
  If SL[0].index < SH[0].index → initial state = BEAR
"""

import numpy as np

from bot.core.smc.models import StructureType, SwingPoint, SwingType
from bot.core.smc.structural_detector import StructuralDetector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _swing(index: int, price: float, swing_type: SwingType) -> SwingPoint:
    return SwingPoint(index=index, price=price, swing_type=swing_type, strength=5)


def _bull_swings() -> list[SwingPoint]:
    """SH first → seeds BULL. SH@5(120), SL@10(108), SH@20(140)."""
    return [
        _swing(5, 120.0, SwingType.HIGH),
        _swing(10, 108.0, SwingType.LOW),
        _swing(20, 140.0, SwingType.HIGH),
    ]


def _bear_swings() -> list[SwingPoint]:
    """SL first → seeds BEAR. SL@5(100), SH@10(120), SL@20(90)."""
    return [
        _swing(5, 100.0, SwingType.LOW),
        _swing(10, 120.0, SwingType.HIGH),
        _swing(20, 90.0, SwingType.LOW),
    ]


def _midclose(n: int, lo: float, hi: float, break_bar: int, break_val: float) -> np.ndarray:
    """Flat close between lo and hi, then spike at break_bar."""
    mid = (lo + hi) / 2.0
    close = np.full(n, mid)
    close[break_bar] = break_val
    return close


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStructuralDetector:
    def setup_method(self):
        self.det = StructuralDetector(min_impulse_atr=0.0)

    def test_empty_swing_list_returns_empty(self):
        assert self.det.detect([], np.linspace(100, 120, 50)) == []

    def test_single_swing_returns_empty(self):
        swings = [_swing(5, 100.0, SwingType.HIGH)]
        assert self.det.detect(swings, np.linspace(90, 110, 20)) == []

    def test_bos_bull_detected(self):
        """State=BULL, price breaks above last SH (140) → BOS_BULL."""
        swings = _bull_swings()
        # close between SL(108) and first SH(120), then break above SH(140) at bar 25
        close = _midclose(40, 108.0, 120.0, break_bar=25, break_val=145.0)
        events = self.det.detect(swings, close)
        bos_bull = [e for e in events if e.structure_type == StructureType.BOS_BULL]
        assert len(bos_bull) >= 1

    def test_bos_bear_detected(self):
        """State=BEAR, price breaks below last SL (90) → BOS_BEAR."""
        swings = _bear_swings()
        # close between SL(100) and SH(120), then break below SL(90) at bar 25
        close = _midclose(40, 100.0, 120.0, break_bar=25, break_val=85.0)
        events = self.det.detect(swings, close)
        bos_bear = [e for e in events if e.structure_type == StructureType.BOS_BEAR]
        assert len(bos_bear) >= 1

    def test_choch_bull_detected(self):
        """State=BEAR, price breaks above SH (120) → CHoCH_BULL (reversal)."""
        swings = _bear_swings()
        # close between SL(100) and SH(120), then break above SH(120) at bar 25
        close = _midclose(40, 100.0, 120.0, break_bar=25, break_val=125.0)
        events = self.det.detect(swings, close)
        choch_bull = [e for e in events if e.structure_type == StructureType.CHOCH_BULL]
        assert len(choch_bull) >= 1

    def test_choch_bear_detected(self):
        """State=BULL, price breaks below SL (108) → CHoCH_BEAR (reversal)."""
        swings = _bull_swings()
        # close between SL(108) and first SH(120), then break below SL(108) at bar 25
        close = _midclose(40, 108.0, 120.0, break_bar=25, break_val=104.0)
        events = self.det.detect(swings, close)
        choch_bear = [e for e in events if e.structure_type == StructureType.CHOCH_BEAR]
        assert len(choch_bear) >= 1

    def test_events_sorted_by_index_ascending(self):
        swings = _bull_swings()
        close = _midclose(40, 108.0, 120.0, break_bar=22, break_val=145.0)
        close[35] = 160.0
        events = self.det.detect(swings, close)
        indices = [e.index for e in events]
        assert indices == sorted(indices)

    def test_impulse_filter_removes_tiny_breaks(self):
        det = StructuralDetector(min_impulse_atr=2.0)
        swings = _bear_swings()
        close = _midclose(40, 100.0, 120.0, break_bar=25, break_val=121.0)
        atr = np.full(40, 10.0)
        # impulse = 121 - 120 = 1.0 < 2.0 * 10 = 20 → filtered
        events = det.detect(swings, close, atr)
        choch = [e for e in events if e.structure_type == StructureType.CHOCH_BULL]
        assert len(choch) == 0

    def test_impulse_filter_passes_large_break(self):
        det = StructuralDetector(min_impulse_atr=0.5)
        swings = _bear_swings()
        close = _midclose(40, 100.0, 120.0, break_bar=25, break_val=130.0)
        atr = np.full(40, 1.0)
        events = det.detect(swings, close, atr)
        assert len(events) >= 1

    def test_latest_bias_bull(self):
        swings = _bear_swings()
        close = _midclose(40, 100.0, 120.0, break_bar=25, break_val=125.0)
        events = self.det.detect(swings, close)
        assert StructuralDetector.latest_bias(events) == 1.0

    def test_latest_bias_bear(self):
        swings = _bull_swings()
        close = _midclose(40, 108.0, 120.0, break_bar=25, break_val=104.0)
        events = self.det.detect(swings, close)
        assert StructuralDetector.latest_bias(events) == -1.0

    def test_latest_bias_empty_is_zero(self):
        assert StructuralDetector.latest_bias([]) == 0.0

    def test_bullish_event_references_swing_high(self):
        swings = _bear_swings()
        close = _midclose(40, 100.0, 120.0, break_bar=25, break_val=130.0)
        events = self.det.detect(swings, close)
        for event in events:
            if event.is_bullish:
                assert event.broken_swing.swing_type == SwingType.HIGH

    def test_event_impulse_size_positive(self):
        swings = _bear_swings()
        close = _midclose(40, 100.0, 120.0, break_bar=25, break_val=130.0)
        events = self.det.detect(swings, close)
        for event in events:
            assert event.impulse_size > 0

    def test_no_break_when_price_stays_in_range(self):
        """No late events when price never crosses swing levels after bar 21."""
        swings = _bull_swings()  # SH at 120 and 140, SL at 108
        # Stay strictly between 108 and 120 the whole time
        close = np.full(40, 114.0)
        events = self.det.detect(swings, close)
        # No events after bar 20 (price is always between 108 and 140)
        late = [e for e in events if e.index > 20]
        assert len(late) == 0
