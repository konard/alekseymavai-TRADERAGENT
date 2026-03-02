"""Tests for TimeProvider abstraction (Phase 2)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from bot.core.trading_core import BacktestTimeProvider, LiveTimeProvider, TimeProvider

UTC = timezone.utc


class TestLiveTimeProvider:
    """Verify LiveTimeProvider delegates to real system clocks."""

    def test_is_time_provider(self) -> None:
        provider = LiveTimeProvider()
        assert isinstance(provider, TimeProvider)

    def test_now_returns_utc_datetime(self) -> None:
        provider = LiveTimeProvider()
        now = provider.now()
        assert isinstance(now, datetime)
        assert now.tzinfo is not None
        assert now.tzinfo == UTC

    def test_now_is_approximately_current(self) -> None:
        provider = LiveTimeProvider()
        before = datetime.now(UTC)
        result = provider.now()
        after = datetime.now(UTC)
        assert before <= result <= after

    def test_monotonic_is_positive(self) -> None:
        provider = LiveTimeProvider()
        assert provider.monotonic() > 0

    def test_monotonic_increases(self) -> None:
        provider = LiveTimeProvider()
        t1 = provider.monotonic()
        t2 = provider.monotonic()
        assert t2 >= t1

    def test_timestamp_matches_now(self) -> None:
        provider = LiveTimeProvider()
        ts = provider.timestamp()
        expected = datetime.now(UTC).timestamp()
        assert abs(ts - expected) < 1.0  # within 1 second


class TestBacktestTimeProvider:
    """Verify BacktestTimeProvider simulates time correctly."""

    def test_is_time_provider(self) -> None:
        provider = BacktestTimeProvider()
        assert isinstance(provider, TimeProvider)

    def test_default_start_is_2020(self) -> None:
        provider = BacktestTimeProvider()
        assert provider.now() == datetime(2020, 1, 1, tzinfo=UTC)

    def test_custom_start(self) -> None:
        start = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
        provider = BacktestTimeProvider(start=start)
        assert provider.now() == start

    def test_naive_start_gets_utc(self) -> None:
        start = datetime(2024, 1, 1)  # no tzinfo
        provider = BacktestTimeProvider(start=start)
        assert provider.now().tzinfo == UTC

    def test_advance_moves_time(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        provider = BacktestTimeProvider(start=start)
        provider.advance(timedelta(minutes=5))
        expected = datetime(2024, 1, 1, 0, 5, tzinfo=UTC)
        assert provider.now() == expected

    def test_advance_accumulates(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        provider = BacktestTimeProvider(start=start)
        for _ in range(3):
            provider.advance(timedelta(minutes=5))
        expected = datetime(2024, 1, 1, 0, 15, tzinfo=UTC)
        assert provider.now() == expected

    def test_advance_bars_m5(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        provider = BacktestTimeProvider(start=start)
        provider.advance_bars(n=12, bar_duration_seconds=300)   # 12 × 5 min = 1 hour
        expected = datetime(2024, 1, 1, 1, 0, tzinfo=UTC)
        assert provider.now() == expected

    def test_advance_bars_m1(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        provider = BacktestTimeProvider(start=start)
        provider.advance_bars(n=60, bar_duration_seconds=60)    # 60 × 1 min = 1 hour
        expected = datetime(2024, 1, 1, 1, 0, tzinfo=UTC)
        assert provider.now() == expected

    def test_advance_negative_raises(self) -> None:
        provider = BacktestTimeProvider()
        with pytest.raises(ValueError, match="positive delta"):
            provider.advance(timedelta(minutes=-1))

    def test_advance_zero_raises(self) -> None:
        provider = BacktestTimeProvider()
        with pytest.raises(ValueError, match="positive delta"):
            provider.advance(timedelta(0))

    def test_set_time_teleports(self) -> None:
        provider = BacktestTimeProvider(start=datetime(2024, 1, 1, tzinfo=UTC))
        target = datetime(2025, 6, 1, tzinfo=UTC)
        provider.set_time(target)
        assert provider.now() == target

    def test_set_time_naive_gets_utc(self) -> None:
        provider = BacktestTimeProvider()
        provider.set_time(datetime(2025, 1, 1))
        assert provider.now().tzinfo == UTC

    def test_monotonic_starts_at_zero(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        provider = BacktestTimeProvider(start=start)
        assert provider.monotonic() == pytest.approx(0.0)

    def test_monotonic_increases_with_advance(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        provider = BacktestTimeProvider(start=start)
        provider.advance(timedelta(minutes=10))
        assert provider.monotonic() == pytest.approx(600.0)

    def test_monotonic_cooldown_simulation(self) -> None:
        """Cooldown logic (monotonic delta >= threshold) works in simulated time."""
        start = datetime(2024, 1, 1, tzinfo=UTC)
        provider = BacktestTimeProvider(start=start)
        cooldown_seconds = 600.0

        t0 = provider.monotonic()
        provider.advance_bars(n=1, bar_duration_seconds=300)  # +5 min
        elapsed = provider.monotonic() - t0
        assert elapsed < cooldown_seconds  # still in cooldown

        provider.advance_bars(n=3, bar_duration_seconds=300)  # +15 min total
        elapsed = provider.monotonic() - t0
        assert elapsed >= cooldown_seconds  # cooldown expired

    def test_timestamp_matches_now(self) -> None:
        start = datetime(2024, 3, 15, 9, 30, tzinfo=UTC)
        provider = BacktestTimeProvider(start=start)
        assert provider.timestamp() == pytest.approx(start.timestamp())
