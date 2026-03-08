"""
Unit tests for bot/tests/backtesting/strategy_router.py

Tests cover the unified RoutingConfig-backed StrategyRouter (issue #368 / #371):
- Regime-to-strategy mapping via RoutingConfig
- HYBRID recommendation handling
- HOLD / REDUCE_EXPOSURE behaviour
- Cooldown blocking and expiry
- Activated / deactivated tracking
- Switch history recording
- Reset behaviour
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
    def test_no_regime_returns_empty(self) -> None:
        """When no regime is known yet, the router returns its current (empty) state."""
        router = StrategyRouter()
        event = router.on_bar(regime=None, current_bar=0)
        # New unified router starts empty — strategies are only activated once a
        # regime is detected (matches live StrategySelector behaviour).
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

    def test_hybrid_regime_includes_tf_grid_dca(self) -> None:
        """HYBRID recommendation activates all three strategies from hybrid_weights."""
        router = StrategyRouter(cooldown_bars=0)
        regime = _make_regime(MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID)
        event = router.on_bar(regime, current_bar=0)
        assert "grid" in event.active_strategies
        assert "dca" in event.active_strategies
        assert "trend_follower" in event.active_strategies

    def test_bull_trend_dca_includes_tf_and_dca(self) -> None:
        """BULL_TREND + DCA recommendation → trend_follower + dca from routing config."""
        router = StrategyRouter(cooldown_bars=0)
        regime = _make_regime(MarketRegime.BULL_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=0)
        assert "trend_follower" in event.active_strategies
        assert "dca" in event.active_strategies

    def test_hold_keeps_current_strategies(self) -> None:
        """HOLD keeps whatever was previously active (does not empty the set)."""
        router = StrategyRouter(cooldown_bars=0)
        # First: establish grid
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), current_bar=0
        )
        # Then: HOLD — should keep grid
        event = router.on_bar(
            _make_regime(MarketRegime.QUIET_TRANSITION, RecommendedStrategy.HOLD),
            current_bar=1,
        )
        assert "grid" in event.active_strategies

    def test_reduce_exposure_deactivates_all(self) -> None:
        """REDUCE_EXPOSURE should result in no active strategies."""
        router = StrategyRouter(cooldown_bars=0)
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), current_bar=0
        )
        event = router.on_bar(
            _make_regime(MarketRegime.VOLATILE_TRANSITION, RecommendedStrategy.REDUCE_EXPOSURE),
            current_bar=1,
        )
        assert event.active_strategies == set()

    def test_smc_strategy_in_accumulation(self) -> None:
        """ACCUMULATION regime → smc strategy (via RoutingConfig)."""
        router = StrategyRouter(cooldown_bars=0)
        regime = _make_regime(MarketRegime.ACCUMULATION, RecommendedStrategy.SMC)
        event = router.on_bar(regime, current_bar=0)
        assert "smc" in event.active_strategies

    def test_smc_strategy_in_distribution(self) -> None:
        """DISTRIBUTION regime → smc strategy (via RoutingConfig)."""
        router = StrategyRouter(cooldown_bars=0)
        regime = _make_regime(MarketRegime.DISTRIBUTION, RecommendedStrategy.SMC)
        event = router.on_bar(regime, current_bar=0)
        assert "smc" in event.active_strategies

    def test_cooldown_blocks_switch(self) -> None:
        router = StrategyRouter(cooldown_bars=10)
        # Establish initial state (grid only)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        # Try to switch to DCA 5 bars later — cooldown should block
        regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=5)
        assert event.cooldown_remaining > 0
        # Strategy set should NOT have changed
        assert "grid" in event.active_strategies

    def test_cooldown_expires_after_n_bars(self) -> None:
        router = StrategyRouter(cooldown_bars=5)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        # 6 bars later — cooldown should have expired
        regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=6)
        assert event.cooldown_remaining == 0
        assert "dca" in event.active_strategies
        assert "grid" not in event.active_strategies

    def test_activated_deactivated_tracking(self) -> None:
        router = StrategyRouter(cooldown_bars=0)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        event = router.on_bar(
            _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA), current_bar=1
        )
        assert "dca" in event.activated
        assert "grid" in event.deactivated

    def test_switch_history_recorded(self) -> None:
        router = StrategyRouter(cooldown_bars=0)
        # Bar 0: bootstrap → GRID (switch #1)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        # Bar 1: GRID → DCA (switch #2)
        router.on_bar(_make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA), 1)
        assert len(router.switch_history) == 2
        # The last switch (bar 1) should show DCA in the "to" field
        last_switch = router.switch_history[-1]
        assert last_switch["bar"] == 1
        assert "dca" in last_switch["to"]

    def test_reset_clears_history_and_state(self) -> None:
        router = StrategyRouter(cooldown_bars=0)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        router.on_bar(_make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA), 1)
        router.reset()
        assert router.switch_history == []
        # After reset, strategies are empty (same as initial state)
        assert router._active_strategies == set()

    def test_no_switch_if_same_regime(self) -> None:
        router = StrategyRouter(cooldown_bars=0)
        regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        # Bar 0: bootstrap → GRID (switch #1 recorded)
        router.on_bar(regime, 0)
        history_len_before = len(router.switch_history)
        # Bar 1: same regime — no switch
        event = router.on_bar(regime, 1)
        assert len(router.switch_history) == history_len_before  # no new switch
        assert event.activated == set()
        assert event.deactivated == set()
