"""
Simulated clock for accelerated replay.

Patches time.monotonic, time.time, datetime.now, datetime.utcnow, and
asyncio.sleep so that the BotOrchestrator (and everything it calls) sees
simulated timestamps while wall-clock time advances near-instantly.
"""

import asyncio
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch


class SimulatedClock:
    """Virtual clock that can be advanced programmatically."""

    def __init__(self, start_time: float) -> None:
        """
        Args:
            start_time: Initial simulated UTC timestamp (seconds since epoch).
        """
        self.current_time: float = start_time
        self._monotonic_base: float = 0.0  # monotonic starts at 0

    def advance(self, seconds: float) -> None:
        """Move clock forward by *seconds*."""
        self.current_time += seconds
        self._monotonic_base += seconds

    def advance_to(self, timestamp: float) -> None:
        """Jump to a specific UTC timestamp (must be >= current)."""
        delta = timestamp - self.current_time
        if delta > 0:
            self.advance(delta)

    def now(self, tz: timezone | None = None) -> datetime:
        """Return current simulated datetime (UTC)."""
        dt = datetime.fromtimestamp(self.current_time, tz=timezone.utc)
        if tz is not None and tz is not timezone.utc:
            dt = dt.astimezone(tz)
        return dt

    def monotonic(self) -> float:
        """Simulated monotonic clock value."""
        return self._monotonic_base

    def time(self) -> float:
        """Simulated time.time() value."""
        return self.current_time


def _make_fake_datetime_class(clock: SimulatedClock):
    """
    Create a ``datetime`` subclass whose ``now()`` and ``utcnow()``
    class methods return simulated time from *clock*.

    This is needed because ``datetime.datetime`` is a C extension type
    and its attributes are immutable in Python 3.12+.  By replacing the
    *module-level* ``datetime`` name with this subclass, all call-sites
    that do ``datetime.now(...)`` will get simulated time.
    """

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return datetime.fromtimestamp(clock.current_time, tz=timezone.utc).replace(
                    tzinfo=None
                )
            return datetime.fromtimestamp(clock.current_time, tz=tz)

        @classmethod
        def utcnow(cls):
            return datetime.fromtimestamp(clock.current_time, tz=timezone.utc).replace(tzinfo=None)

    return FakeDatetime


# Modules whose ``datetime`` name should be replaced with FakeDatetime.
# Only modules used in the BotOrchestrator hot path need patching.
_DATETIME_MODULES = [
    "bot.orchestrator.bot_orchestrator",
    "bot.orchestrator.health_monitor",
    "bot.orchestrator.events",
    "bot.orchestrator.strategy_registry",
    "bot.orchestrator.market_regime",
    "bot.orchestrator.strategy_selector",
    "bot.orchestrator.state_persistence",
    "bot.database.models",
]


@contextmanager
def patch_time(clock: SimulatedClock):
    """
    Context manager that monkey-patches stdlib time functions and asyncio.sleep
    to use *clock* instead of real wall-clock time.

    Inside the block:
    - ``time.monotonic()`` → ``clock.monotonic()``
    - ``time.time()`` → ``clock.time()``
    - ``datetime.now(tz)`` → simulated time
    - ``datetime.utcnow()`` → simulated time (naive UTC)
    - ``asyncio.sleep(n)`` → advance clock by *n* seconds, then yield control
    """

    # -- time module patches (time is a C module but its functions are patchable) --

    _real_monotonic = time.monotonic
    _real_time = time.time

    time.monotonic = clock.monotonic  # type: ignore[assignment]
    time.time = clock.time  # type: ignore[assignment]

    # -- asyncio.sleep patch -----------------------------------------------

    _real_sleep = asyncio.sleep

    async def _fake_sleep(seconds, result=None):
        """Advance simulated clock by *seconds*, then yield control."""
        if seconds > 0:
            clock.advance(seconds)
        await _real_sleep(0)
        return result

    asyncio.sleep = _fake_sleep  # type: ignore[assignment]

    # -- datetime patches (module-level name replacement) ------------------

    FakeDatetime = _make_fake_datetime_class(clock)
    _saved: dict[str, object] = {}

    for modname in _DATETIME_MODULES:
        mod = sys.modules.get(modname)
        if mod is not None and hasattr(mod, "datetime"):
            _saved[modname] = getattr(mod, "datetime")
            setattr(mod, "datetime", FakeDatetime)

    try:
        yield clock
    finally:
        # Restore everything
        time.monotonic = _real_monotonic  # type: ignore[assignment]
        time.time = _real_time  # type: ignore[assignment]
        asyncio.sleep = _real_sleep  # type: ignore[assignment]

        for modname, original in _saved.items():
            mod = sys.modules.get(modname)
            if mod is not None:
                setattr(mod, "datetime", original)
