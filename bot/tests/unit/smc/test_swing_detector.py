"""
Tests for bot/core/smc/swing_detector.py
"""

import numpy as np

from bot.core.smc.models import SwingType
from bot.core.smc.swing_detector import find_swing_highs, find_swing_lows, find_swing_points

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_zigzag(n: int = 50, amplitude: float = 100.0) -> tuple[np.ndarray, np.ndarray]:
    """Alternating peaks and troughs, each 10 bars apart."""
    price = np.zeros(n)
    for i in range(n):
        price[i] = amplitude * np.sin(2 * np.pi * i / 10)
    high = price.copy()
    low = price.copy()
    high[price > 0] += 5
    low[price < 0] -= 5
    return high, low


# ---------------------------------------------------------------------------
# find_swing_points
# ---------------------------------------------------------------------------


class TestFindSwingPoints:
    def test_returns_empty_when_insufficient_bars(self):
        high = np.array([1.0, 2.0, 3.0])
        low = np.array([0.5, 1.0, 1.5])
        # strength=5 requires 11 bars
        result = find_swing_points(high, low, strength=5)
        assert result == []

    def test_detects_obvious_peak(self):
        # Flat series with one clear peak in the middle
        high = np.array([1.0] * 20, dtype=float)
        low = np.array([0.9] * 20, dtype=float)
        high[10] = 2.0  # peak
        low[10] = 0.8
        result = find_swing_points(high, low, strength=5)
        highs = [s for s in result if s.is_high]
        assert any(s.index == 10 for s in highs), "Peak at index 10 not found"

    def test_detects_obvious_trough(self):
        high = np.array([1.0] * 20, dtype=float)
        low = np.array([0.9] * 20, dtype=float)
        low[10] = 0.2  # trough
        result = find_swing_points(high, low, strength=5)
        lows = [s for s in result if s.is_low]
        assert any(s.index == 10 for s in lows), "Trough at index 10 not found"

    def test_sorted_by_index(self):
        high, low = _make_zigzag(60)
        result = find_swing_points(high, low, strength=4)
        indices = [s.index for s in result]
        assert indices == sorted(indices)

    def test_swing_types_are_correct(self):
        high, low = _make_zigzag(60)
        result = find_swing_points(high, low, strength=4)
        for s in result:
            if s.swing_type == SwingType.HIGH:
                assert s.is_high
                assert not s.is_low
            else:
                assert s.is_low
                assert not s.is_high

    def test_strength_attribute_preserved(self):
        high = np.array([1.0] * 20, dtype=float)
        low = np.array([0.9] * 20, dtype=float)
        high[10] = 2.0
        result = find_swing_points(high, low, strength=5)
        for s in result:
            assert s.strength == 5

    def test_constant_series_returns_all_as_swings(self):
        high = np.ones(20)
        low = np.ones(20)
        result = find_swing_points(high, low, strength=3)
        # Every interior bar qualifies (ties are allowed)
        assert len(result) > 0


class TestConvenienceWrappers:
    def test_find_swing_highs_returns_only_highs(self):
        high, low = _make_zigzag(60)
        result = find_swing_highs(high, low, strength=4)
        assert all(s.is_high for s in result)

    def test_find_swing_lows_returns_only_lows(self):
        high, low = _make_zigzag(60)
        result = find_swing_lows(high, low, strength=4)
        assert all(s.is_low for s in result)
