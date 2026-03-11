"""
Unit tests for bot/tests/backtesting/strategy_router.py (issue #371)

Tests cover:
- Regime-to-strategy mapping via RoutingConfig
- REDUCE_EXPOSURE and HOLD special cases
- Cooldown blocking and expiry
- Activated/deactivated tracking
- Switch history recording
- Reset behaviour
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.routing_config import RoutingConfig
from bot.tests.backtesting.strategy_router import StrategyRouter

# Path to the production routing config
_PROD_ROUTING_CONFIG = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "configs",
    "strategy_routing.yaml",
)


def _make_routing_config() -> RoutingConfig:
    return RoutingConfig(_PROD_ROUTING_CONFIG)


def _make_router(cooldown_bars: int = 0) -> StrategyRouter:
    return StrategyRouter(routing_config=_make_routing_config(), cooldown_bars=cooldown_bars)


def _make_regime(
    regime: MarketRegime = MarketRegime.TIGHT_RANGE,
    recommended: RecommendedStrategy = RecommendedStrategy.GRID,
    confidence: float = 0.8,
    confluence_score: float = 0.5,
) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=regime,
        confidence=confidence,
        recommended_strategy=recommended,
        confluence_score=confluence_score,
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


class TestStrategyRouterBasicRouting:
    def test_no_regime_returns_initial_set(self) -> None:
        router = _make_router()
        event = router.on_bar(regime=None, current_bar=0)
        # Bootstrap: grid + dca active before any regime arrives
        assert "grid" in event.active_strategies
        assert "dca" in event.active_strategies
        assert event.cooldown_remaining == 0
        assert event.activated == set()

    def test_tight_range_selects_grid(self) -> None:
        router = _make_router()
        regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        event = router.on_bar(regime, current_bar=0)
        assert "grid" in event.active_strategies
        assert "dca" not in event.active_strategies

    def test_wide_range_selects_grid(self) -> None:
        router = _make_router()
        regime = _make_regime(MarketRegime.WIDE_RANGE, RecommendedStrategy.GRID)
        event = router.on_bar(regime, current_bar=0)
        assert "grid" in event.active_strategies
        assert "dca" not in event.active_strategies

    def test_bear_trend_selects_dca(self) -> None:
        router = _make_router()
        regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=0)
        assert "dca" in event.active_strategies
        assert "grid" not in event.active_strategies

    def test_bull_trend_default_selects_trend_follower_and_smc(self) -> None:
        """BULL_TREND with low confluence → trend_follower + smc (DCA removed: arbiter conflict)."""
        router = _make_router()
        # low confluence_score (<0.7) → not HYBRID rule
        regime = _make_regime(
            MarketRegime.BULL_TREND,
            RecommendedStrategy.DCA,
            confluence_score=0.5,
        )
        event = router.on_bar(regime, current_bar=0)
        assert "trend_follower" in event.active_strategies
        assert "smc" in event.active_strategies
        assert "dca" not in event.active_strategies  # removed: arbiter blocks DCA in _UPTREND

    def test_bull_trend_high_confluence_selects_hybrid(self) -> None:
        """BULL_TREND with high confluence (>=0.7) → hybrid rule (grid + tf + smc)."""
        router = _make_router()
        regime = _make_regime(
            MarketRegime.BULL_TREND,
            RecommendedStrategy.HYBRID,
            confluence_score=0.8,
        )
        event = router.on_bar(regime, current_bar=0)
        assert "grid" in event.active_strategies
        assert "trend_follower" in event.active_strategies

    def test_volatile_transition_selects_smc(self) -> None:
        router = _make_router()
        regime = _make_regime(MarketRegime.VOLATILE_TRANSITION, RecommendedStrategy.SMC)
        event = router.on_bar(regime, current_bar=0)
        assert "smc" in event.active_strategies

    def test_quiet_transition_selects_grid(self) -> None:
        router = _make_router()
        regime = _make_regime(MarketRegime.QUIET_TRANSITION, RecommendedStrategy.GRID)
        event = router.on_bar(regime, current_bar=0)
        assert "grid" in event.active_strategies


class TestStrategyRouterSpecialCases:
    def test_reduce_exposure_deactivates_all(self) -> None:
        """REDUCE_EXPOSURE → empty set regardless of current state."""
        router = _make_router()
        # First establish something active
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), current_bar=0
        )
        regime = _make_regime(MarketRegime.VOLATILE_TRANSITION, RecommendedStrategy.REDUCE_EXPOSURE)
        event = router.on_bar(regime, current_bar=1)
        assert event.active_strategies == set()

    def test_hold_keeps_current_active_strategies(self) -> None:
        """HOLD → active set unchanged."""
        router = _make_router()
        # Establish grid as active
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), current_bar=0
        )
        before = router._active_strategies.copy()
        regime = _make_regime(MarketRegime.QUIET_TRANSITION, RecommendedStrategy.HOLD)
        event = router.on_bar(regime, current_bar=1)
        assert event.active_strategies == before

    def test_regime_value_and_recommendation_in_event(self) -> None:
        router = _make_router()
        regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=0)
        assert event.regime_value == "bear_trend"
        assert event.recommendation == "dca"


class TestStrategyRouterCooldown:
    def test_cooldown_blocks_switch(self) -> None:
        router = _make_router(cooldown_bars=10)
        # Establish initial state (grid only)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        # Try to switch to DCA 5 bars later — cooldown should block
        regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=5)
        assert event.cooldown_remaining > 0
        # Strategy set should NOT have changed
        assert "grid" in event.active_strategies

    def test_cooldown_expires_after_n_bars(self) -> None:
        router = _make_router(cooldown_bars=5)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        # 6 bars later — cooldown should have expired
        regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=6)
        assert event.cooldown_remaining == 0
        assert "dca" in event.active_strategies
        assert "grid" not in event.active_strategies

    def test_no_cooldown_on_zero_cooldown_bars(self) -> None:
        router = _make_router(cooldown_bars=0)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        event = router.on_bar(regime, current_bar=1)
        assert event.cooldown_remaining == 0
        assert "dca" in event.active_strategies


class TestStrategyRouterTracking:
    def test_activated_deactivated_tracking(self) -> None:
        router = _make_router()
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        event = router.on_bar(
            _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA), current_bar=1
        )
        assert "dca" in event.activated
        assert "grid" in event.deactivated

    def test_switch_history_recorded(self) -> None:
        router = _make_router()
        # Bar 0: bootstrap → GRID (switch #1)
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        # Bar 1: GRID → DCA (switch #2)
        router.on_bar(_make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA), 1)
        assert len(router.switch_history) == 2
        # The last switch (bar 1) should show DCA in the "to" field
        last_switch = router.switch_history[-1]
        assert last_switch["bar"] == 1
        assert "dca" in last_switch["to"]

    def test_no_switch_if_same_regime(self) -> None:
        router = _make_router()
        regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        # Bar 0: bootstrap → GRID (switch #1 recorded)
        router.on_bar(regime, 0)
        history_len_before = len(router.switch_history)
        # Bar 1: same regime — no switch
        event = router.on_bar(regime, 1)
        assert len(router.switch_history) == history_len_before  # no new switch
        assert event.activated == set()
        assert event.deactivated == set()

    def test_reset_clears_history(self) -> None:
        router = _make_router()
        router.on_bar(_make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), 0)
        router.on_bar(_make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA), 1)
        router.reset()
        assert router.switch_history == []
        assert "grid" in router._active_strategies  # reset to initial set
