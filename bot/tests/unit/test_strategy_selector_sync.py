"""
Tests verifying that StrategySelector with RoutingConfig produces the same
strategy selection results as the old hard-coded DEFAULT_REGIME_STRATEGIES.

Purpose (issue #368 / #370):
- Ensure refactoring StrategySelector to use RoutingConfig does NOT change
  the live bot's behaviour.
- Compare old (explicit dict) vs new (RoutingConfig-backed) for every
  MarketRegime value and a set of RecommendedStrategy combinations.
"""

from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_analysis(
    regime: MarketRegime,
    recommended: RecommendedStrategy,
    confidence: float = 0.8,
    regime_duration: int = 300,
    confluence_score: float = 0.5,
) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=regime,
        confidence=confidence,
        recommended_strategy=recommended,
        confluence_score=confluence_score,
        trend_strength=0.0,
        volatility_percentile=50.0,
        ema_divergence_pct=0.0,
        atr_pct=2.0,
        rsi=50.0,
        adx=20.0,
        bb_width_pct=3.0,
        volume_ratio=1.0,
        regime_duration_seconds=regime_duration,
        previous_regime=None,
        timestamp=datetime.now(timezone.utc),
        analysis_details={"current_price": 3000.0},
    )


def _make_registry() -> StrategyRegistry:
    registry = StrategyRegistry(max_strategies=10)
    registry.register("grid-1", "grid", {"pair": "BTCUSDT"})
    registry.register("dca-1", "dca", {"pair": "BTCUSDT"})
    registry.register("trend-1", "trend_follower", {"pair": "BTCUSDT"})
    registry.register("smc-1", "smc", {"pair": "BTCUSDT"})
    return registry


def _old_selector() -> StrategySelector:
    """StrategySelector using the old hard-coded DEFAULT_REGIME_STRATEGIES."""
    return StrategySelector(
        registry=_make_registry(),
        regime_strategies=DEFAULT_REGIME_STRATEGIES,
        hybrid_weights=HYBRID_STRATEGY_WEIGHTS,
        require_smc_confirmation=False,
        transition_cooldown_seconds=0.0,
        min_regime_duration_seconds=0.0,
    )


def _new_selector() -> StrategySelector:
    """StrategySelector backed by RoutingConfig (default YAML)."""
    return StrategySelector(
        registry=_make_registry(),
        require_smc_confirmation=False,
        transition_cooldown_seconds=0.0,
        min_regime_duration_seconds=0.0,
        routing_config=RoutingConfig(),
    )


def _target_types(selector: StrategySelector, analysis: RegimeAnalysis) -> frozenset[str]:
    """Run select() and return the set of strategy types that *would* start + keep."""
    result = selector.select(analysis)
    start_types = {w.strategy_type for w in result.strategies_to_start}
    keep_types = set(result.strategies_to_keep)
    return frozenset(start_types | keep_types)


# ---------------------------------------------------------------------------
# Parametric regime-by-regime comparison
# ---------------------------------------------------------------------------

# Map each regime to a "normal" recommended strategy (no HYBRID / special path)
_REGIME_RECOMMENDED = [
    (MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
    (MarketRegime.WIDE_RANGE, RecommendedStrategy.GRID),
    (MarketRegime.QUIET_TRANSITION, RecommendedStrategy.GRID),
    (MarketRegime.VOLATILE_TRANSITION, RecommendedStrategy.REDUCE_EXPOSURE),
    (MarketRegime.BULL_TREND, RecommendedStrategy.DCA),
    (MarketRegime.BEAR_TREND, RecommendedStrategy.DCA),
    (MarketRegime.UNKNOWN, RecommendedStrategy.HOLD),
]


class TestStrategySelectorSync:
    """Verify that old (hardcoded) and new (YAML-backed) selectors agree."""

    @pytest.mark.parametrize("regime,recommended", _REGIME_RECOMMENDED)
    def test_first_selection_same_types(
        self, regime: MarketRegime, recommended: RecommendedStrategy
    ) -> None:
        """On first call (no prior regime) both selectors must pick the same types."""
        analysis = _make_analysis(regime, recommended)
        old_result = _old_selector().select(analysis)
        new_result = _new_selector().select(analysis)

        old_types = {w.strategy_type for w in old_result.strategies_to_start}
        new_types = {w.strategy_type for w in new_result.strategies_to_start}
        assert (
            old_types == new_types
        ), f"Regime {regime.value}: old={sorted(old_types)}, new={sorted(new_types)}"

    def test_bull_trend_has_tf_and_dca(self) -> None:
        """BULL_TREND (non-hybrid) must start trend_follower + dca in new selector."""
        analysis = _make_analysis(MarketRegime.BULL_TREND, RecommendedStrategy.DCA)
        result = _new_selector().select(analysis)
        types = {w.strategy_type for w in result.strategies_to_start}
        assert "trend_follower" in types
        assert "dca" in types

    def test_bear_trend_has_dca_only(self) -> None:
        analysis = _make_analysis(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        result = _new_selector().select(analysis)
        types = {w.strategy_type for w in result.strategies_to_start}
        assert types == {"dca"}

    def test_tight_range_has_grid_only(self) -> None:
        analysis = _make_analysis(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        result = _new_selector().select(analysis)
        types = {w.strategy_type for w in result.strategies_to_start}
        assert types == {"grid"}

    def test_wide_range_has_grid_only(self) -> None:
        analysis = _make_analysis(MarketRegime.WIDE_RANGE, RecommendedStrategy.GRID)
        result = _new_selector().select(analysis)
        types = {w.strategy_type for w in result.strategies_to_start}
        assert types == {"grid"}

    def test_volatile_transition_no_strategies_started(self) -> None:
        """VOLATILE_TRANSITION with REDUCE_EXPOSURE → no strategies."""
        analysis = _make_analysis(
            MarketRegime.VOLATILE_TRANSITION, RecommendedStrategy.REDUCE_EXPOSURE
        )
        result = _new_selector().select(analysis)
        assert result.strategies_to_start == []

    def test_hybrid_recommendation_uses_hybrid_weights(self) -> None:
        """HYBRID recommendation → hybrid_weights from YAML (dca + grid + tf)."""
        analysis = _make_analysis(
            MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID, confluence_score=0.9
        )
        old_result = _old_selector().select(analysis)
        new_result = _new_selector().select(analysis)

        old_types = {w.strategy_type for w in old_result.strategies_to_start}
        new_types = {w.strategy_type for w in new_result.strategies_to_start}
        assert old_types == new_types

    def test_new_selector_uses_routing_config(self) -> None:
        """StrategySelector created without regime_strategies uses RoutingConfig."""
        selector = _new_selector()
        assert selector._routing_config is not None
        assert selector._regime_strategies is None

    def test_old_selector_uses_explicit_regime_strategies(self) -> None:
        """StrategySelector with explicit regime_strategies ignores RoutingConfig."""
        selector = _old_selector()
        assert selector._regime_strategies is not None


# ---------------------------------------------------------------------------
# SMC regimes — only present in new YAML config, not in DEFAULT_REGIME_STRATEGIES
# ---------------------------------------------------------------------------


class TestSMCRegimeSync:
    """ACCUMULATION and DISTRIBUTION regimes map to smc in the new config."""

    def test_accumulation_maps_to_smc(self) -> None:
        analysis = _make_analysis(MarketRegime.ACCUMULATION, RecommendedStrategy.SMC)
        result = _new_selector().select(analysis)
        types = {w.strategy_type for w in result.strategies_to_start}
        assert "smc" in types

    def test_distribution_maps_to_smc(self) -> None:
        analysis = _make_analysis(MarketRegime.DISTRIBUTION, RecommendedStrategy.SMC)
        result = _new_selector().select(analysis)
        types = {w.strategy_type for w in result.strategies_to_start}
        assert "smc" in types

    def test_accumulation_not_in_old_default(self) -> None:
        """ACCUMULATION was missing from the old hardcoded mapping — should return []."""
        assert MarketRegime.ACCUMULATION not in DEFAULT_REGIME_STRATEGIES

    def test_distribution_not_in_old_default(self) -> None:
        """DISTRIBUTION was missing from the old hardcoded mapping — should return []."""
        assert MarketRegime.DISTRIBUTION not in DEFAULT_REGIME_STRATEGIES


# ---------------------------------------------------------------------------
# Weights and priorities preserved
# ---------------------------------------------------------------------------


class TestWeightsPreserved:
    """Weights and priorities from the YAML must match the hardcoded values."""

    def test_bull_trend_tf_weight(self) -> None:
        cfg = RoutingConfig()
        configs = cfg.get_strategies({"market_regime": "bull_trend"})
        tf = next(c for c in configs if c.type == "trend_follower")
        assert tf.weight == pytest.approx(0.7)

    def test_bull_trend_dca_weight(self) -> None:
        cfg = RoutingConfig()
        configs = cfg.get_strategies({"market_regime": "bull_trend"})
        dca = next(c for c in configs if c.type == "dca")
        assert dca.weight == pytest.approx(0.3)

    def test_bear_trend_dca_weight(self) -> None:
        cfg = RoutingConfig()
        configs = cfg.get_strategies({"market_regime": "bear_trend"})
        dca = next(c for c in configs if c.type == "dca")
        assert dca.weight == pytest.approx(1.0)

    def test_hybrid_weights_match_hardcoded(self) -> None:
        cfg = RoutingConfig()
        yaml_weights = {sc.type: sc.weight for sc in cfg.get_hybrid_weights()}
        hardcoded = {w.strategy_type: w.weight for w in HYBRID_STRATEGY_WEIGHTS}
        assert yaml_weights == pytest.approx(hardcoded)
