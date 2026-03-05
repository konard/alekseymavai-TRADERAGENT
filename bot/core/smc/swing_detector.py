"""
bot/core/smc/swing_detector.py — Swing point detection.

Uses a symmetric window: a bar i is a swing HIGH if it is the maximum
over [i-strength, i+strength], and a swing LOW if it is the minimum.
Requires at least (2*strength + 1) bars.

Vectorised with numpy; O(n) after the initial rolling max/min.
"""
from __future__ import annotations

import numpy as np

from bot.core.smc.models import SwingPoint, SwingType
from bot.utils.logger import get_logger

logger = get_logger(__name__)

MIN_BARS = 11   # absolute floor (2*5 + 1)


def find_swing_points(
    high: np.ndarray,
    low: np.ndarray,
    strength: int = 5,
) -> list[SwingPoint]:
    """
    Detect confirmed swing highs and lows.

    A bar i is a swing HIGH  iff  high[i] == max(high[i-s : i+s+1])
    A bar i is a swing LOW   iff  low[i]  == min(low[i-s  : i+s+1])

    Bars within `strength` of either end cannot be confirmed and are skipped.

    Args:
        high:     Array of high prices.
        low:      Array of low prices.
        strength: Number of bars on each side required for confirmation.

    Returns:
        List of SwingPoint, sorted by index ascending.
    """
    n = len(high)
    required = 2 * strength + 1
    if n < required:
        logger.debug("swing_detector_insufficient_bars", bars=n, required=required)
        return []

    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)

    # Rolling window max/min (size = 2*strength+1, centred)
    from numpy.lib.stride_tricks import sliding_window_view

    win = 2 * strength + 1
    # Only interior bars [strength .. n-strength-1] can be confirmed
    interior_high = sliding_window_view(high, win)   # shape: (n - win + 1, win)
    interior_low  = sliding_window_view(low,  win)

    window_max = interior_high.max(axis=1)   # shape: (n - win + 1,)
    window_min = interior_low.min(axis=1)

    # Interior index i (relative to original array) = i_win + strength
    results: list[SwingPoint] = []
    for i_win in range(len(window_max)):
        i = i_win + strength
        centre_high = high[i]
        centre_low  = low[i]

        if centre_high == window_max[i_win]:
            results.append(SwingPoint(index=i, price=centre_high,
                                      swing_type=SwingType.HIGH, strength=strength))

        if centre_low == window_min[i_win]:
            results.append(SwingPoint(index=i, price=centre_low,
                                      swing_type=SwingType.LOW, strength=strength))

    results.sort(key=lambda sp: sp.index)
    logger.debug("swing_points_found", count=len(results),
                 highs=sum(1 for s in results if s.is_high),
                 lows=sum(1 for s in results if s.is_low))
    return results


def find_swing_highs(high: np.ndarray, low: np.ndarray,
                     strength: int = 5) -> list[SwingPoint]:
    """Convenience wrapper — returns only swing highs."""
    return [s for s in find_swing_points(high, low, strength) if s.is_high]


def find_swing_lows(high: np.ndarray, low: np.ndarray,
                    strength: int = 5) -> list[SwingPoint]:
    """Convenience wrapper — returns only swing lows."""
    return [s for s in find_swing_points(high, low, strength) if s.is_low]
