"""
Tests for bot/core/smc/imbalance_detector.py
"""

import numpy as np

from bot.core.smc.imbalance_detector import ImbalanceDetector
from bot.core.smc.models import (
    FVGType,
    OBType,
    StructureEvent,
    StructureType,
    SwingPoint,
    SwingType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _swing(i, price, swing_type):
    return SwingPoint(index=i, price=price, swing_type=swing_type, strength=5)


def _struct_event(index, stype, break_price, broken_swing, impulse=10.0):
    return StructureEvent(
        index=index,
        structure_type=stype,
        break_price=break_price,
        broken_swing=broken_swing,
        impulse_size=impulse,
    )


def _flat_ohlcv(n: int, base: float = 100.0):
    open_ = np.full(n, base)
    high = np.full(n, base + 1)
    low = np.full(n, base - 1)
    close = np.full(n, base)
    return open_, high, low, close


# ---------------------------------------------------------------------------
# FVG tests
# ---------------------------------------------------------------------------


class TestFVGDetection:
    def setup_method(self):
        self.det = ImbalanceDetector(min_fvg_atr=0.0)

    def test_bull_fvg_detected(self):
        """high[i-2] < low[i] → bull FVG."""
        n = 10
        open_, high, low, close = _flat_ohlcv(n)
        high[4] = 100.0
        low[6] = 103.0  # gap: [100, 103]
        high[6] = 105.0
        fvgs = self.det.detect_fvg(open_, high, low, close)
        bull = [f for f in fvgs if f.fvg_type == FVGType.BULL]
        assert any(abs(f.gap_low - 100.0) < 1e-6 for f in bull)

    def test_bear_fvg_detected(self):
        """low[i-2] > high[i] → bear FVG."""
        n = 10
        open_, high, low, close = _flat_ohlcv(n)
        low[4] = 110.0
        high[6] = 107.0  # gap: [107, 110]
        fvgs = self.det.detect_fvg(open_, high, low, close)
        bear = [f for f in fvgs if f.fvg_type == FVGType.BEAR]
        assert any(abs(f.gap_high - 110.0) < 1e-6 for f in bear)

    def test_no_fvg_on_flat(self):
        n = 20
        open_, high, low, close = _flat_ohlcv(n)
        fvgs = self.det.detect_fvg(open_, high, low, close)
        assert fvgs == []

    def test_min_fvg_atr_filter(self):
        det = ImbalanceDetector(min_fvg_atr=5.0)
        n = 10
        open_, high, low, close = _flat_ohlcv(n)
        atr = np.full(n, 10.0)
        high[4] = 100.0
        low[6] = 101.0  # gap = 1.0 < 5.0 * 10 = 50 → filtered
        high[6] = 105.0
        fvgs = det.detect_fvg(open_, high, low, close, atr)
        assert fvgs == []

    def test_insufficient_bars_returns_empty(self):
        open_, high, low, close = _flat_ohlcv(2)
        assert self.det.detect_fvg(open_, high, low, close) == []

    def test_fvg_size_correct(self):
        n = 10
        open_, high, low, close = _flat_ohlcv(n)
        high[4] = 100.0
        low[6] = 105.0
        high[6] = 107.0
        fvgs = self.det.detect_fvg(open_, high, low, close)
        bull = [f for f in fvgs if f.fvg_type == FVGType.BULL]
        assert any(abs(f.size - 5.0) < 1e-6 for f in bull)

    def test_filled_fvg_marked(self):
        """If current price is inside the gap, filled=True."""
        n = 10
        open_, high, low, close = _flat_ohlcv(n, base=102.0)  # last close inside gap
        open_[:] = 100.0
        high[4] = 100.0
        low[6] = 103.0
        high[6] = 105.0
        close[-1] = 101.5  # inside [100, 103]
        fvgs = self.det.detect_fvg(open_, high, low, close)
        bull = [f for f in fvgs if f.fvg_type == FVGType.BULL and f.gap_low == 100.0]
        assert any(f.filled for f in bull)


# ---------------------------------------------------------------------------
# Order Block tests
# ---------------------------------------------------------------------------


class TestOBDetection:
    def setup_method(self):
        self.det = ImbalanceDetector(min_fvg_atr=0.0, max_ob_lookback=10)

    def _bull_event(self, bar: int) -> StructureEvent:
        broken = _swing(bar - 2, 100.0, SwingType.HIGH)
        return _struct_event(bar, StructureType.BOS_BULL, 105.0, broken)

    def _bear_event(self, bar: int) -> StructureEvent:
        broken = _swing(bar - 2, 100.0, SwingType.LOW)
        return _struct_event(bar, StructureType.BOS_BEAR, 95.0, broken)

    def test_bull_ob_found_before_bull_event(self):
        """Should find last bearish candle before bullish structural event."""
        n = 20
        open_, high, low, close = _flat_ohlcv(n, base=100.0)
        # Make bar 8 bearish (close < open)
        open_[8] = 105.0
        close[8] = 102.0
        event = self._bull_event(12)
        obs = self.det.detect_ob(open_, high, low, close, structure_events=[event])
        bull_obs = [o for o in obs if o.ob_type == OBType.BULL]
        assert len(bull_obs) >= 1
        assert any(o.index == 8 for o in bull_obs)

    def test_bear_ob_found_before_bear_event(self):
        """Should find last bullish candle before bearish structural event."""
        n = 20
        open_, high, low, close = _flat_ohlcv(n, base=100.0)
        # Make bar 8 bullish (close > open)
        open_[8] = 95.0
        close[8] = 98.0
        event = self._bear_event(12)
        obs = self.det.detect_ob(open_, high, low, close, structure_events=[event])
        bear_obs = [o for o in obs if o.ob_type == OBType.BEAR]
        assert len(bear_obs) >= 1
        assert any(o.index == 8 for o in bear_obs)

    def test_no_events_returns_empty(self):
        n = 20
        open_, high, low, close = _flat_ohlcv(n)
        obs = self.det.detect_ob(open_, high, low, close, structure_events=[])
        assert obs == []

    def test_invalidated_ob_flagged(self):
        """An OB whose zone has been closed through should be flagged invalidated."""
        n = 20
        open_ = np.full(n, 100.0)
        high = np.full(n, 101.0)
        low = np.full(n, 99.0)
        close = np.full(n, 100.0)
        # Bar 8: bearish
        open_[8] = 105.0
        close[8] = 102.0
        high[8] = 106.0
        low[8] = 101.5
        # Last close drops below OB low (99.0 base)
        close[-1] = 90.0
        low[-1] = 90.0
        event = self._bull_event(12)
        obs = self.det.detect_ob(open_, high, low, close, structure_events=[event])
        # All OBs whose low was above 90 are still valid (low[8] = 101.5 > 90)
        # Invalidation = close < ob.low for BULL OB
        bull_obs = [o for o in obs if o.ob_type == OBType.BULL]
        assert any(o.invalidated for o in bull_obs)
