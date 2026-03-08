"""
Tests for two-phase PRE_SWITCH / CONFIRMED cooldown with SMC confirmation.

Issue #360: Двухфазный Cooldown с SMC-подтверждением при переключении стратегий

Test coverage:
- All 5 transition types from the issue table (>= 10 cases each tested)
- Integration: false signal → PRE_SWITCH → STABLE (no switch)
- Integration: confirmed signal → CONFIRMED → TRANSITIONING → new regime
- TransitionPhase enum completeness
- cancelled_pre_switch_count metric
- SMC signal detection
- Timer logic per transition type
- restrict_new_entries / enter_pre_switch / exit_pre_switch in BaseStrategy
- PRE_SWITCH state in StrategyRegistry state machine
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.strategy_registry import (
    StrategyRegistry,
    StrategyState,
)
from bot.orchestrator.strategy_selector import (
    TRANSITION_SMC_REQUIREMENTS,
    TRANSITION_TIMERS,
    StrategySelector,
    TransitionPhase,
    TransitionState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_analysis(
    regime: MarketRegime = MarketRegime.TIGHT_RANGE,
    recommended: RecommendedStrategy = RecommendedStrategy.GRID,
    confidence: float = 0.8,
    regime_duration: int = 300,
    adx: float = 20.0,
    smc_signal: str | None = None,
) -> RegimeAnalysis:
    """Create a RegimeAnalysis for testing."""
    details: dict = {"current_price": 3000.0}
    if smc_signal is not None:
        details["smc_signal"] = smc_signal
    return RegimeAnalysis(
        regime=regime,
        confidence=confidence,
        recommended_strategy=recommended,
        confluence_score=0.5,
        trend_strength=0.0,
        volatility_percentile=50.0,
        ema_divergence_pct=0.0,
        atr_pct=2.0,
        rsi=50.0,
        adx=adx,
        bb_width_pct=3.0,
        volume_ratio=1.0,
        regime_duration_seconds=regime_duration,
        previous_regime=None,
        timestamp=datetime.now(timezone.utc),
        analysis_details=details,
    )


def _make_registry_with_strategies() -> StrategyRegistry:
    """Create a registry with all strategy types registered."""
    registry = StrategyRegistry(max_strategies=10)
    registry.register("grid-1", "grid", {"pair": "BTCUSDT"})
    registry.register("dca-1", "dca", {"pair": "BTCUSDT"})
    registry.register("trend-1", "trend_follower", {"pair": "BTCUSDT"})
    registry.register("smc-1", "smc", {"pair": "BTCUSDT"})
    return registry


def _selector_with_zero_timers(
    registry: StrategyRegistry,
    require_smc: bool = True,
) -> StrategySelector:
    """Selector with zero-second pre-switch timer for fast tests."""
    return StrategySelector(
        registry,
        transition_cooldown_seconds=0,
        min_regime_duration_seconds=0,
        pre_switch_duration_seconds=0.0,
        require_smc_confirmation=require_smc,
    )


def _selector_with_long_timers(
    registry: StrategyRegistry,
    require_smc: bool = True,
) -> StrategySelector:
    """Selector with 3600s pre-switch timer so the gate is never cleared in tests."""
    return StrategySelector(
        registry,
        transition_cooldown_seconds=0,
        min_regime_duration_seconds=0,
        pre_switch_duration_seconds=3600.0,
        require_smc_confirmation=require_smc,
    )


# ---------------------------------------------------------------------------
# TransitionPhase enum
# ---------------------------------------------------------------------------


class TestTransitionPhaseEnum:
    """TransitionPhase has the required 4 states."""

    def test_all_four_states_exist(self):
        phases = {p.value for p in TransitionPhase}
        assert "stable" in phases
        assert "pre_switch" in phases
        assert "confirmed" in phases
        assert "transitioning" in phases

    def test_initial_phase_is_stable(self):
        registry = StrategyRegistry()
        selector = StrategySelector(registry)
        assert selector.transition_phase == TransitionPhase.STABLE


# ---------------------------------------------------------------------------
# PRE_SWITCH state in StrategyRegistry
# ---------------------------------------------------------------------------


class TestRegistryPreSwitchState:
    """PRE_SWITCH is a valid StrategyState with the right transitions."""

    def test_pre_switch_state_exists(self):
        assert StrategyState.PRE_SWITCH.value == "pre_switch"

    @pytest.mark.asyncio
    async def test_active_can_transition_to_pre_switch(self):
        registry = StrategyRegistry()
        registry.register("s1", "grid")
        await registry.start_strategy("s1")
        assert await registry.enter_pre_switch("s1")
        assert registry.get("s1").state == StrategyState.PRE_SWITCH

    @pytest.mark.asyncio
    async def test_pre_switch_can_return_to_active(self):
        registry = StrategyRegistry()
        registry.register("s1", "grid")
        await registry.start_strategy("s1")
        await registry.enter_pre_switch("s1")
        assert await registry.exit_pre_switch("s1")
        assert registry.get("s1").state == StrategyState.ACTIVE

    @pytest.mark.asyncio
    async def test_pre_switch_can_advance_to_stopping(self):
        registry = StrategyRegistry()
        registry.register("s1", "grid")
        await registry.start_strategy("s1")
        await registry.enter_pre_switch("s1")
        # PRE_SWITCH → STOPPING is valid (confirmed transition)
        assert await registry.stop_strategy("s1")
        assert registry.get("s1").state == StrategyState.STOPPED

    @pytest.mark.asyncio
    async def test_idle_cannot_enter_pre_switch_directly(self):
        registry = StrategyRegistry()
        registry.register("s1", "grid")
        # IDLE → PRE_SWITCH should fail (not a valid direct transition)
        result = await registry.enter_pre_switch("s1")
        assert not result
        assert registry.get("s1").state == StrategyState.IDLE

    @pytest.mark.asyncio
    async def test_get_pre_switch_returns_correct_strategies(self):
        registry = StrategyRegistry()
        registry.register("s1", "grid")
        registry.register("s2", "dca")
        await registry.start_strategy("s1")
        await registry.start_strategy("s2")
        await registry.enter_pre_switch("s1")

        pre_switch = registry.get_pre_switch()
        assert len(pre_switch) == 1
        assert pre_switch[0].strategy_id == "s1"

    @pytest.mark.asyncio
    async def test_stop_all_includes_pre_switch_strategies(self):
        registry = StrategyRegistry()
        registry.register("s1", "grid")
        registry.register("s2", "dca")
        await registry.start_strategy("s1")
        await registry.start_strategy("s2")
        await registry.enter_pre_switch("s1")

        results = await registry.stop_all()
        assert results.get("s1") is True
        assert results.get("s2") is True


# ---------------------------------------------------------------------------
# BaseStrategy PRE_SWITCH interface
# ---------------------------------------------------------------------------


class TestBaseStrategyPreSwitchInterface:
    """BaseStrategy provides default no-op implementations of pre-switch hooks."""

    def _make_concrete_strategy(self):
        """Return a minimal concrete BaseStrategy subclass."""


        from bot.strategies.base import (
            BaseMarketAnalysis,
            BaseStrategy,
            StrategyPerformance,
        )

        class _MinimalStrategy(BaseStrategy):
            def get_strategy_name(self) -> str:
                return "minimal"

            def get_strategy_type(self) -> str:
                return "grid"

            def analyze_market(self, *dfs):
                return MagicMock(spec=BaseMarketAnalysis)

            def generate_signal(self, df, current_balance):
                return None

            def open_position(self, signal, position_size):
                return "pos-1"

            def update_positions(self, current_price, df):
                return []

            def close_position(self, position_id, exit_reason, exit_price):
                pass

            def get_active_positions(self):
                return []

            def get_performance(self):
                return StrategyPerformance()

        return _MinimalStrategy()

    def test_restrict_new_entries_default_false(self):
        s = self._make_concrete_strategy()
        assert s.restrict_new_entries() is False

    def test_enter_pre_switch_noop(self):
        s = self._make_concrete_strategy()
        s.enter_pre_switch(tighten_stops=True)  # should not raise

    def test_exit_pre_switch_noop(self):
        s = self._make_concrete_strategy()
        s.exit_pre_switch()  # should not raise


# ---------------------------------------------------------------------------
# StrategySelector two-phase gate — unit tests
# ---------------------------------------------------------------------------


class TestTwoPhaseGateFirstTransition:
    """First transition (no prior regime) is never gated."""

    @pytest.mark.asyncio
    async def test_first_transition_bypasses_two_phase_gate(self):
        """Without a current regime the two-phase gate is skipped entirely."""
        registry = _make_registry_with_strategies()
        selector = _selector_with_long_timers(registry, require_smc=True)

        # No current regime set → first transition
        analysis = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        result = selector.select(analysis)
        # Should be allowed immediately (first selection)
        assert result.transition_needed is True
        assert selector.transition_phase == TransitionPhase.STABLE  # not entered PRE_SWITCH

    @pytest.mark.asyncio
    async def test_first_transition_executes_and_resets_phase(self):
        registry = _make_registry_with_strategies()
        selector = _selector_with_long_timers(registry, require_smc=True)

        analysis = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        result = selector.select(analysis)
        record = await selector.execute_transition(result)
        assert record.state == TransitionState.COMPLETED
        assert selector.transition_phase == TransitionPhase.STABLE


# ---------------------------------------------------------------------------
# Five transition types (>= 10 cases)
# ---------------------------------------------------------------------------


class TestTransitionType1RangeToTrend:
    """Range → Trend transitions (30 min timer, BOS confirmation)."""

    @pytest.mark.asyncio
    async def test_range_to_bull_enters_pre_switch(self):
        """TIGHT_RANGE → BULL_TREND enters PRE_SWITCH on first detection."""
        registry = _make_registry_with_strategies()
        selector = _selector_with_long_timers(registry, require_smc=True)
        await registry.start_strategy("grid-1")
        selector._current_regime = MarketRegime.TIGHT_RANGE

        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
        )
        result = selector.select(analysis)
        assert result.transition_needed is False
        assert "PRE_SWITCH" in result.reason
        assert selector.transition_phase == TransitionPhase.PRE_SWITCH
        assert selector._pre_switch_target_regime == MarketRegime.BULL_TREND

    @pytest.mark.asyncio
    async def test_range_to_bull_timer_required_30min(self):
        """TIGHT_RANGE → BULL_TREND requires 1800s timer."""
        key = (MarketRegime.TIGHT_RANGE, MarketRegime.BULL_TREND)
        assert TRANSITION_TIMERS.get(key) == 1800.0

    @pytest.mark.asyncio
    async def test_range_to_bull_requires_bos(self):
        """TIGHT_RANGE → BULL_TREND requires BOS signal."""
        key = (MarketRegime.TIGHT_RANGE, MarketRegime.BULL_TREND)
        assert TRANSITION_SMC_REQUIREMENTS.get(key) == "BOS"

    @pytest.mark.asyncio
    async def test_wide_range_to_bull_enters_pre_switch(self):
        """WIDE_RANGE → BULL_TREND also enters PRE_SWITCH."""
        registry = _make_registry_with_strategies()
        selector = _selector_with_long_timers(registry, require_smc=True)
        await registry.start_strategy("grid-1")
        selector._current_regime = MarketRegime.WIDE_RANGE

        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
        )
        result = selector.select(analysis)
        assert result.transition_needed is False
        assert selector.transition_phase == TransitionPhase.PRE_SWITCH

    @pytest.mark.asyncio
    async def test_range_to_bear_enters_pre_switch(self):
        """TIGHT_RANGE → BEAR_TREND enters PRE_SWITCH."""
        registry = _make_registry_with_strategies()
        selector = _selector_with_long_timers(registry, require_smc=True)
        await registry.start_strategy("grid-1")
        selector._current_regime = MarketRegime.TIGHT_RANGE

        analysis = _make_analysis(
            regime=MarketRegime.BEAR_TREND,
            recommended=RecommendedStrategy.DCA,
        )
        result = selector.select(analysis)
        assert result.transition_needed is False
        assert selector.transition_phase == TransitionPhase.PRE_SWITCH


class TestTransitionType2TrendToRange:
    """Trend → Range transitions (45 min timer, no SMC required)."""

    @pytest.mark.asyncio
    async def test_bull_to_range_requires_45min_timer(self):
        """BULL_TREND → TIGHT_RANGE requires 2700s (45 min) timer."""
        key = (MarketRegime.BULL_TREND, MarketRegime.TIGHT_RANGE)
        assert TRANSITION_TIMERS.get(key) == 2700.0

    @pytest.mark.asyncio
    async def test_bull_to_range_no_smc_required(self):
        """BULL_TREND → TIGHT_RANGE has no SMC signal requirement."""
        key = (MarketRegime.BULL_TREND, MarketRegime.TIGHT_RANGE)
        assert TRANSITION_SMC_REQUIREMENTS.get(key) is None

    @pytest.mark.asyncio
    async def test_bull_to_range_enters_pre_switch(self):
        """BULL_TREND → TIGHT_RANGE enters PRE_SWITCH."""
        registry = _make_registry_with_strategies()
        selector = _selector_with_long_timers(registry, require_smc=False)
        await registry.start_strategy("trend-1")
        selector._current_regime = MarketRegime.BULL_TREND

        analysis = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        result = selector.select(analysis)
        assert result.transition_needed is False
        assert selector.transition_phase == TransitionPhase.PRE_SWITCH

    @pytest.mark.asyncio
    async def test_bear_to_wide_range_requires_45min_timer(self):
        key = (MarketRegime.BEAR_TREND, MarketRegime.WIDE_RANGE)
        assert TRANSITION_TIMERS.get(key) == 2700.0


class TestTransitionType3TrendToCounterTrend:
    """Trend → Counter-trend transitions (60 min timer, CHoCH confirmation)."""

    @pytest.mark.asyncio
    async def test_bull_to_bear_requires_60min_timer(self):
        key = (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND)
        assert TRANSITION_TIMERS.get(key) == 3600.0

    @pytest.mark.asyncio
    async def test_bull_to_bear_requires_choch(self):
        key = (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND)
        assert TRANSITION_SMC_REQUIREMENTS.get(key) == "CHoCH"

    @pytest.mark.asyncio
    async def test_bear_to_bull_requires_60min_timer(self):
        key = (MarketRegime.BEAR_TREND, MarketRegime.BULL_TREND)
        assert TRANSITION_TIMERS.get(key) == 3600.0

    @pytest.mark.asyncio
    async def test_bear_to_bull_requires_choch(self):
        key = (MarketRegime.BEAR_TREND, MarketRegime.BULL_TREND)
        assert TRANSITION_SMC_REQUIREMENTS.get(key) == "CHoCH"

    @pytest.mark.asyncio
    async def test_counter_trend_blocked_without_choch(self):
        """Counter-trend transition blocked if no CHoCH present."""
        registry = _make_registry_with_strategies()
        selector = _selector_with_zero_timers(registry, require_smc=True)
        await registry.start_strategy("trend-1")
        selector._current_regime = MarketRegime.BULL_TREND

        # No SMC signal → PRE_SWITCH entered but SMC not confirmed
        analysis = _make_analysis(
            regime=MarketRegime.BEAR_TREND,
            recommended=RecommendedStrategy.DCA,
            smc_signal=None,
        )
        result = selector.select(analysis)
        # First call → enters PRE_SWITCH
        assert result.transition_needed is False
        assert selector.transition_phase == TransitionPhase.PRE_SWITCH

        # Second call: timer is zero so timer_ok=True, but SMC still missing
        result2 = selector.select(analysis)
        assert result2.transition_needed is False

    @pytest.mark.asyncio
    async def test_counter_trend_allowed_with_choch_and_timer(self):
        """Counter-trend transition proceeds with CHoCH + timer met."""
        registry = _make_registry_with_strategies()
        selector = _selector_with_zero_timers(registry, require_smc=True)
        await registry.start_strategy("trend-1")
        selector._current_regime = MarketRegime.BULL_TREND

        # First call → PRE_SWITCH
        analysis_no_smc = _make_analysis(
            regime=MarketRegime.BEAR_TREND,
            recommended=RecommendedStrategy.DCA,
            smc_signal=None,
        )
        selector.select(analysis_no_smc)
        assert selector.transition_phase == TransitionPhase.PRE_SWITCH

        # Second call WITH CHoCH — timer=0 so both criteria now met → CONFIRMED
        analysis_with_choch = _make_analysis(
            regime=MarketRegime.BEAR_TREND,
            recommended=RecommendedStrategy.DCA,
            smc_signal="CHoCH",
        )
        result2 = selector.select(analysis_with_choch)
        # Should proceed (CONFIRMED) — transition_needed=True
        assert result2.transition_needed is True
        assert selector.transition_phase == TransitionPhase.CONFIRMED


class TestTransitionType4ToAccumulation:
    """Any → ACCUMULATION transitions (30 min, CHoCH + OB)."""

    @pytest.mark.asyncio
    async def test_range_to_accumulation_requires_30min(self):
        key = (MarketRegime.TIGHT_RANGE, MarketRegime.ACCUMULATION)
        assert TRANSITION_TIMERS.get(key) == 1800.0

    @pytest.mark.asyncio
    async def test_trend_to_accumulation_requires_choch(self):
        key = (MarketRegime.BULL_TREND, MarketRegime.ACCUMULATION)
        assert TRANSITION_SMC_REQUIREMENTS.get(key) == "CHoCH"

    @pytest.mark.asyncio
    async def test_bear_trend_to_accumulation_requires_30min(self):
        key = (MarketRegime.BEAR_TREND, MarketRegime.ACCUMULATION)
        assert TRANSITION_TIMERS.get(key) == 1800.0


class TestTransitionType5ToDistribution:
    """Any → DISTRIBUTION transitions (30 min, CHoCH + OB)."""

    @pytest.mark.asyncio
    async def test_range_to_distribution_requires_30min(self):
        key = (MarketRegime.TIGHT_RANGE, MarketRegime.DISTRIBUTION)
        assert TRANSITION_TIMERS.get(key) == 1800.0

    @pytest.mark.asyncio
    async def test_trend_to_distribution_requires_choch(self):
        key = (MarketRegime.BULL_TREND, MarketRegime.DISTRIBUTION)
        assert TRANSITION_SMC_REQUIREMENTS.get(key) == "CHoCH"

    @pytest.mark.asyncio
    async def test_bear_trend_to_distribution_requires_30min(self):
        key = (MarketRegime.BEAR_TREND, MarketRegime.DISTRIBUTION)
        assert TRANSITION_TIMERS.get(key) == 1800.0


# ---------------------------------------------------------------------------
# Integration: false signal → PRE_SWITCH → STABLE (no actual switch)
# ---------------------------------------------------------------------------


class TestIntegrationFalseSignalCancelled:
    """A false signal enters PRE_SWITCH but regime returns to stable — no switch."""

    @pytest.mark.asyncio
    async def test_false_signal_returns_to_stable_without_switch(self):
        """
        Scenario:
        1. TIGHT_RANGE is active (grid running).
        2. BULL_TREND briefly detected → PRE_SWITCH entered.
        3. Next tick: TIGHT_RANGE detected again → PRE_SWITCH cancelled.
        4. No transition executed; grid stays active; cancelled count = 1.
        """
        registry = _make_registry_with_strategies()
        selector = _selector_with_long_timers(registry, require_smc=True)

        # Establish initial regime: TIGHT_RANGE
        first = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        result = selector.select(first)
        await selector.execute_transition(result)
        assert selector.current_regime == MarketRegime.TIGHT_RANGE
        await registry.start_strategy("grid-1")

        # Tick 2: BULL_TREND detected → PRE_SWITCH entered
        bull = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
        )
        result2 = selector.select(bull)
        assert result2.transition_needed is False
        assert selector.transition_phase == TransitionPhase.PRE_SWITCH
        assert selector.cancelled_pre_switch_count == 0

        # Tick 3: regime returns to TIGHT_RANGE (false alarm)
        stable_again = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        result3 = selector.select(stable_again)
        # No transition — regime matched active regime
        assert result3.transition_needed is False
        assert selector.transition_phase == TransitionPhase.STABLE
        assert selector.cancelled_pre_switch_count == 1

        # Grid is still active (was never stopped)
        assert registry.get("grid-1").state == StrategyState.ACTIVE

    @pytest.mark.asyncio
    async def test_cancelled_pre_switch_count_increments(self):
        """Multiple false alarms each increment the counter."""
        registry = _make_registry_with_strategies()
        selector = _selector_with_long_timers(registry, require_smc=True)

        # Establish initial regime
        first = _make_analysis(regime=MarketRegime.TIGHT_RANGE)
        await selector.execute_transition(selector.select(first))
        await registry.start_strategy("grid-1")

        for _ in range(3):
            # Introduce false BULL_TREND signal
            selector.select(
                _make_analysis(
                    regime=MarketRegime.BULL_TREND,
                    recommended=RecommendedStrategy.DCA,
                )
            )
            # Regime returns to TIGHT_RANGE
            selector.select(
                _make_analysis(
                    regime=MarketRegime.TIGHT_RANGE,
                    recommended=RecommendedStrategy.GRID,
                )
            )

        assert selector.cancelled_pre_switch_count == 3


# ---------------------------------------------------------------------------
# Integration: confirmed signal → CONFIRMED → TRANSITIONING → new regime
# ---------------------------------------------------------------------------


class TestIntegrationConfirmedTransition:
    """A confirmed signal progresses through the full two-phase pipeline."""

    @pytest.mark.asyncio
    async def test_confirmed_signal_executes_transition(self):
        """
        Scenario:
        1. TIGHT_RANGE active (grid running).
        2. BULL_TREND detected → PRE_SWITCH entered.
        3. BOS signal + zero timer → CONFIRMED.
        4. execute_transition() called → STABLE (completed), grid stopped, DCA active.
        """
        registry = _make_registry_with_strategies()
        selector = _selector_with_zero_timers(registry, require_smc=True)

        # Establish TIGHT_RANGE
        first = _make_analysis(regime=MarketRegime.TIGHT_RANGE)
        await selector.execute_transition(selector.select(first))
        await registry.start_strategy("grid-1")
        assert selector.current_regime == MarketRegime.TIGHT_RANGE

        # Tick 1: BULL_TREND detected — enter PRE_SWITCH (timer=0 but needs BOS)
        bull_no_bos = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
            smc_signal=None,
        )
        r1 = selector.select(bull_no_bos)
        assert r1.transition_needed is False
        assert selector.transition_phase == TransitionPhase.PRE_SWITCH

        # Tick 2: BOS confirmed (timer still 0 → both criteria met → CONFIRMED)
        bull_bos = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
            smc_signal="BOS",
        )
        r2 = selector.select(bull_bos)
        assert r2.transition_needed is True
        assert selector.transition_phase == TransitionPhase.CONFIRMED

        # Execute the confirmed transition
        record = await selector.execute_transition(r2)
        assert record.state == TransitionState.COMPLETED
        assert selector.current_regime == MarketRegime.BULL_TREND
        assert selector.transition_phase == TransitionPhase.STABLE

        # Grid stopped; DCA (or trend_follower) active
        assert registry.get("grid-1").state == StrategyState.STOPPED

    @pytest.mark.asyncio
    async def test_transition_phase_resets_after_completion(self):
        """After a completed transition, phase returns to STABLE."""
        registry = _make_registry_with_strategies()
        selector = _selector_with_zero_timers(registry, require_smc=False)

        # Establish TIGHT_RANGE
        first = _make_analysis(regime=MarketRegime.TIGHT_RANGE)
        await selector.execute_transition(selector.select(first))
        await registry.start_strategy("grid-1")

        # PRE_SWITCH → CONFIRMED (no SMC required, timer=0 → immediate)
        bull = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
        )
        r1 = selector.select(bull)
        assert r1.transition_needed is False  # first call enters PRE_SWITCH

        r2 = selector.select(bull)  # second call — timer cleared → CONFIRMED
        assert r2.transition_needed is True

        await selector.execute_transition(r2)
        assert selector.transition_phase == TransitionPhase.STABLE


# ---------------------------------------------------------------------------
# PRE_SWITCH target regime change
# ---------------------------------------------------------------------------


class TestPreSwitchTargetChange:
    """When the target regime changes during PRE_SWITCH, restart the timer."""

    @pytest.mark.asyncio
    async def test_target_regime_change_restarts_pre_switch(self):
        registry = _make_registry_with_strategies()
        selector = _selector_with_long_timers(registry, require_smc=False)

        # Establish TIGHT_RANGE
        first = _make_analysis(regime=MarketRegime.TIGHT_RANGE)
        await selector.execute_transition(selector.select(first))
        await registry.start_strategy("grid-1")

        # Enter PRE_SWITCH targeting BULL_TREND
        selector.select(
            _make_analysis(
                regime=MarketRegime.BULL_TREND,
                recommended=RecommendedStrategy.DCA,
            )
        )
        assert selector.transition_phase == TransitionPhase.PRE_SWITCH
        assert selector._pre_switch_target_regime == MarketRegime.BULL_TREND
        first_start = selector._pre_switch_started_at

        # Now the detected regime changes to BEAR_TREND → target mismatch
        import time

        time.sleep(0.01)  # tiny sleep so timestamps differ
        selector.select(
            _make_analysis(
                regime=MarketRegime.BEAR_TREND,
                recommended=RecommendedStrategy.DCA,
            )
        )
        # PRE_SWITCH restarted with new target
        assert selector.transition_phase == TransitionPhase.PRE_SWITCH
        assert selector._pre_switch_target_regime == MarketRegime.BEAR_TREND
        # Timestamp should have been reset (new start time)
        assert selector._pre_switch_started_at >= first_start
        # cancelled_pre_switch_count incremented for the abandoned BULL_TREND pre-switch
        assert selector.cancelled_pre_switch_count == 1


# ---------------------------------------------------------------------------
# get_status includes two-phase fields
# ---------------------------------------------------------------------------


class TestSelectorStatusTwoPhase:
    """get_status() exposes PRE_SWITCH information."""

    def test_initial_status_two_phase_fields(self):
        registry = StrategyRegistry()
        selector = StrategySelector(registry, require_smc_confirmation=True)
        status = selector.get_status()

        assert status["transition_phase"] == "stable"
        assert status["pre_switch_target_regime"] is None
        assert status["pre_switch_started_at"] is None
        assert status["pre_switch_smc_confirmed"] is False
        assert status["cancelled_pre_switch_count"] == 0
        assert status["require_smc_confirmation"] is True
        assert status["tighten_stops_on_pre_switch"] is True

    @pytest.mark.asyncio
    async def test_status_during_pre_switch(self):
        registry = _make_registry_with_strategies()
        selector = _selector_with_long_timers(registry, require_smc=True)

        # Establish initial regime
        first = _make_analysis(regime=MarketRegime.TIGHT_RANGE)
        await selector.execute_transition(selector.select(first))
        await registry.start_strategy("grid-1")

        # Enter PRE_SWITCH
        selector.select(
            _make_analysis(
                regime=MarketRegime.BULL_TREND,
                recommended=RecommendedStrategy.DCA,
            )
        )

        status = selector.get_status()
        assert status["transition_phase"] == "pre_switch"
        assert status["pre_switch_target_regime"] == "bull_trend"
        assert status["pre_switch_started_at"] is not None


# ---------------------------------------------------------------------------
# SMC signal detection
# ---------------------------------------------------------------------------


class TestSMCSignalDetection:
    """SMC signal is read from analysis_details["smc_signal"]."""

    @pytest.mark.asyncio
    async def test_bos_signal_detected(self):
        registry = _make_registry_with_strategies()
        selector = _selector_with_zero_timers(registry, require_smc=True)

        # Establish initial regime
        first = _make_analysis(regime=MarketRegime.TIGHT_RANGE)
        await selector.execute_transition(selector.select(first))
        await registry.start_strategy("grid-1")

        # PRE_SWITCH with no SMC
        selector.select(
            _make_analysis(
                regime=MarketRegime.BULL_TREND,
                recommended=RecommendedStrategy.DCA,
                smc_signal=None,
            )
        )
        assert not selector._pre_switch_smc_confirmed

        # Next tick with BOS
        result = selector.select(
            _make_analysis(
                regime=MarketRegime.BULL_TREND,
                recommended=RecommendedStrategy.DCA,
                smc_signal="BOS",
            )
        )
        # Timer=0 + BOS → CONFIRMED → transition_needed=True
        assert result.transition_needed is True

    @pytest.mark.asyncio
    async def test_wrong_smc_signal_not_accepted(self):
        """CHoCH when BOS is required should NOT confirm the transition."""
        registry = _make_registry_with_strategies()
        selector = _selector_with_zero_timers(registry, require_smc=True)

        # Establish initial regime
        first = _make_analysis(regime=MarketRegime.TIGHT_RANGE)
        await selector.execute_transition(selector.select(first))
        await registry.start_strategy("grid-1")

        # PRE_SWITCH
        selector.select(
            _make_analysis(
                regime=MarketRegime.BULL_TREND,
                recommended=RecommendedStrategy.DCA,
                smc_signal=None,
            )
        )

        # Wrong signal (CHoCH instead of BOS)
        result = selector.select(
            _make_analysis(
                regime=MarketRegime.BULL_TREND,
                recommended=RecommendedStrategy.DCA,
                smc_signal="CHoCH",
            )
        )
        # Should still be blocked — SMC not confirmed with wrong type
        assert result.transition_needed is False

    def test_no_smc_requirement_always_confirmed(self):
        """When no SMC is required for a transition pair, confirmation is immediate."""
        registry = StrategyRegistry()
        selector = StrategySelector(
            registry,
            transition_cooldown_seconds=0,
            min_regime_duration_seconds=0,
            pre_switch_duration_seconds=0.0,
            require_smc_confirmation=True,  # global flag is True
        )
        selector._current_regime = MarketRegime.BULL_TREND

        # BULL_TREND → TIGHT_RANGE has no SMC requirement
        analysis = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        confirmed = selector._check_smc_signal(
            from_regime=MarketRegime.BULL_TREND,
            to_regime=MarketRegime.TIGHT_RANGE,
            analysis=analysis,
        )
        assert confirmed is True


# ---------------------------------------------------------------------------
# require_smc_confirmation=False bypasses SMC gate entirely
# ---------------------------------------------------------------------------


class TestSMCConfirmationDisabled:
    """When require_smc_confirmation=False, timer alone suffices."""

    @pytest.mark.asyncio
    async def test_no_smc_requirement_flag_bypasses_gate(self):
        registry = _make_registry_with_strategies()
        selector = _selector_with_zero_timers(registry, require_smc=False)

        # Establish initial regime
        first = _make_analysis(regime=MarketRegime.TIGHT_RANGE)
        await selector.execute_transition(selector.select(first))
        await registry.start_strategy("grid-1")

        # Tick 1 → PRE_SWITCH (timer=0 means it will clear on next tick)
        r1 = selector.select(
            _make_analysis(
                regime=MarketRegime.BULL_TREND,
                recommended=RecommendedStrategy.DCA,
                smc_signal=None,
            )
        )
        assert r1.transition_needed is False  # first call enters PRE_SWITCH

        # Tick 2 → CONFIRMED (no SMC needed, timer=0)
        r2 = selector.select(
            _make_analysis(
                regime=MarketRegime.BULL_TREND,
                recommended=RecommendedStrategy.DCA,
                smc_signal=None,
            )
        )
        assert r2.transition_needed is True
        assert selector.transition_phase == TransitionPhase.CONFIRMED
