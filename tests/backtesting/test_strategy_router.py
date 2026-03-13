"""
Unit tests for bot/tests/backtesting/strategy_router.py (issue #371)

Tests cover:
- Regime-to-strategy mapping via RoutingConfig
- REDUCE_EXPOSURE and HOLD special cases
- Cooldown blocking and expiry
- Activated/deactivated tracking
- Switch history recording
- Reset behaviour
- Two-phase PRE_SWITCH gate (issue #360 / C1 parity fix)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

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
    """Router without PRE_SWITCH gate — for cooldown / routing tests."""
    return StrategyRouter(
        routing_config=_make_routing_config(),
        cooldown_bars=cooldown_bars,
        enable_pre_switch_gate=False,
    )


def _make_router_with_gate(
    require_smc: bool = True,
    timer_override: float = 0.0,
) -> StrategyRouter:
    """Router with PRE_SWITCH gate enabled, zero timer by default for instant confirmation."""
    return StrategyRouter(
        routing_config=_make_routing_config(),
        cooldown_bars=0,
        enable_pre_switch_gate=True,
        require_smc_confirmation=require_smc,
        pre_switch_duration_override=timer_override,
    )


def _make_regime(
    regime: MarketRegime = MarketRegime.TIGHT_RANGE,
    recommended: RecommendedStrategy = RecommendedStrategy.GRID,
    confidence: float = 0.8,
    confluence_score: float = 0.5,
    smc_signal: str | None = None,
) -> RegimeAnalysis:
    details: dict = {}
    if smc_signal is not None:
        details["smc_signal"] = smc_signal
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
        analysis_details=details,
    )


def _ts(offset_seconds: float = 0.0) -> datetime:
    """Return a UTC datetime offset from a fixed base (for timer tests)."""
    base = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_seconds)


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

    def test_quiet_transition_selects_smc(self) -> None:
        # QUIET_TRANSITION now routes to SMC (Grid removed — prone to trend losses)
        router = _make_router()
        regime = _make_regime(MarketRegime.QUIET_TRANSITION, RecommendedStrategy.GRID)
        event = router.on_bar(regime, current_bar=0)
        assert "smc" in event.active_strategies
        assert "grid" not in event.active_strategies


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


class TestStrategyRouterPreSwitchGate:
    """Tests for the two-phase PRE_SWITCH gate (mirrors StrategySelector behaviour)."""

    def test_first_transition_bypasses_gate(self) -> None:
        """On startup (_current_regime=None), the gate is skipped — first regime fires immediately."""
        router = _make_router_with_gate(require_smc=True, timer_override=3600.0)
        regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        event = router.on_bar(regime, current_bar=0, current_timestamp=_ts(0))
        # First transition: gate bypassed, switch immediate
        assert "grid" in event.active_strategies
        assert event.pre_switch_active is False

    def test_pre_switch_entered_on_regime_change(self) -> None:
        """Second transition enters PRE_SWITCH and blocks immediately."""
        router = _make_router_with_gate(require_smc=True, timer_override=3600.0)
        # Establish initial regime
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
            current_bar=0, current_timestamp=_ts(0),
        )
        # Regime changes to BEAR_TREND → should enter PRE_SWITCH
        event = router.on_bar(
            _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA),
            current_bar=1, current_timestamp=_ts(60),
        )
        assert event.pre_switch_active is True
        assert "grid" in event.active_strategies  # old strategies kept
        assert "dca" not in event.active_strategies

    def test_pre_switch_blocks_until_timer_expires(self) -> None:
        """Gate holds for timer duration, then fires when timer clears (no SMC required)."""
        # 600s timer, no SMC requirement
        router = _make_router_with_gate(require_smc=False, timer_override=600.0)
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
            current_bar=0, current_timestamp=_ts(0),
        )
        bear = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)

        # Enter PRE_SWITCH at t=60s
        e1 = router.on_bar(bear, current_bar=1, current_timestamp=_ts(60))
        assert e1.pre_switch_active is True

        # Still in PRE_SWITCH at t=500s (< 600s)
        e2 = router.on_bar(bear, current_bar=2, current_timestamp=_ts(500))
        assert e2.pre_switch_active is True
        assert "grid" in e2.active_strategies

        # Timer expires at t=661s (≥ 660s = 60 + 600)
        e3 = router.on_bar(bear, current_bar=3, current_timestamp=_ts(661))
        assert e3.pre_switch_active is False
        assert "dca" in e3.active_strategies
        assert "grid" not in e3.active_strategies

    def test_pre_switch_requires_smc_signal(self) -> None:
        """When require_smc=True, transition only fires after timer + BOS/CHoCH signal."""
        router = _make_router_with_gate(require_smc=True, timer_override=0.0)
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
            current_bar=0, current_timestamp=_ts(0),
        )
        # TIGHT_RANGE → BULL_TREND requires "BOS" (per TRANSITION_SMC_REQUIREMENTS)
        bull_no_smc = _make_regime(
            MarketRegime.BULL_TREND, RecommendedStrategy.DCA, smc_signal=None
        )
        # Timer=0 but no SMC → still blocked
        e1 = router.on_bar(bull_no_smc, current_bar=1, current_timestamp=_ts(1))
        assert e1.pre_switch_active is True

        # BOS signal arrives → gate clears
        bull_with_bos = _make_regime(
            MarketRegime.BULL_TREND, RecommendedStrategy.DCA, smc_signal="BOS"
        )
        e2 = router.on_bar(bull_with_bos, current_bar=2, current_timestamp=_ts(2))
        assert e2.pre_switch_active is False
        assert "trend_follower" in e2.active_strategies  # bull_trend_default

    def test_pre_switch_cancelled_on_regime_return(self) -> None:
        """If regime returns to current, PRE_SWITCH is cancelled."""
        router = _make_router_with_gate(require_smc=True, timer_override=3600.0)
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
            current_bar=0, current_timestamp=_ts(0),
        )
        # Enter PRE_SWITCH for BEAR_TREND
        router.on_bar(
            _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA),
            current_bar=1, current_timestamp=_ts(60),
        )
        assert router._transition_phase.value == "pre_switch"

        # Regime returns to TIGHT_RANGE → PRE_SWITCH cancelled
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
            current_bar=2, current_timestamp=_ts(120),
        )
        assert router._transition_phase.value == "stable"

    def test_pre_switch_restarted_on_target_change(self) -> None:
        """If the target regime changes during PRE_SWITCH, it restarts for the new target."""
        router = _make_router_with_gate(require_smc=True, timer_override=3600.0)
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
            current_bar=0, current_timestamp=_ts(0),
        )
        # PRE_SWITCH for BEAR_TREND
        router.on_bar(
            _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA),
            current_bar=1, current_timestamp=_ts(60),
        )
        assert router._pre_switch_target_regime == MarketRegime.BEAR_TREND

        # Target changes to BULL_TREND → PRE_SWITCH restarted
        router.on_bar(
            _make_regime(MarketRegime.BULL_TREND, RecommendedStrategy.DCA),
            current_bar=2, current_timestamp=_ts(120),
        )
        assert router._pre_switch_target_regime == MarketRegime.BULL_TREND
        # PRE_SWITCH started fresh (elapsed reset)
        assert router._pre_switch_smc_confirmed is False

    def test_gate_disabled_switches_immediately(self) -> None:
        """With enable_pre_switch_gate=False, second transition fires immediately (old behaviour)."""
        router = _make_router(cooldown_bars=0)  # gate disabled
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
            current_bar=0,
        )
        event = router.on_bar(
            _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA),
            current_bar=1,
        )
        assert event.pre_switch_active is False
        assert "dca" in event.active_strategies

    def test_pre_switch_elapsed_reported(self) -> None:
        """pre_switch_elapsed_s in the event reflects time since PRE_SWITCH started."""
        router = _make_router_with_gate(require_smc=False, timer_override=3600.0)
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
            current_bar=0, current_timestamp=_ts(0),
        )
        bear = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        # Enter PRE_SWITCH at t=0
        router.on_bar(bear, current_bar=1, current_timestamp=_ts(0))
        # 500s later
        e = router.on_bar(bear, current_bar=2, current_timestamp=_ts(500))
        assert e.pre_switch_active is True
        assert abs(e.pre_switch_elapsed_s - 500.0) < 1.0

    def test_reset_clears_gate_state(self) -> None:
        """reset() returns gate to STABLE with no pending PRE_SWITCH."""
        router = _make_router_with_gate(require_smc=True, timer_override=3600.0)
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
            current_bar=0, current_timestamp=_ts(0),
        )
        router.on_bar(
            _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA),
            current_bar=1, current_timestamp=_ts(60),
        )
        assert router._transition_phase.value == "pre_switch"
        router.reset()
        assert router._transition_phase.value == "stable"
        assert router._current_regime is None
        assert router._pre_switch_target_regime is None
