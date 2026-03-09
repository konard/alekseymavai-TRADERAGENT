"""
Behavioral synchronisation tests for StrategySelector (issue #370).

Verifies that using RoutingConfig-based routing produces results that are
behaviourally identical to the legacy hard-coded DEFAULT_REGIME_STRATEGIES /
HYBRID_STRATEGY_WEIGHTS for all market regimes defined in the original mapping.

The production routing config (configs/strategy_routing.yaml) is used as the
source of truth for the RoutingConfig-based selector so that the YAML and the
Python constants remain in sync.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.routing_config import RoutingConfig
from bot.orchestrator.strategy_registry import StrategyRegistry
from bot.orchestrator.strategy_selector import (
    DEFAULT_REGIME_STRATEGIES,
    HYBRID_STRATEGY_WEIGHTS,
    StrategySelector,
)

# Path to the production routing config
_PROD_ROUTING_CONFIG = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "configs",
    "strategy_routing.yaml",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_analysis(
    regime: MarketRegime,
    recommended: RecommendedStrategy,
    confluence_score: float = 0.5,
    confidence: float = 0.8,
    regime_duration: int = 300,
    atr_pct: float = 2.0,
) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=regime,
        confidence=confidence,
        recommended_strategy=recommended,
        confluence_score=confluence_score,
        trend_strength=0.0,
        volatility_percentile=50.0,
        ema_divergence_pct=0.0,
        atr_pct=atr_pct,
        rsi=50.0,
        adx=20.0,
        bb_width_pct=3.0,
        volume_ratio=1.0,
        regime_duration_seconds=regime_duration,
        previous_regime=None,
        timestamp=datetime.now(timezone.utc),
        analysis_details={"current_price": 3000.0},
    )


def _make_legacy_selector() -> StrategySelector:
    """Create a selector using the old hard-coded dicts (no RoutingConfig)."""
    return StrategySelector(
        registry=StrategyRegistry(max_strategies=10),
        transition_cooldown_seconds=0.0,
        min_regime_duration_seconds=0.0,
        require_smc_confirmation=False,
    )


def _make_routing_selector() -> StrategySelector:
    """Create a selector backed by the production RoutingConfig YAML."""
    routing_config = RoutingConfig(_PROD_ROUTING_CONFIG)
    return StrategySelector(
        registry=StrategyRegistry(max_strategies=10),
        transition_cooldown_seconds=0.0,
        min_regime_duration_seconds=0.0,
        require_smc_confirmation=False,
        routing_config=routing_config,
    )


def _strategy_types(selector: StrategySelector, analysis: RegimeAnalysis) -> set[str]:
    """Return the set of strategy types that would be started by select()."""
    result = selector.select(analysis)
    return {w.strategy_type for w in result.strategies_to_start}


# ---------------------------------------------------------------------------
# Regime → recommended strategy mapping for legacy selectors
# ---------------------------------------------------------------------------

# The scenarios below mirror what the MarketRegimeDetector produces for each
# regime.  We test each regime in isolation (no prior regime, first call) so
# that no PRE_SWITCH / cooldown gates can interfere.

_REGIME_SCENARIOS: list[tuple[MarketRegime, RecommendedStrategy, float]] = [
    # (regime, recommended, confluence_score)
    (MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID, 0.5),
    (MarketRegime.WIDE_RANGE, RecommendedStrategy.GRID, 0.5),
    (MarketRegime.QUIET_TRANSITION, RecommendedStrategy.GRID, 0.5),
    (MarketRegime.VOLATILE_TRANSITION, RecommendedStrategy.SMC, 0.5),
    (MarketRegime.BULL_TREND, RecommendedStrategy.DCA, 0.5),  # low confluence
    (MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID, 0.8),  # high confluence
    (MarketRegime.BEAR_TREND, RecommendedStrategy.DCA, 0.5),
]


class TestRegimeStrategySynchronisation:
    """
    For each regime the legacy (hard-coded) and the RoutingConfig-based selectors
    must activate exactly the same set of strategy types.
    """

    @pytest.mark.parametrize(
        "regime,recommended,confluence",
        _REGIME_SCENARIOS,
        ids=[f"{r.value}/{rec.value}/conf={c}" for r, rec, c in _REGIME_SCENARIOS],
    )
    def test_strategy_types_match(
        self,
        regime: MarketRegime,
        recommended: RecommendedStrategy,
        confluence: float,
    ) -> None:
        """Legacy and RoutingConfig selectors must return the same strategy types."""
        analysis = _make_analysis(
            regime=regime,
            recommended=recommended,
            confluence_score=confluence,
        )

        legacy = _make_legacy_selector()
        routing = _make_routing_selector()

        legacy_types = _strategy_types(legacy, analysis)
        routing_types = _strategy_types(routing, analysis)

        assert legacy_types == routing_types, (
            f"Regime={regime.value}, recommended={recommended.value}, "
            f"confluence={confluence}: "
            f"legacy={sorted(legacy_types)} vs routing={sorted(routing_types)}"
        )

    @pytest.mark.parametrize(
        "regime,recommended,confluence",
        _REGIME_SCENARIOS,
        ids=[f"{r.value}/{rec.value}/conf={c}" for r, rec, c in _REGIME_SCENARIOS],
    )
    def test_weights_and_priorities_match(
        self,
        regime: MarketRegime,
        recommended: RecommendedStrategy,
        confluence: float,
    ) -> None:
        """Strategy weights and priorities must be identical between legacy and RoutingConfig."""
        analysis = _make_analysis(
            regime=regime,
            recommended=recommended,
            confluence_score=confluence,
        )

        legacy = _make_legacy_selector()
        routing = _make_routing_selector()

        legacy_result = legacy.select(analysis)
        routing_result = routing.select(analysis)

        # Compare weight/priority per strategy type
        legacy_map = {
            w.strategy_type: (w.weight, w.priority) for w in legacy_result.strategies_to_start
        }
        routing_map = {
            w.strategy_type: (w.weight, w.priority) for w in routing_result.strategies_to_start
        }

        assert legacy_map == routing_map, (
            f"Regime={regime.value}, recommended={recommended.value}, "
            f"confluence={confluence}: "
            f"legacy={legacy_map} vs routing={routing_map}"
        )


# ---------------------------------------------------------------------------
# Default regime strategies dict vs YAML
# ---------------------------------------------------------------------------


class TestDefaultRegimeStrategiesVsYaml:
    """Verify that each regime in DEFAULT_REGIME_STRATEGIES is covered by the YAML."""

    def test_all_legacy_regimes_have_yaml_coverage(self) -> None:
        """Every regime in DEFAULT_REGIME_STRATEGIES must match when queried via YAML."""
        routing = RoutingConfig(_PROD_ROUTING_CONFIG)

        for regime, legacy_weights in DEFAULT_REGIME_STRATEGIES.items():
            if not legacy_weights:
                # UNKNOWN regime: empty in legacy; YAML fallback returns something → skip
                continue

            conditions = {"market_regime": regime.value}
            yaml_configs = routing.get_strategies(conditions)
            yaml_types = {sc.name for sc in yaml_configs}
            legacy_types = {w.strategy_type for w in legacy_weights}

            assert yaml_types == legacy_types, (
                f"Regime {regime.value}: "
                f"legacy={sorted(legacy_types)} vs YAML={sorted(yaml_types)}"
            )

    def test_hybrid_weights_match_yaml_bull_trend_high_confluence(self) -> None:
        """HYBRID_STRATEGY_WEIGHTS must match the YAML bull_trend + confluence_high rule."""
        routing = RoutingConfig(_PROD_ROUTING_CONFIG)
        yaml_configs = routing.get_strategies(
            {"market_regime": "bull_trend", "confluence_high": True}
        )

        yaml_types = {sc.name for sc in yaml_configs}
        hybrid_types = {w.strategy_type for w in HYBRID_STRATEGY_WEIGHTS}

        assert (
            yaml_types == hybrid_types
        ), f"Hybrid: legacy={sorted(hybrid_types)} vs YAML={sorted(yaml_types)}"


# ---------------------------------------------------------------------------
# RoutingConfig initialisation in StrategySelector
# ---------------------------------------------------------------------------


class TestRoutingConfigInit:
    """Verify that StrategySelector correctly stores and uses routing_config."""

    def test_selector_without_routing_config_uses_legacy(self) -> None:
        """Without routing_config, selector uses DEFAULT_REGIME_STRATEGIES."""
        selector = _make_legacy_selector()
        assert selector._routing_config is None

    def test_selector_with_routing_config_stores_it(self) -> None:
        """With routing_config, selector stores the RoutingConfig instance."""
        cfg = RoutingConfig(_PROD_ROUTING_CONFIG)
        selector = _make_routing_selector()
        assert selector._routing_config is not None
        assert isinstance(selector._routing_config, RoutingConfig)

    def test_routing_config_is_used_for_selection(self) -> None:
        """When routing_config is set, _get_target_strategies delegates to it."""
        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.DCA,
        )
        selector = _make_routing_selector()
        result = selector.select(analysis)

        start_types = {w.strategy_type for w in result.strategies_to_start}
        # BULL_TREND (low confluence) → trend_follower + dca
        assert "trend_follower" in start_types
        assert "dca" in start_types

    def test_routing_config_hybrid_via_confluence_high(self) -> None:
        """High confluence activates the hybrid (bull_trend_hybrid) rule in YAML."""
        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            recommended=RecommendedStrategy.HYBRID,
            confluence_score=0.85,
        )
        selector = _make_routing_selector()
        result = selector.select(analysis)

        start_types = {w.strategy_type for w in result.strategies_to_start}
        # Hybrid rule: dca + grid + trend_follower
        assert "dca" in start_types
        assert "grid" in start_types
        assert "trend_follower" in start_types

    def test_reduce_exposure_always_returns_empty(self) -> None:
        """REDUCE_EXPOSURE overrides RoutingConfig and returns no strategies."""
        analysis = _make_analysis(
            regime=MarketRegime.VOLATILE_TRANSITION,
            recommended=RecommendedStrategy.REDUCE_EXPOSURE,
        )
        selector = _make_routing_selector()
        result = selector.select(analysis)

        assert result.strategies_to_start == []

    def test_hold_returns_current_weights(self) -> None:
        """HOLD recommendation keeps current strategies (no changes)."""
        selector = _make_routing_selector()
        # First establish a regime so _current_regime is set
        first_analysis = _make_analysis(
            regime=MarketRegime.TIGHT_RANGE,
            recommended=RecommendedStrategy.GRID,
        )
        selector._current_regime = MarketRegime.TIGHT_RANGE

        analysis = _make_analysis(
            regime=MarketRegime.QUIET_TRANSITION,
            recommended=RecommendedStrategy.HOLD,
        )
        result = selector.select(analysis)
        # HOLD → no transition needed
        assert not result.transition_needed
