"""
bot/core/smc/structure_analyzer.py — SMCStructureAnalyzer: caching real-time service.

Wraps SMCAnalyzer with a per-symbol TTL cache (default 5 minutes) so that
multiple callers (MarketRegimeDetector, BotOrchestrator) share one context
object without redundant re-computation on every tick.

Public API
----------
get_context(symbol, df_h1)  → SMCContext
get_structural_levels(symbol) → list[float]
get_phase(symbol)           → SMCPhase | None
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from bot.core.smc.analyzer import SMCAnalyzer
from bot.core.smc.models import SMCContext, SMCPhase
from bot.utils.logger import get_logger

logger = get_logger(__name__)

# Default TTL for the per-symbol context cache (seconds).
_DEFAULT_TTL_SECONDS: float = 300.0  # 5 minutes


@dataclass
class _CacheEntry:
    """Internal cache record for one symbol."""

    context: SMCContext
    updated_at: float = field(default_factory=time.monotonic)

    def is_stale(self, ttl: float) -> bool:
        return (time.monotonic() - self.updated_at) >= ttl


class SMCStructureAnalyzer:
    """
    Caching wrapper around SMCAnalyzer for live-bot use.

    Keeps one SMCContext per symbol, refreshed at most every ``ttl_seconds``.
    Thread-unsafe (designed for single-threaded asyncio event loop).

    Parameters
    ----------
    ttl_seconds : float
        Maximum age of a cached context before it is recomputed.
    **analyzer_kwargs
        Forwarded verbatim to SMCAnalyzer.__init__ (e.g. ``swing_strength``,
        ``min_warmup_bars``, ``min_impulse_atr``).
    """

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        **analyzer_kwargs: Any,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._analyzer = SMCAnalyzer(**analyzer_kwargs)
        self._cache: dict[str, _CacheEntry] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_context(self, symbol: str, df_h1: pd.DataFrame) -> SMCContext:
        """
        Return a fresh (or cached) SMCContext for *symbol*.

        If the cached entry is younger than ``ttl_seconds`` it is returned
        immediately without re-running the analysis.  Otherwise the analyzer
        is called and the cache is updated.

        Args:
            symbol: Trading pair identifier (e.g. "BTC/USDT").  Used as cache
                    key only — SMCAnalyzer itself is symbol-agnostic.
            df_h1:  OHLCV DataFrame with columns open/high/low/close/volume.

        Returns:
            SMCContext — the most recent (possibly cached) analysis result.
        """
        entry = self._cache.get(symbol)
        if entry is not None and not entry.is_stale(self.ttl_seconds):
            logger.debug("smc_structure_cache_hit", symbol=symbol)
            return entry.context

        context = self._analyzer.analyze(df_h1)
        self._cache[symbol] = _CacheEntry(context=context)

        logger.debug(
            "smc_structure_cache_updated",
            symbol=symbol,
            phase=context.phase.value,
            warmup=context.warmup_complete,
            bars=context.bars_analyzed,
        )
        return context

    def get_structural_levels(self, symbol: str) -> list[float]:
        """
        Return the break-prices of all cached structural events for *symbol*.

        Structural levels are the price levels at which BOS/CHoCH events were
        confirmed.  They represent key support/resistance for SMC strategies.

        Returns an empty list when no cached context exists for the symbol.
        """
        entry = self._cache.get(symbol)
        if entry is None:
            return []
        return [ev.break_price for ev in entry.context.structure_events]

    def get_phase(self, symbol: str) -> SMCPhase | None:
        """
        Return the cached SMC phase for *symbol*, or None if not yet computed.
        """
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        return entry.context.phase

    def invalidate(self, symbol: str) -> None:
        """Remove the cached context for *symbol*, forcing a recompute on the next call."""
        self._cache.pop(symbol, None)

    def invalidate_all(self) -> None:
        """Clear the entire cache."""
        self._cache.clear()

    @property
    def cached_symbols(self) -> list[str]:
        """Return the list of symbols that currently have a cache entry."""
        return list(self._cache.keys())
