"""
Tests for bot/orchestrator/market_regime.py

Covers:
- New MarketRegime.ACCUMULATION / DISTRIBUTION enum values
- New RecommendedStrategy.SMC enum value
- analyze_with_smc() override logic
- _recommend_strategy() for new regimes
- _calculate_confidence() for new regimes
- _REGIME_TO_STRATEGIES includes SMC
"""

import numpy as np
import pandas as pd

from bot.core.smc.models import SMCContext, SMCPhase
from bot.orchestrator.market_regime import (
    MarketRegime,
    MarketRegimeDetector,
    RecommendedStrategy,
)

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


def _smc_ctx(phase: SMCPhase, warmup: bool = True, bias: float = 0.0) -> SMCContext:
    """Build a minimal SMCContext with the given phase."""
    return SMCContext(
        phase=phase,
        trend_bias=bias,
        warmup_complete=warmup,
        current_price=1000.0,
        bar_index=299,
        bars_analyzed=300,
    )


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestEnums:
    def test_accumulation_in_market_regime(self):
        assert MarketRegime.ACCUMULATION.value == "accumulation"

    def test_distribution_in_market_regime(self):
        assert MarketRegime.DISTRIBUTION.value == "distribution"

    def test_smc_in_recommended_strategy(self):
        assert RecommendedStrategy.SMC.value == "smc"

    def test_all_regimes_unique(self):
        values = [r.value for r in MarketRegime]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# _recommend_strategy
# ---------------------------------------------------------------------------


class TestRecommendStrategy:
    def setup_method(self):
        self.det = MarketRegimeDetector()

    def test_accumulation_recommends_smc(self):
        rec = self.det._recommend_strategy(MarketRegime.ACCUMULATION, 0.5, 25.0)
        assert rec == RecommendedStrategy.SMC

    def test_distribution_recommends_smc(self):
        rec = self.det._recommend_strategy(MarketRegime.DISTRIBUTION, 0.5, 25.0)
        assert rec == RecommendedStrategy.SMC

    def test_tight_range_recommends_grid(self):
        rec = self.det._recommend_strategy(MarketRegime.TIGHT_RANGE, 0.3, 12.0)
        assert rec == RecommendedStrategy.GRID

    def test_bear_trend_recommends_dca(self):
        rec = self.det._recommend_strategy(MarketRegime.BEAR_TREND, 0.4, 35.0)
        assert rec == RecommendedStrategy.DCA


# ---------------------------------------------------------------------------
# _calculate_confidence
# ---------------------------------------------------------------------------


class TestCalculateConfidence:
    def setup_method(self):
        self.det = MarketRegimeDetector()

    def test_accumulation_confidence_in_range(self):
        conf = self.det._calculate_confidence(
            regime=MarketRegime.ACCUMULATION,
            ema_divergence_pct=1.0,
            volatility_percentile=60.0,
            trend_strength=0.5,
            adx=25.0,
        )
        assert 0.0 <= conf <= 1.0

    def test_distribution_confidence_in_range(self):
        conf = self.det._calculate_confidence(
            regime=MarketRegime.DISTRIBUTION,
            ema_divergence_pct=1.0,
            volatility_percentile=60.0,
            trend_strength=0.5,
            adx=25.0,
        )
        assert 0.0 <= conf <= 1.0

    def test_accumulation_confidence_higher_with_strong_adx(self):
        low_adx = self.det._calculate_confidence(
            regime=MarketRegime.ACCUMULATION,
            ema_divergence_pct=0.5,
            volatility_percentile=50.0,
            trend_strength=0.3,
            adx=10.0,
        )
        high_adx = self.det._calculate_confidence(
            regime=MarketRegime.ACCUMULATION,
            ema_divergence_pct=0.5,
            volatility_percentile=50.0,
            trend_strength=0.3,
            adx=40.0,
        )
        assert high_adx > low_adx


# ---------------------------------------------------------------------------
# analyze_with_smc
# ---------------------------------------------------------------------------


class TestAnalyzeWithSmc:
    def setup_method(self):
        self.det = MarketRegimeDetector()
        self.df = _make_df(300, "flat")

    def test_accumulation_phase_overrides_regime(self):
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        result = self.det.analyze_with_smc(self.df, ctx)
        assert result.regime == MarketRegime.ACCUMULATION

    def test_distribution_phase_overrides_regime(self):
        ctx = _smc_ctx(SMCPhase.DISTRIBUTION, warmup=True)
        result = self.det.analyze_with_smc(self.df, ctx)
        assert result.regime == MarketRegime.DISTRIBUTION

    def test_accumulation_recommends_smc(self):
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        result = self.det.analyze_with_smc(self.df, ctx)
        assert result.recommended_strategy == RecommendedStrategy.SMC

    def test_distribution_recommends_smc(self):
        ctx = _smc_ctx(SMCPhase.DISTRIBUTION, warmup=True)
        result = self.det.analyze_with_smc(self.df, ctx)
        assert result.recommended_strategy == RecommendedStrategy.SMC

    def test_bull_trend_no_override(self):
        """Non-ACCUMULATION/DISTRIBUTION phases must not override the regime."""
        ctx = _smc_ctx(SMCPhase.BULL_TREND, warmup=True)
        result = self.det.analyze_with_smc(self.df, ctx)
        assert result.regime != MarketRegime.ACCUMULATION
        assert result.regime != MarketRegime.DISTRIBUTION

    def test_ranging_no_override(self):
        ctx = _smc_ctx(SMCPhase.RANGING, warmup=True)
        result = self.det.analyze_with_smc(self.df, ctx)
        assert result.regime not in (MarketRegime.ACCUMULATION, MarketRegime.DISTRIBUTION)

    def test_last_analysis_updated_to_accumulation(self):
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        self.det.analyze_with_smc(self.df, ctx)
        assert self.det.last_analysis.regime == MarketRegime.ACCUMULATION

    def test_regime_history_updated(self):
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        self.det.analyze_with_smc(self.df, ctx)
        assert self.det.regime_history[0].regime == MarketRegime.ACCUMULATION

    def test_smc_details_stored_in_analysis(self):
        ctx = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True, bias=0.6)
        result = self.det.analyze_with_smc(self.df, ctx)
        details = result.analysis_details
        assert details.get("smc_phase") == "accumulation"
        assert details.get("smc_warmup_complete") is True
        assert abs(details.get("smc_trend_bias", -99) - 0.6) < 1e-6

    def test_confidence_reduced_without_warmup(self):
        ctx_warm = _smc_ctx(SMCPhase.ACCUMULATION, warmup=True)
        ctx_cold = _smc_ctx(SMCPhase.ACCUMULATION, warmup=False)
        r_warm = self.det.analyze_with_smc(self.df, ctx_warm)
        # Re-create detector so _last_analysis starts fresh
        det2 = MarketRegimeDetector()
        r_cold = det2.analyze_with_smc(self.df, ctx_cold)
        assert r_warm.confidence > r_cold.confidence

    def test_returns_regime_analysis_object(self):
        from bot.orchestrator.market_regime import RegimeAnalysis

        ctx = _smc_ctx(SMCPhase.DISTRIBUTION, warmup=True)
        result = self.det.analyze_with_smc(self.df, ctx)
        assert isinstance(result, RegimeAnalysis)


# ---------------------------------------------------------------------------
# Volatility guard (P1.3)
# ---------------------------------------------------------------------------


class TestVolatilityGuard:
    """
    Tests the volatility guard that blocks strategy expansion during extreme
    ATR spikes.  We check the constant and set-expansion semantics used in
    _update_active_strategies.
    """

    def test_max_volatility_constant_exists(self):
        from bot.orchestrator.bot_orchestrator import BotOrchestrator

        assert hasattr(BotOrchestrator, "_MAX_VOLATILITY_ATR_PCT")
        assert BotOrchestrator._MAX_VOLATILITY_ATR_PCT > 0

    def test_max_volatility_default_is_3_pct(self):
        from bot.orchestrator.bot_orchestrator import BotOrchestrator

        assert BotOrchestrator._MAX_VOLATILITY_ATR_PCT == 3.0

    def test_set_expansion_semantics(self):
        """Guard uses `strategies > prev` (proper superset) to detect expansion."""
        prev = {"grid"}
        assert {"grid", "smc"} > prev  # adding smc → expansion → blocked
        assert not ({"grid"} > prev)  # same → not blocked
        assert not ({"smc"} > prev)  # switch, not superset → not blocked
        assert not (set() > prev)  # reduction → not blocked

    def test_reduction_not_blocked_by_guard_semantics(self):
        """Deactivation (prev → subset) is never a proper superset — guard passes."""
        prev = {"grid", "dca", "smc"}
        reduced = {"grid"}
        assert not (reduced > prev)  # reduction is fine
