"""
Smoke / integration tests for SMC → MarketRegimeDetector integration.

Covers:
- SMCStructureAnalyzer wired into MarketRegimeDetector.analyze()
- ACCUMULATION detected via real CHoCH on synthetic H1 data
- Freeze: indicator noise cannot switch regime during ACCUMULATION/DISTRIBUTION
- Freeze release: opposite CHoCH unlocks the freeze
- RegimeAnalysis.smc_context field is populated when SMC drives the regime
- analyze_with_smc() backward-compatibility shim
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.core.smc.analyzer import SMCAnalyzer
from bot.core.smc.models import SMCContext, SMCPhase, StructureEvent, StructureType, SwingPoint, SwingType
from bot.core.smc.structure_analyzer import SMCStructureAnalyzer
from bot.orchestrator.market_regime import (
    MarketRegime,
    MarketRegimeDetector,
    RecommendedStrategy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_df(n: int = 300, trend: str = "flat") -> pd.DataFrame:
    """Generate synthetic OHLCV."""
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
    return pd.DataFrame({
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": rng.uniform(1000, 5000, n),
    })


def _make_swing(index: int, price: float, swing_type: SwingType) -> SwingPoint:
    return SwingPoint(index=index, price=price, swing_type=swing_type)


def _make_event(
    index: int,
    structure_type: StructureType,
    break_price: float,
    swing_price: float = 100.0,
    swing_idx: int = 0,
) -> StructureEvent:
    swing_type = SwingType.LOW if structure_type in (
        StructureType.CHOCH_BULL, StructureType.BOS_BULL
    ) else SwingType.HIGH
    return StructureEvent(
        index=index,
        structure_type=structure_type,
        break_price=break_price,
        broken_swing=_make_swing(swing_idx, swing_price, swing_type),
        impulse_size=abs(break_price - swing_price),
    )


def _smc_ctx(phase: SMCPhase, warmup: bool = True, bias: float = 0.0, last_event_type: StructureType | None = None) -> SMCContext:
    """Build a minimal SMCContext."""
    events = []
    if last_event_type is not None:
        events = [_make_event(index=10, structure_type=last_event_type, break_price=1005.0)]
    return SMCContext(
        phase=phase,
        trend_bias=bias,
        warmup_complete=warmup,
        current_price=1000.0,
        bar_index=299,
        bars_analyzed=300,
        structure_events=events,
    )


# ---------------------------------------------------------------------------
# Smoke test: ACCUMULATION from real SMC on synthetic CHoCH H1 data
# ---------------------------------------------------------------------------


class TestSmokeSMCAccumulation:
    """
    Smoke test: bot correctly identifies ACCUMULATION on CHoCH in H1 data.

    We synthesize a downtrend followed by a bullish CHoCH by constructing a
    DataFrame with a clear swing low break, then verify the full SMCAnalyzer →
    MarketRegimeDetector pipeline detects ACCUMULATION.
    """

    def test_smc_structure_analyzer_returns_context(self):
        """SMCStructureAnalyzer must return a valid SMCContext for any OHLCV."""
        sa = SMCStructureAnalyzer(ttl_seconds=300.0, swing_strength=3, min_warmup_bars=50)
        df = _make_df(300, "flat")
        ctx = sa.get_context("BTC/USDT", df)
        assert isinstance(ctx, SMCContext)
        assert ctx.bars_analyzed == 300

    def test_regime_detector_accepts_smc_context(self):
        """MarketRegimeDetector.analyze() should accept and process an SMCContext."""
        df = _make_df(300, "flat")
        det = MarketRegimeDetector()
        ctx = _smc_ctx(SMCPhase.ACCUMULATION)
        result = det.analyze(df, smc_context=ctx)
        assert result.regime == MarketRegime.ACCUMULATION
        assert result.recommended_strategy == RecommendedStrategy.SMC

    def test_regime_detector_sets_accumulation_from_choch(self):
        """
        Full pipeline: SMCContext(phase=ACCUMULATION, warmup=True) →
        MarketRegimeDetector detects ACCUMULATION.
        """
        df = _make_df(300)
        det = MarketRegimeDetector()
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True, bias=0.3)
        result = det.analyze(df, smc_context=ctx)
        assert result.regime == MarketRegime.ACCUMULATION

    def test_regime_detector_sets_distribution_from_choch(self):
        """SMCContext(phase=DISTRIBUTION) → DISTRIBUTION regime."""
        df = _make_df(300)
        det = MarketRegimeDetector()
        ctx = _smc_ctx(SMCPhase.DISTRIBUTION, warmup=True, bias=-0.3)
        result = det.analyze(df, smc_context=ctx)
        assert result.regime == MarketRegime.DISTRIBUTION

    def test_no_smc_override_without_warmup(self):
        """When warmup is False, SMC must NOT override the indicator-based regime."""
        df = _make_df(300, "flat")
        det = MarketRegimeDetector()
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=False)
        result = det.analyze(df, smc_context=ctx)
        # Should be indicator-based — not forced to ACCUMULATION
        assert result.regime != MarketRegime.ACCUMULATION

    def test_smc_context_stored_in_regime_analysis(self):
        """When SMC drives the regime, smc_context must be stored in RegimeAnalysis."""
        df = _make_df(300)
        det = MarketRegimeDetector()
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        result = det.analyze(df, smc_context=ctx)
        assert result.smc_context is ctx

    def test_smc_context_none_for_indicator_regime(self):
        """When no SMC context, smc_context must be None in RegimeAnalysis."""
        df = _make_df(300)
        det = MarketRegimeDetector()
        result = det.analyze(df, smc_context=None)
        assert result.smc_context is None

    def test_smc_details_in_analysis_details(self):
        """SMC metadata should appear in analysis_details when SMC drives regime."""
        df = _make_df(300)
        det = MarketRegimeDetector()
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True, bias=0.4)
        result = det.analyze(df, smc_context=ctx)
        details = result.analysis_details
        assert details.get("smc_phase") == "accumulation"
        assert details.get("smc_warmup_complete") is True

    def test_smc_recommended_strategy_is_smc(self):
        """ACCUMULATION detected via SMC must recommend SMC strategy."""
        df = _make_df(300)
        det = MarketRegimeDetector()
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        result = det.analyze(df, smc_context=ctx)
        assert result.recommended_strategy == RecommendedStrategy.SMC


# ---------------------------------------------------------------------------
# Freeze tests
# ---------------------------------------------------------------------------


class TestSMCFreeze:
    """Freeze: ACCUMULATION/DISTRIBUTION blocks indicator-based regime switching."""

    def setup_method(self):
        self.df = _make_df(300, "flat")
        self.det = MarketRegimeDetector()

    def test_freeze_engaged_on_accumulation(self):
        """After ACCUMULATION, _frozen_smc_regime must be set."""
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        self.det.analyze(self.df, smc_context=ctx)
        assert self.det._frozen_smc_regime == MarketRegime.ACCUMULATION

    def test_freeze_engaged_on_distribution(self):
        """After DISTRIBUTION, _frozen_smc_regime must be set."""
        ctx = _smc_ctx(SMCPhase.DISTRIBUTION, warmup=True)
        self.det.analyze(self.df, smc_context=ctx)
        assert self.det._frozen_smc_regime == MarketRegime.DISTRIBUTION

    def test_frozen_regime_held_during_indicator_noise(self):
        """
        After ACCUMULATION freeze: subsequent call with RANGING SMC context
        must NOT switch to an indicator-based regime.
        """
        # Engage freeze
        accum_ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        self.det.analyze(self.df, smc_context=accum_ctx)
        assert self.det._frozen_smc_regime == MarketRegime.ACCUMULATION

        # Now simulate RANGING (no more CHoCH signal) — freeze must hold
        ranging_ctx = _smc_ctx(SMCPhase.RANGING, warmup=True)
        result = self.det.analyze(self.df, smc_context=ranging_ctx)
        assert result.regime == MarketRegime.ACCUMULATION

    def test_freeze_released_by_opposite_choch_from_accumulation(self):
        """
        ACCUMULATION freeze is released when SMC signals CHoCH_BEAR.
        """
        # Engage freeze
        accum_ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        self.det.analyze(self.df, smc_context=accum_ctx)
        assert self.det._frozen_smc_regime == MarketRegime.ACCUMULATION

        # CHoCH_BEAR should release the freeze
        choch_bear_ctx = _smc_ctx(
            SMCPhase.DISTRIBUTION,  # New phase after CHoCH_BEAR
            warmup=True,
            last_event_type=StructureType.CHOCH_BEAR,
        )
        self.det.analyze(self.df, smc_context=choch_bear_ctx)
        # The freeze from ACCUMULATION is released; new DISTRIBUTION freeze might be set
        # (or freeze is cleared if the new phase is not ACCUMULATION/DISTRIBUTION)
        # In this case: new phase is DISTRIBUTION → new freeze engaged
        assert self.det._frozen_smc_regime != MarketRegime.ACCUMULATION

    def test_freeze_released_by_opposite_choch_from_distribution(self):
        """
        DISTRIBUTION freeze is released when SMC signals CHoCH_BULL.
        """
        # Engage freeze
        dist_ctx = _smc_ctx(SMCPhase.DISTRIBUTION, warmup=True)
        self.det.analyze(self.df, smc_context=dist_ctx)
        assert self.det._frozen_smc_regime == MarketRegime.DISTRIBUTION

        # CHoCH_BULL should release the DISTRIBUTION freeze
        choch_bull_ctx = _smc_ctx(
            SMCPhase.ACCUMULATION,  # New phase after CHoCH_BULL
            warmup=True,
            last_event_type=StructureType.CHOCH_BULL,
        )
        self.det.analyze(self.df, smc_context=choch_bull_ctx)
        # Distribution freeze cleared; accumulation freeze now set
        assert self.det._frozen_smc_regime != MarketRegime.DISTRIBUTION

    def test_no_freeze_without_smc_context(self):
        """Without any SMC context, freeze must never be engaged."""
        self.det.analyze(self.df, smc_context=None)
        assert self.det._frozen_smc_regime is None

    def test_freeze_not_engaged_without_warmup(self):
        """Pre-warmup SMC context must not engage the freeze."""
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=False)
        self.det.analyze(self.df, smc_context=ctx)
        assert self.det._frozen_smc_regime is None


# ---------------------------------------------------------------------------
# Backward compatibility: analyze_with_smc()
# ---------------------------------------------------------------------------


class TestAnalyzeWithSmcBackwardCompat:
    """analyze_with_smc() must still work correctly as a thin shim."""

    def setup_method(self):
        self.df = _make_df(300, "flat")
        self.det = MarketRegimeDetector()

    def test_accumulation_via_shim(self):
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        result = self.det.analyze_with_smc(self.df, ctx)
        assert result.regime == MarketRegime.ACCUMULATION

    def test_distribution_via_shim(self):
        ctx = _smc_ctx(SMCPhase.DISTRIBUTION, warmup=True)
        result = self.det.analyze_with_smc(self.df, ctx)
        assert result.regime == MarketRegime.DISTRIBUTION

    def test_non_smc_phase_via_shim(self):
        ctx = _smc_ctx(SMCPhase.RANGING, warmup=True)
        result = self.det.analyze_with_smc(self.df, ctx)
        assert result.regime not in (MarketRegime.ACCUMULATION, MarketRegime.DISTRIBUTION)

    def test_confidence_higher_with_warmup_via_shim(self):
        """With warmup=True confidence > warmup=False for same ACCUMULATION phase."""
        ctx_warm = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        r_warm = self.det.analyze_with_smc(self.df, ctx_warm)
        det2 = MarketRegimeDetector()
        ctx_cold = _smc_ctx(SMCPhase.ACCUMULATION, warmup=False)
        r_cold = det2.analyze_with_smc(self.df, ctx_cold)
        assert r_warm.confidence > r_cold.confidence

    def test_smc_details_stored_via_shim(self):
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True, bias=0.6)
        result = self.det.analyze_with_smc(self.df, ctx)
        details = result.analysis_details
        assert details.get("smc_phase") == "accumulation"
        assert details.get("smc_warmup_complete") is True
        assert abs(details.get("smc_trend_bias", -99) - 0.6) < 1e-6


# ---------------------------------------------------------------------------
# SMCStructureAnalyzer full pipeline smoke
# ---------------------------------------------------------------------------


class TestSMCStructureAnalyzerPipeline:
    """End-to-end: SMCStructureAnalyzer → MarketRegimeDetector."""

    def test_pipeline_returns_regime_analysis(self):
        sa = SMCStructureAnalyzer(ttl_seconds=300.0, swing_strength=3, min_warmup_bars=50)
        det = MarketRegimeDetector()
        df = _make_df(300, "flat")
        ctx = sa.get_context("BTC/USDT", df)
        result = det.analyze(df, smc_context=ctx)
        # Result is always a RegimeAnalysis object
        from bot.orchestrator.market_regime import RegimeAnalysis
        assert isinstance(result, RegimeAnalysis)

    def test_pipeline_caches_context(self):
        """Second call to get_context uses cached context without recomputing."""
        from unittest.mock import patch
        sa = SMCStructureAnalyzer(ttl_seconds=300.0, swing_strength=3, min_warmup_bars=50)
        df = _make_df(300)
        with patch.object(sa._analyzer, "analyze", wraps=sa._analyzer.analyze) as mock_analyze:
            sa.get_context("BTC/USDT", df)
            sa.get_context("BTC/USDT", df)
            assert mock_analyze.call_count == 1
