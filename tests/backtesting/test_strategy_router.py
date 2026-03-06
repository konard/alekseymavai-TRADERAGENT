"""
Unit tests for bot/tests/backtesting/strategy_router.py

Tests cover:
- Regime-to-strategy mapping
- Cooldown blocking
- Trend-regime strategy additions
- Reset
"""

from datetime import datetime, timezone

from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.tests.backtesting.strategy_router import StrategyRouter


def _make_regime(
    regime: MarketRegime = MarketRegime.TIGHT_RANGE,
    recommended: RecommendedStrategy = RecommendedStrategy.GRID,
    confidence: float = 0.8,
) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=regime,
        confidence=confidence,
        recommended_strategy=recommended,
        confluence_score=0.7,
        trend_strength=0.0,
        volatility_percentile=50.0,
        ema_divergence_pct=0.01,
        atr_pct=0.01,
        rsi=50.0,
        adx=20.0,
        bb_width_pct=0.02,
        volume_ratio=1.0,
        regime_duration_seconds=3600,
        previous_regime=None,
        timestamp=datetime.now(timezone.utc),
        analysis_details={},
    )


class TestStrategyRouter:
    def test_no_regime_returns_all(self) -> None:
        router = StrategyRouter()
        event = router.on_bar(regime=None, current_bar=0)
        # Should return the initial "everything active" set
        assert "grid" in event.active_strategies
        assert "dca" in event.active_strategies
        assert event.cooldown_remaining == 0
        assert event.activated == set()

    def test_grid_regime(self) -> None:
        router = StrategyRouter(cooldown_bars=0)
        regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        event = router.on_bar(regime, current_bar=0)
        assert "grid" in event.active_strategies
        assert "dca" not in event.active_strategies

    def test_dca_regime(self) -> None:
        router = StrategyRouter(cooldown_bars=0)
        regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=0)
        assert "dca" in event.active_strategies
        assert "grid" not in event.active_strategies

    def test_hybrid_regime(self) -> None:
        router = StrategyRouter(cooldown_bars=0)
        regime = _make_regime(MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID)
        event = router.on_bar(regime, current_bar=0)
        assert "grid" in event.active_strategies
        assert "dca" in event.active_strategies

    def test_hold_regime_deactivates_all(self) -> None:
        router = StrategyRouter(cooldown_bars=0)
        # First bar — establish a non-empty previous set
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), current_bar=0
        )
        regime = _make_regime(MarketRegime.QUIET_TRANSITION, RecommendedStrategy.HOLD)
        event = router.on_bar(regime, current_bar=1)
        assert event.active_strategies == set()

    def test_trend_follower_in_bull_trend(self) -> None:
        router = StrategyRouter(cooldown_bars=0, enable_trend_follower=True)
        regime = _make_regime(MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID)
        event = router.on_bar(regime, current_bar=0)
        assert "trend_follower" in event.active_strategies

    def test_trend_follower_disabled(self) -> None:
        router = StrategyRouter(cooldown_bars=0, enable_trend_follower=False)
        regime = _make_regime(MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID)
        event = router.on_bar(regime, current_bar=0)
        assert "trend_follower" not in event.active_strategies

    def test_smc_disabled_by_default(self) -> None:
        router = StrategyRouter(cooldown_bars=0, enable_smc=False)
        regime = _make_regime(MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID)
        event = router.on_bar(regime, current_bar=0)
        assert "smc" not in event.active_strategies

    def test_smc_enabled_in_trending(self) -> None:
        router = StrategyRouter(cooldown_bars=0, enable_smc=True)
        regime = _make_regime(MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID)
        event = router.on_bar(regime, current_bar=0)
        assert "smc" in event.active_strategies

    def test_cooldown_blocks_switch(self) -> None:
        router = StrategyRouter(cooldown_bars=10, enable_trend_follower=False)
        # Establish initial state (grid only)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        # Try to switch to DCA 5 bars later — cooldown should block
        regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=5)
        assert event.cooldown_remaining > 0
        # Strategy set should NOT have changed
        assert "grid" in event.active_strategies

    def test_cooldown_expires_after_n_bars(self) -> None:
        router = StrategyRouter(cooldown_bars=5, enable_trend_follower=False)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        # 6 bars later — cooldown should have expired
        regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=6)
        assert event.cooldown_remaining == 0
        assert "dca" in event.active_strategies
        assert "grid" not in event.active_strategies

    def test_activated_deactivated_tracking(self) -> None:
        router = StrategyRouter(cooldown_bars=0, enable_trend_follower=False)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        event = router.on_bar(
            _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA), current_bar=1
        )
        assert "dca" in event.activated
        assert "grid" in event.deactivated

    def test_switch_history_recorded(self) -> None:
        router = StrategyRouter(cooldown_bars=0, enable_trend_follower=False)
        # Bar 0: bootstrap → GRID (switch #1)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        # Bar 1: GRID → DCA (switch #2)
        router.on_bar(_make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA), 1)
        assert len(router.switch_history) == 2
        # The last switch (bar 1) should show DCA in the "to" field
        last_switch = router.switch_history[-1]
        assert last_switch["bar"] == 1
        assert "dca" in last_switch["to"]

    def test_reset_clears_history(self) -> None:
        router = StrategyRouter(cooldown_bars=0)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        router.on_bar(_make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA), 1)
        router.reset()
        assert router.switch_history == []
        assert "grid" in router._active_strategies  # reset to initial set

    def test_no_switch_if_same_regime(self) -> None:
        router = StrategyRouter(cooldown_bars=0, enable_trend_follower=False)
        regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        # Bar 0: bootstrap → GRID (switch #1 recorded)
        router.on_bar(regime, 0)
        history_len_before = len(router.switch_history)
        # Bar 1: same regime — no switch
        event = router.on_bar(regime, 1)
        assert len(router.switch_history) == history_len_before  # no new switch
        assert event.activated == set()
        assert event.deactivated == set()
