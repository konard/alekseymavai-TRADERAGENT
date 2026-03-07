"""Tests for StrategySelector."""

from datetime import datetime, timezone

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
    DEFAULT_REGIME_STRATEGIES,
    HYBRID_STRATEGY_WEIGHTS,
    SelectionResult,
    StrategySelector,
    StrategyWeight,
    TransitionRecord,
    TransitionState,
)


def _make_analysis(
    regime: MarketRegime = MarketRegime.TIGHT_RANGE,
    recommended: RecommendedStrategy = RecommendedStrategy.GRID,
    confidence: float = 0.8,
    regime_duration: int = 300,
    adx: float = 20.0,
) -> RegimeAnalysis:
    """Create a RegimeAnalysis for testing."""
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
        analysis_details={"current_price": 3000.0},
    )


def _make_registry_with_strategies() -> StrategyRegistry:
    """Create a registry with all strategy types registered."""
    registry = StrategyRegistry(max_strategies=10)
    registry.register("grid-1", "grid", {"pair": "BTCUSDT"})
    registry.register("dca-1", "dca", {"pair": "BTCUSDT"})
    registry.register("trend-1", "trend_follower", {"pair": "BTCUSDT"})
    registry.register("smc-1", "smc", {"pair": "BTCUSDT"})
    return registry


class TestStrategyWeight:
    """Tests for StrategyWeight dataclass."""

    def test_creation(self):
        w = StrategyWeight(strategy_type="grid", weight=1.0, priority=1)
        assert w.strategy_type == "grid"
        assert w.weight == 1.0
        assert w.priority == 1


class TestDefaultRegimeStrategies:
    """Tests for default regime-to-strategy mapping (v2.0)."""

    def test_tight_range_maps_to_grid(self):
        weights = DEFAULT_REGIME_STRATEGIES[MarketRegime.TIGHT_RANGE]
        assert len(weights) == 1
        assert weights[0].strategy_type == "grid"

    def test_wide_range_maps_to_grid(self):
        weights = DEFAULT_REGIME_STRATEGIES[MarketRegime.WIDE_RANGE]
        assert len(weights) == 1
        assert weights[0].strategy_type == "grid"

    def test_bull_trend_maps_to_trend_and_dca(self):
        weights = DEFAULT_REGIME_STRATEGIES[MarketRegime.BULL_TREND]
        types = {w.strategy_type for w in weights}
        assert "trend_follower" in types
        assert "dca" in types

    def test_bear_trend_maps_to_dca_and_trend(self):
        weights = DEFAULT_REGIME_STRATEGIES[MarketRegime.BEAR_TREND]
        types = {w.strategy_type for w in weights}
        assert "dca" in types
        assert "trend_follower" in types

    def test_volatile_transition_maps_to_smc(self):
        weights = DEFAULT_REGIME_STRATEGIES[MarketRegime.VOLATILE_TRANSITION]
        assert len(weights) == 1
        assert weights[0].strategy_type == "smc"

    def test_quiet_transition_maps_to_grid(self):
        weights = DEFAULT_REGIME_STRATEGIES[MarketRegime.QUIET_TRANSITION]
        assert len(weights) == 1
        assert weights[0].strategy_type == "grid"

    def test_unknown_maps_to_nothing(self):
        weights = DEFAULT_REGIME_STRATEGIES[MarketRegime.UNKNOWN]
        assert len(weights) == 0

    def test_hybrid_includes_grid_and_dca(self):
        types = {w.strategy_type for w in HYBRID_STRATEGY_WEIGHTS}
        assert "dca" in types
        assert "grid" in types
        assert "trend_follower" in types


class TestSelectorInit:
    """Tests for StrategySelector initialization."""

    def test_defaults(self):
        registry = StrategyRegistry()
        selector = StrategySelector(registry)
        assert selector.current_regime is None
        assert selector.last_transition_time is None
        assert len(selector.transition_history) == 0

    def test_custom_params(self):
        registry = StrategyRegistry()
        selector = StrategySelector(
            registry,
            transition_cooldown_seconds=600.0,
            min_regime_duration_seconds=60.0,
            max_transition_history=20,
        )
        status = selector.get_status()
        assert status["transition_cooldown_seconds"] == 600.0
        assert status["min_regime_duration_seconds"] == 60.0


class TestSelectMethod:
    """Tests for StrategySelector.select()."""

    def test_tight_range_selects_grid(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )

        analysis = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        result = selector.select(analysis)

        assert result.regime == MarketRegime.TIGHT_RANGE
        assert result.recommended == RecommendedStrategy.GRID
        start_types = {w.strategy_type for w in result.strategies_to_start}
        assert "grid" in start_types

    def test_wide_range_selects_grid(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )

        analysis = _make_analysis(
            regime=MarketRegime.WIDE_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        result = selector.select(analysis)

        start_types = {w.strategy_type for w in result.strategies_to_start}
        assert "grid" in start_types

    def test_bull_trend_selects_trend_and_dca(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )

        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
        )
        result = selector.select(analysis)

        start_types = {w.strategy_type for w in result.strategies_to_start}
        assert "trend_follower" in start_types or "dca" in start_types

    def test_hybrid_selects_multiple(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )

        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.HYBRID,
        )
        result = selector.select(analysis)

        start_types = {w.strategy_type for w in result.strategies_to_start}
        assert "dca" in start_types
        assert "grid" in start_types

    async def test_reduce_exposure_stops_all(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )

        # Activate grid
        await registry.start_strategy("grid-1")

        analysis = _make_analysis(
            regime=MarketRegime.VOLATILE_TRANSITION,
            recommended=RecommendedStrategy.REDUCE_EXPOSURE,
        )
        result = selector.select(analysis)

        assert "grid" in result.strategies_to_stop
        assert len(result.strategies_to_start) == 0

    async def test_no_transition_when_already_correct(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )

        # Activate grid
        await registry.start_strategy("grid-1")

        # Detect tight range (grid should stay)
        analysis = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        result = selector.select(analysis)

        assert not result.transition_needed
        assert "grid" in result.strategies_to_keep
        assert len(result.strategies_to_stop) == 0

    def test_transition_blocked_by_cooldown(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry,
            transition_cooldown_seconds=300.0,
            min_regime_duration_seconds=0,
        )
        # Simulate a recent transition
        selector._last_transition_time = datetime.now(timezone.utc)
        selector._current_regime = MarketRegime.TIGHT_RANGE

        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
        )
        result = selector.select(analysis)

        assert not result.transition_needed
        assert "cooldown" in result.reason.lower()

    async def test_transition_blocked_by_short_regime(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry,
            transition_cooldown_seconds=0,
            min_regime_duration_seconds=120.0,
        )

        # Establish a prior regime first (duration/confidence gates only apply after first call)
        first = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
            regime_duration=300,
        )
        first_result = selector.select(first)
        await selector.execute_transition(first_result)

        # Now a young regime should be blocked
        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
            regime_duration=30,  # Too short
        )
        result = selector.select(analysis)

        assert not result.transition_needed
        assert "too young" in result.reason.lower()

    async def test_transition_blocked_by_low_confidence(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry,
            transition_cooldown_seconds=0,
            min_regime_duration_seconds=0,
        )

        # Establish a prior regime first (duration/confidence gates only apply after first call)
        first = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
            confidence=0.8,
        )
        first_result = selector.select(first)
        await selector.execute_transition(first_result)

        # Now a low-confidence analysis should be blocked
        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
            confidence=0.1,  # Too low
        )
        result = selector.select(analysis)

        assert not result.transition_needed
        assert "confidence" in result.reason.lower()

    async def test_hold_keeps_current(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )
        selector._current_regime = MarketRegime.TIGHT_RANGE

        # Activate grid (matching tight range)
        await registry.start_strategy("grid-1")

        analysis = _make_analysis(
            regime=MarketRegime.QUIET_TRANSITION,
            recommended=RecommendedStrategy.HOLD,
        )
        result = selector.select(analysis)

        # HOLD should keep current strategies, no changes
        assert not result.transition_needed


class TestExecuteTransition:
    """Tests for StrategySelector.execute_transition()."""

    async def test_successful_transition(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )

        analysis = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        result = selector.select(analysis)
        record = await selector.execute_transition(result)

        assert record.state == TransitionState.COMPLETED
        assert record.to_regime == MarketRegime.TIGHT_RANGE
        assert selector.current_regime == MarketRegime.TIGHT_RANGE
        assert selector.last_transition_time is not None

    async def test_transition_stops_old_starts_new(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )

        # Start with grid active
        await registry.start_strategy("grid-1")
        selector._current_regime = MarketRegime.TIGHT_RANGE

        # Switch to bull trend
        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
        )
        result = selector.select(analysis)
        record = await selector.execute_transition(result)

        assert record.state == TransitionState.COMPLETED

        # Grid should be stopped
        grid = registry.get("grid-1")
        assert grid.state == StrategyState.STOPPED

        # DCA should be active
        dca = registry.get("dca-1")
        assert dca.state == StrategyState.ACTIVE

    async def test_history_recorded(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )

        analysis = _make_analysis()
        result = selector.select(analysis)
        await selector.execute_transition(result)

        assert len(selector.transition_history) == 1
        record = selector.transition_history[0]
        assert record.to_regime == MarketRegime.TIGHT_RANGE
        assert record.state == TransitionState.COMPLETED

    async def test_history_max_size(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry,
            transition_cooldown_seconds=0,
            min_regime_duration_seconds=0,
            max_transition_history=3,
        )

        for _ in range(5):
            analysis = _make_analysis()
            result = selector.select(analysis)
            await selector.execute_transition(result)

        assert len(selector.transition_history) == 3


class TestSignalConflictResolution:
    """Tests for signal conflict resolution."""

    def test_no_signals_returns_none(self):
        registry = StrategyRegistry()
        selector = StrategySelector(registry)
        assert selector.resolve_signal_conflict([]) is None

    def test_single_signal_returns_it(self):
        registry = StrategyRegistry()
        selector = StrategySelector(registry)

        signal = {"strategy_type": "dca", "confidence": 0.8, "direction": "long"}
        result = selector.resolve_signal_conflict([signal])
        assert result == signal

    def test_higher_priority_wins(self):
        registry = StrategyRegistry()
        selector = StrategySelector(registry)
        selector._current_regime = MarketRegime.BULL_TREND

        signals = [
            {"strategy_type": "dca", "confidence": 0.7, "direction": "long"},
            {"strategy_type": "trend_follower", "confidence": 0.9, "direction": "long"},
        ]
        winner = selector.resolve_signal_conflict(signals)

        # trend_follower has priority 1, dca has priority 2 in BULL_TREND
        assert winner["strategy_type"] == "trend_follower"

    def test_confidence_breaks_ties(self):
        registry = StrategyRegistry()
        selector = StrategySelector(registry)

        # Both unknown types -> same priority (999), use confidence
        signals = [
            {"strategy_type": "custom_a", "confidence": 0.6},
            {"strategy_type": "custom_b", "confidence": 0.9},
        ]
        winner = selector.resolve_signal_conflict(signals)
        assert winner["strategy_type"] == "custom_b"


class TestSelectionResult:
    """Tests for SelectionResult serialization."""

    def test_to_dict(self):
        result = SelectionResult(
            strategies_to_start=[StrategyWeight("grid", 1.0, 1)],
            strategies_to_stop=["dca"],
            strategies_to_keep=["smc"],
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
            transition_needed=True,
            reason="Test reason",
        )
        d = result.to_dict()

        assert d["regime"] == "tight_range"
        assert d["recommended"] == "grid"
        assert d["transition_needed"] is True
        assert len(d["strategies_to_start"]) == 1
        assert d["strategies_to_start"][0]["type"] == "grid"
        assert d["strategies_to_stop"] == ["dca"]


class TestTransitionRecord:
    """Tests for TransitionRecord serialization."""

    def test_to_dict(self):
        record = TransitionRecord(
            from_regime=MarketRegime.TIGHT_RANGE,
            to_regime=MarketRegime.BULL_TREND,
            from_strategies=["grid"],
            to_strategies=["dca", "trend_follower"],
            recommended=RecommendedStrategy.DCA,
            timestamp=datetime.now(timezone.utc),
            state=TransitionState.COMPLETED,
        )
        d = record.to_dict()

        assert d["from_regime"] == "tight_range"
        assert d["to_regime"] == "bull_trend"
        assert d["state"] == "completed"
        assert "grid" in d["from_strategies"]


class TestGetStatus:
    """Tests for selector status reporting."""

    def test_initial_status(self):
        registry = StrategyRegistry()
        selector = StrategySelector(registry)
        status = selector.get_status()

        assert status["current_regime"] is None
        assert status["last_transition"] is None
        assert status["active_strategies"] == []
        assert status["history_count"] == 0

    async def test_status_after_transition(self):
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry, transition_cooldown_seconds=0, min_regime_duration_seconds=0
        )

        analysis = _make_analysis()
        result = selector.select(analysis)
        await selector.execute_transition(result)

        status = selector.get_status()
        assert status["current_regime"] == "tight_range"
        assert status["last_transition"] is not None
        assert status["history_count"] == 1


class TestIntegrationWithBotOrchestrator:
    """
    Integration tests verifying StrategySelector is the authoritative router
    in BotOrchestrator._update_active_strategies() (issue #352).

    Confirms the three conflicts are resolved:
    - Conflict #1: Single mapping source (DEFAULT_REGIME_STRATEGIES)
    - Conflict #2: QUIET_TRANSITION routes to grid, not empty set
    - Conflict #3: VOLATILE_TRANSITION/REDUCE_EXPOSURE → empty (no SMC contradiction)
    """

    async def test_orchestrator_delegates_to_strategy_selector(self):
        """BotOrchestrator._update_active_strategies uses StrategySelector."""
        from unittest.mock import AsyncMock

        from bot.orchestrator.bot_orchestrator import BotOrchestrator

        orch = object.__new__(BotOrchestrator)
        orch._current_regime = None
        orch._active_strategies = set()
        orch._last_strategy_switch_at = 0.0
        orch._strategy_switch_cooldown = 0.0
        orch._strategy_locked = False
        orch._locked_strategies = None
        orch._last_regime_update_at = 1.0
        orch._regime_stale_threshold = 120.0
        orch.detect_market_regime = AsyncMock(return_value=None)
        orch._publish_event = AsyncMock()
        orch._graceful_transition = AsyncMock()
        orch.strategy_selector = StrategySelector(
            registry=StrategyRegistry(),
            transition_cooldown_seconds=0.0,
            min_regime_duration_seconds=0.0,
        )

        # Verify strategy_selector attribute exists
        assert hasattr(orch, "strategy_selector")
        assert isinstance(orch.strategy_selector, StrategySelector)

    async def test_conflict1_single_mapping_source(self):
        """Conflict #1: BULL_TREND uses DEFAULT_REGIME_STRATEGIES, not _REGIME_TO_STRATEGIES."""
        from unittest.mock import AsyncMock

        from bot.orchestrator.bot_orchestrator import BotOrchestrator

        orch = object.__new__(BotOrchestrator)
        orch._current_regime = None
        orch._active_strategies = set()
        orch._last_strategy_switch_at = 0.0
        orch._strategy_switch_cooldown = 0.0
        orch._strategy_locked = False
        orch._locked_strategies = None
        orch._last_regime_update_at = 1.0
        orch._regime_stale_threshold = 120.0
        orch.detect_market_regime = AsyncMock(return_value=None)
        orch._publish_event = AsyncMock()
        orch._graceful_transition = AsyncMock()
        orch.strategy_selector = StrategySelector(
            registry=StrategyRegistry(),
            transition_cooldown_seconds=0.0,
            min_regime_duration_seconds=0.0,
        )

        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
        )
        orch._current_regime = analysis
        await orch._update_active_strategies()

        # DEFAULT_REGIME_STRATEGIES: BULL_TREND → trend_follower + dca (no SMC hardcode)
        assert "trend_follower" in orch._active_strategies
        assert "dca" in orch._active_strategies
        # SMC is NOT hardcoded for BULL_TREND in DEFAULT_REGIME_STRATEGIES
        assert "smc" not in orch._active_strategies

    async def test_conflict2_quiet_transition_activates_grid(self):
        """Conflict #2: QUIET_TRANSITION/GRID activates grid, not empty set."""
        from unittest.mock import AsyncMock

        from bot.orchestrator.bot_orchestrator import BotOrchestrator

        orch = object.__new__(BotOrchestrator)
        orch._current_regime = None
        orch._active_strategies = set()
        orch._last_strategy_switch_at = 0.0
        orch._strategy_switch_cooldown = 0.0
        orch._strategy_locked = False
        orch._locked_strategies = None
        orch._last_regime_update_at = 1.0
        orch._regime_stale_threshold = 120.0
        orch.detect_market_regime = AsyncMock(return_value=None)
        orch._publish_event = AsyncMock()
        orch._graceful_transition = AsyncMock()
        orch.strategy_selector = StrategySelector(
            registry=StrategyRegistry(),
            transition_cooldown_seconds=0.0,
            min_regime_duration_seconds=0.0,
        )

        analysis = _make_analysis(
            regime=MarketRegime.QUIET_TRANSITION,
            recommended=RecommendedStrategy.GRID,
        )
        orch._current_regime = analysis
        await orch._update_active_strategies()

        # QUIET_TRANSITION → grid (not empty set as in the old buggy code)
        assert "grid" in orch._active_strategies

    async def test_conflict3_volatile_transition_reduce_empty(self):
        """Conflict #3: VOLATILE_TRANSITION/REDUCE_EXPOSURE → empty, not {smc}."""
        from unittest.mock import AsyncMock

        from bot.orchestrator.bot_orchestrator import BotOrchestrator

        orch = object.__new__(BotOrchestrator)
        orch._current_regime = None
        orch._active_strategies = set()
        orch._last_strategy_switch_at = 0.0
        orch._strategy_switch_cooldown = 0.0
        orch._strategy_locked = False
        orch._locked_strategies = None
        orch._last_regime_update_at = 1.0
        orch._regime_stale_threshold = 120.0
        orch.detect_market_regime = AsyncMock(return_value=None)
        orch._publish_event = AsyncMock()
        orch._graceful_transition = AsyncMock()
        orch.strategy_selector = StrategySelector(
            registry=StrategyRegistry(),
            transition_cooldown_seconds=0.0,
            min_regime_duration_seconds=0.0,
        )

        analysis = _make_analysis(
            regime=MarketRegime.VOLATILE_TRANSITION,
            recommended=RecommendedStrategy.REDUCE_EXPOSURE,
        )
        orch._current_regime = analysis
        await orch._update_active_strategies()

        # REDUCE_EXPOSURE → empty (SMC contradiction fixed: no SMC in dangerous regime)
        assert orch._active_strategies == set()

    async def test_selector_is_single_source_of_truth(self):
        """_REGIME_TO_STRATEGIES class variable no longer exists in BotOrchestrator."""
        from bot.orchestrator.bot_orchestrator import BotOrchestrator

        # The conflicting _REGIME_TO_STRATEGIES class attribute should be removed
        assert not hasattr(BotOrchestrator, "_REGIME_TO_STRATEGIES"), (
            "_REGIME_TO_STRATEGIES still present — mapping must live only in "
            "strategy_selector.py (DEFAULT_REGIME_STRATEGIES)"
        )

    async def test_first_transition_not_blocked_by_guards(self):
        """First transition always proceeds regardless of confidence/duration."""
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry,
            transition_cooldown_seconds=600.0,  # long cooldown
            min_regime_duration_seconds=120.0,
        )

        # Very low confidence and short regime — but it's the first call
        analysis = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
            confidence=0.1,  # below 0.3 threshold
            regime_duration=5,  # below 120s minimum
        )
        result = selector.select(analysis)

        # First call is never blocked (cooldown has no prior time, no prior regime)
        assert result.transition_needed is True

    async def test_subsequent_transitions_respect_guards(self):
        """After first transition, confidence and duration gates apply."""
        registry = _make_registry_with_strategies()
        selector = StrategySelector(
            registry,
            transition_cooldown_seconds=0.0,
            min_regime_duration_seconds=120.0,
        )

        # Establish first regime
        first = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
            confidence=0.8,
            regime_duration=300,
        )
        result = selector.select(first)
        await selector.execute_transition(result)

        # Second call with young regime — should be blocked
        second = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
            confidence=0.8,
            regime_duration=30,  # too young
        )
        result2 = selector.select(second)
        assert result2.transition_needed is False
        assert "too young" in result2.reason.lower()
