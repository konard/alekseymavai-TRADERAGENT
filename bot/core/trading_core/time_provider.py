"""
TimeProvider — injectable time abstraction for live bot and backtesting.

Allows the backtest engine to control the notion of "current time" without
touching real wall-clock or monotonic clocks, making tests deterministic and
backtests accurate.

Usage (live bot)::

    provider = LiveTimeProvider()
    now = provider.now()        # datetime.now(UTC)
    ts = provider.monotonic()   # time.monotonic()

Usage (backtest)::

    provider = BacktestTimeProvider(start=datetime(2024, 1, 1, tzinfo=UTC))
    provider.advance(timedelta(minutes=5))   # step one M5 bar
    now = provider.now()                     # returns simulated time
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Protocol

UTC = timezone.utc


class TimeProvider(ABC):
    """Abstract time provider — swap for testing and backtesting."""

    @abstractmethod
    def now(self) -> datetime:
        """Return current datetime (UTC)."""
        ...

    @abstractmethod
    def monotonic(self) -> float:
        """Return monotonic time in seconds (equivalent to time.monotonic())."""
        ...

    def timestamp(self) -> float:
        """Return UNIX timestamp for current time."""
        return self.now().timestamp()


class LiveTimeProvider(TimeProvider):
    """
    Production time provider — delegates to real system clocks.

    Both `now()` and `monotonic()` read from the OS, so they always
    reflect wall-clock reality.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class BacktestTimeProvider(TimeProvider):
    """
    Simulated time provider for backtesting.

    Maintains an internal ``_current_time`` that starts at ``start`` and
    advances by calling ``advance()``.  ``monotonic()`` is derived from the
    simulated time so cooldown logic (which uses monotonic deltas) also works
    correctly in backtests.

    Args:
        start: Starting datetime for the simulated clock.
            Defaults to 2020-01-01 00:00 UTC.

    Example::

        provider = BacktestTimeProvider(
            start=datetime(2024, 1, 1, tzinfo=UTC)
        )
        provider.advance(timedelta(minutes=5))   # one M5 bar
        assert provider.now() == datetime(2024, 1, 1, 0, 5, tzinfo=UTC)
    """

    def __init__(
        self,
        start: datetime | None = None,
    ) -> None:
        if start is None:
            start = datetime(2020, 1, 1, tzinfo=UTC)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        self._current_time: datetime = start
        # Derive monotonic from elapsed seconds since epoch-like start
        self._start_ts: float = start.timestamp()

    def now(self) -> datetime:
        return self._current_time

    def monotonic(self) -> float:
        """Return simulated monotonic time (seconds since simulated start)."""
        return self._current_time.timestamp() - self._start_ts

    def advance(self, delta: timedelta) -> None:
        """
        Advance simulated time by *delta*.

        Args:
            delta: How much time to add to the current simulated time.
                   Must be positive.
        """
        if delta.total_seconds() <= 0:
            raise ValueError(f"advance() requires positive delta, got {delta}")
        self._current_time += delta

    def advance_bars(self, n: int, bar_duration_seconds: int = 300) -> None:
        """
        Advance by *n* bars of *bar_duration_seconds* each.

        Args:
            n: Number of bars to advance.
            bar_duration_seconds: Duration of one bar in seconds. Default 300 = M5.
        """
        self.advance(timedelta(seconds=n * bar_duration_seconds))

    def set_time(self, dt: datetime) -> None:
        """Teleport simulated time to an absolute datetime."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        self._current_time = dt
