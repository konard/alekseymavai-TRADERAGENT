"""
Unit tests for bot/core/smc/structure_analyzer.py — SMCStructureAnalyzer.

Coverage targets:
- Cache hit / miss semantics (TTL)
- get_context returns SMCContext
- get_structural_levels / get_phase helpers
- invalidate / invalidate_all
- cached_symbols property
"""

import time
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from bot.core.smc.models import (
    SMCContext,
    SMCPhase,
    StructureEvent,
    StructureType,
    SwingPoint,
    SwingType,
)
from bot.core.smc.structure_analyzer import SMCStructureAnalyzer, _CacheEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_df(n: int = 300, trend: str = "flat") -> pd.DataFrame:
    rng = np.random.default_rng(42)
    if trend == "bull":
        close = 1000 + np.arange(n) * 0.5 + rng.normal(0, 3, n).cumsum()
    elif trend == "bear":
        close = 1000 - np.arange(n) * 0.5 + rng.normal(0, 3, n).cumsum()
    else:
        close = 1000 + rng.normal(0, 3, n).cumsum()
    close = np.clip(close, 10, None)
    high = close + rng.uniform(1, 5, n)
    low = np.clip(close - rng.uniform(1, 5, n), 1, None)
    return pd.DataFrame(
        {
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1000, 5000, n),
        }
    )


def _make_swing_point(
    index: int = 0, price: float = 100.0, swing_type: SwingType = SwingType.HIGH
) -> SwingPoint:
    return SwingPoint(index=index, price=price, swing_type=swing_type)


def _make_structure_event(
    index: int = 10,
    structure_type: StructureType = StructureType.CHOCH_BULL,
    break_price: float = 105.0,
) -> StructureEvent:
    broken_swing = _make_swing_point(index=5, price=100.0, swing_type=SwingType.HIGH)
    return StructureEvent(
        index=index,
        structure_type=structure_type,
        break_price=break_price,
        broken_swing=broken_swing,
        impulse_size=5.0,
    )


def _make_smc_context(
    phase: SMCPhase = SMCPhase.ACCUMULATION,
    warmup: bool = True,
    structural_levels: list[float] | None = None,
) -> SMCContext:
    events = []
    if structural_levels:
        for i, price in enumerate(structural_levels):
            events.append(_make_structure_event(index=i * 10, break_price=price))
    return SMCContext(
        phase=phase,
        trend_bias=0.5 if phase in (SMCPhase.ACCUMULATION, SMCPhase.BULL_TREND) else -0.5,
        warmup_complete=warmup,
        current_price=1000.0,
        bar_index=299,
        bars_analyzed=300,
        structure_events=events,
        structural_levels=structural_levels or [],
    )


# ---------------------------------------------------------------------------
# _CacheEntry tests
# ---------------------------------------------------------------------------


class TestCacheEntry:
    def test_fresh_entry_not_stale(self):
        ctx = _make_smc_context()
        entry = _CacheEntry(context=ctx)
        assert not entry.is_stale(300.0)

    def test_entry_becomes_stale_after_ttl(self):
        ctx = _make_smc_context()
        entry = _CacheEntry(context=ctx, updated_at=time.monotonic() - 301.0)
        assert entry.is_stale(300.0)

    def test_entry_not_stale_just_before_ttl(self):
        ctx = _make_smc_context()
        entry = _CacheEntry(context=ctx, updated_at=time.monotonic() - 299.0)
        assert not entry.is_stale(300.0)


# ---------------------------------------------------------------------------
# SMCStructureAnalyzer basic construction
# ---------------------------------------------------------------------------


class TestSMCStructureAnalyzerInit:
    def test_default_ttl(self):
        analyzer = SMCStructureAnalyzer()
        assert analyzer.ttl_seconds == 300.0

    def test_custom_ttl(self):
        analyzer = SMCStructureAnalyzer(ttl_seconds=60.0)
        assert analyzer.ttl_seconds == 60.0

    def test_no_cached_symbols_initially(self):
        analyzer = SMCStructureAnalyzer()
        assert analyzer.cached_symbols == []


# ---------------------------------------------------------------------------
# get_context — cache miss / hit
# ---------------------------------------------------------------------------


class TestGetContext:
    def setup_method(self):
        # Use a very low TTL so we can test staleness
        self.analyzer = SMCStructureAnalyzer(
            ttl_seconds=300.0, swing_strength=5, min_warmup_bars=50
        )

    def test_returns_smc_context(self):
        df = _make_df(300)
        ctx = self.analyzer.get_context("BTC/USDT", df)
        assert isinstance(ctx, SMCContext)

    def test_symbol_added_to_cache(self):
        df = _make_df(300)
        self.analyzer.get_context("BTC/USDT", df)
        assert "BTC/USDT" in self.analyzer.cached_symbols

    def test_cache_miss_calls_analyzer(self):
        df = _make_df(300)
        with patch.object(
            self.analyzer._analyzer, "analyze", wraps=self.analyzer._analyzer.analyze
        ) as mock_analyze:
            self.analyzer.get_context("BTC/USDT", df)
            assert mock_analyze.call_count == 1

    def test_cache_hit_does_not_rerun_analyzer(self):
        df = _make_df(300)
        with patch.object(
            self.analyzer._analyzer, "analyze", wraps=self.analyzer._analyzer.analyze
        ) as mock_analyze:
            self.analyzer.get_context("BTC/USDT", df)
            self.analyzer.get_context("BTC/USDT", df)
            # Second call should be served from cache
            assert mock_analyze.call_count == 1

    def test_stale_cache_reruns_analyzer(self):
        df = _make_df(300)
        with patch.object(
            self.analyzer._analyzer, "analyze", wraps=self.analyzer._analyzer.analyze
        ) as mock_analyze:
            self.analyzer.get_context("BTC/USDT", df)
            # Manually expire the cache entry
            self.analyzer._cache["BTC/USDT"].updated_at = time.monotonic() - 400.0
            self.analyzer.get_context("BTC/USDT", df)
            assert mock_analyze.call_count == 2

    def test_different_symbols_independent_cache(self):
        df = _make_df(300)
        with patch.object(
            self.analyzer._analyzer, "analyze", wraps=self.analyzer._analyzer.analyze
        ) as mock_analyze:
            self.analyzer.get_context("BTC/USDT", df)
            self.analyzer.get_context("ETH/USDT", df)
            assert mock_analyze.call_count == 2
            assert "BTC/USDT" in self.analyzer.cached_symbols
            assert "ETH/USDT" in self.analyzer.cached_symbols

    def test_second_call_returns_same_context(self):
        df = _make_df(300)
        ctx1 = self.analyzer.get_context("BTC/USDT", df)
        ctx2 = self.analyzer.get_context("BTC/USDT", df)
        assert ctx1 is ctx2  # Same cached object


# ---------------------------------------------------------------------------
# get_structural_levels
# ---------------------------------------------------------------------------


class TestGetStructuralLevels:
    def setup_method(self):
        self.analyzer = SMCStructureAnalyzer(ttl_seconds=300.0)

    def test_empty_when_no_cache(self):
        levels = self.analyzer.get_structural_levels("BTC/USDT")
        assert levels == []

    def test_returns_levels_from_cached_context(self):
        # Manually populate cache with a known context
        events = [
            _make_structure_event(index=10, break_price=105.0),
            _make_structure_event(index=20, break_price=110.0),
        ]
        ctx = SMCContext(
            phase=SMCPhase.ACCUMULATION,
            trend_bias=0.5,
            warmup_complete=True,
            current_price=112.0,
            bar_index=25,
            bars_analyzed=30,
            structure_events=events,
        )
        from bot.core.smc.structure_analyzer import _CacheEntry

        self.analyzer._cache["BTC/USDT"] = _CacheEntry(context=ctx)

        levels = self.analyzer.get_structural_levels("BTC/USDT")
        assert sorted(levels) == sorted([105.0, 110.0])

    def test_returns_empty_when_no_events(self):
        ctx = SMCContext(
            phase=SMCPhase.RANGING,
            warmup_complete=False,
            current_price=100.0,
            bar_index=5,
            bars_analyzed=10,
        )
        from bot.core.smc.structure_analyzer import _CacheEntry

        self.analyzer._cache["BTC/USDT"] = _CacheEntry(context=ctx)
        levels = self.analyzer.get_structural_levels("BTC/USDT")
        assert levels == []


# ---------------------------------------------------------------------------
# get_phase
# ---------------------------------------------------------------------------


class TestGetPhase:
    def setup_method(self):
        self.analyzer = SMCStructureAnalyzer(ttl_seconds=300.0)

    def test_returns_none_when_no_cache(self):
        phase = self.analyzer.get_phase("BTC/USDT")
        assert phase is None

    def test_returns_phase_from_cache(self):
        ctx = _make_smc_context(phase=SMCPhase.DISTRIBUTION)
        from bot.core.smc.structure_analyzer import _CacheEntry

        self.analyzer._cache["BTC/USDT"] = _CacheEntry(context=ctx)
        assert self.analyzer.get_phase("BTC/USDT") == SMCPhase.DISTRIBUTION

    def test_returns_accumulation_phase(self):
        ctx = _make_smc_context(phase=SMCPhase.ACCUMULATION)
        from bot.core.smc.structure_analyzer import _CacheEntry

        self.analyzer._cache["BTC/USDT"] = _CacheEntry(context=ctx)
        assert self.analyzer.get_phase("BTC/USDT") == SMCPhase.ACCUMULATION


# ---------------------------------------------------------------------------
# invalidate / invalidate_all
# ---------------------------------------------------------------------------


class TestInvalidation:
    def setup_method(self):
        self.analyzer = SMCStructureAnalyzer(
            ttl_seconds=300.0, swing_strength=5, min_warmup_bars=50
        )

    def test_invalidate_removes_single_symbol(self):
        df = _make_df(300)
        self.analyzer.get_context("BTC/USDT", df)
        self.analyzer.get_context("ETH/USDT", df)
        self.analyzer.invalidate("BTC/USDT")
        assert "BTC/USDT" not in self.analyzer.cached_symbols
        assert "ETH/USDT" in self.analyzer.cached_symbols

    def test_invalidate_missing_symbol_is_safe(self):
        """invalidate on unknown symbol should not raise."""
        self.analyzer.invalidate("UNKNOWN/USDT")  # should not raise

    def test_invalidate_all_clears_cache(self):
        df = _make_df(300)
        self.analyzer.get_context("BTC/USDT", df)
        self.analyzer.get_context("ETH/USDT", df)
        self.analyzer.invalidate_all()
        assert self.analyzer.cached_symbols == []

    def test_after_invalidate_next_call_recomputes(self):
        df = _make_df(300)
        with patch.object(
            self.analyzer._analyzer, "analyze", wraps=self.analyzer._analyzer.analyze
        ) as mock_analyze:
            self.analyzer.get_context("BTC/USDT", df)
            self.analyzer.invalidate("BTC/USDT")
            self.analyzer.get_context("BTC/USDT", df)
            assert mock_analyze.call_count == 2


# ---------------------------------------------------------------------------
# cached_symbols property
# ---------------------------------------------------------------------------


class TestCachedSymbols:
    def test_empty_initially(self):
        analyzer = SMCStructureAnalyzer()
        assert analyzer.cached_symbols == []

    def test_grows_as_symbols_added(self):
        analyzer = SMCStructureAnalyzer(ttl_seconds=300.0, swing_strength=5, min_warmup_bars=50)
        df = _make_df(300)
        analyzer.get_context("BTC/USDT", df)
        analyzer.get_context("ETH/USDT", df)
        assert set(analyzer.cached_symbols) == {"BTC/USDT", "ETH/USDT"}

    def test_same_symbol_counted_once(self):
        analyzer = SMCStructureAnalyzer(ttl_seconds=300.0, swing_strength=5, min_warmup_bars=50)
        df = _make_df(300)
        analyzer.get_context("BTC/USDT", df)
        analyzer.get_context("BTC/USDT", df)
        assert analyzer.cached_symbols.count("BTC/USDT") == 1


# ---------------------------------------------------------------------------
# SMCContext new fields (confidence, structural_levels)
# ---------------------------------------------------------------------------


class TestSMCContextNewFields:
    """Verify the new SMCContext fields added as part of this issue."""

    def test_confidence_default_zero(self):
        ctx = SMCContext(current_price=100.0, bar_index=0, bars_analyzed=5)
        assert ctx.confidence == 0.0

    def test_structural_levels_default_empty(self):
        ctx = SMCContext(current_price=100.0, bar_index=0, bars_analyzed=5)
        assert ctx.structural_levels == []

    def test_confidence_in_range(self):
        ctx = SMCContext(current_price=100.0, bar_index=0, bars_analyzed=5, confidence=0.75)
        assert 0.0 <= ctx.confidence <= 1.0

    def test_confidence_clamps_at_1(self):
        with pytest.raises(ValidationError):
            # Pydantic ge/le validation should reject > 1.0
            SMCContext(current_price=100.0, bar_index=0, bars_analyzed=5, confidence=1.5)

    def test_structural_levels_set(self):
        ctx = SMCContext(
            current_price=100.0,
            bar_index=0,
            bars_analyzed=5,
            structural_levels=[105.0, 110.0, 95.0],
        )
        assert ctx.structural_levels == [105.0, 110.0, 95.0]

    def test_to_dict_includes_confidence(self):
        ctx = SMCContext(current_price=100.0, bar_index=0, bars_analyzed=5, confidence=0.6)
        d = ctx.to_dict()
        assert "confidence" in d
        assert abs(d["confidence"] - 0.6) < 1e-3

    def test_to_dict_includes_structural_levels(self):
        ctx = SMCContext(
            current_price=100.0,
            bar_index=0,
            bars_analyzed=5,
            structural_levels=[105.0],
        )
        d = ctx.to_dict()
        assert "structural_levels" in d
        assert d["structural_levels"] == [105.0]
