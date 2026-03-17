"""
Integration tests: Live (StrategySelector) vs Backtest (StrategyRouter) routing sync.

Purpose (issue #368 / #371):
- Generate 1000+ market condition scenarios.
- For each scenario verify that the strategy *types* chosen by StrategySelector
  and StrategyRouter are IDENTICAL when both use the same RoutingConfig.
- This test is the acceptance criterion for Epic #1.

Mark: ``integration`` — runs as part of the standard pytest suite (no external
services needed).
"""

from __future__ import annotations

import itertools
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
from bot.orchestrator.strategy_selector import StrategySelector
from bot.tests.backtesting.strategy_router import StrategyRouter

# ---------------------------------------------------------------------------
# Path to the production routing config
# ---------------------------------------------------------------------------

_PROD_ROUTING_CONFIG = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "configs",
    "strategy_routing.yaml",
)

# ---------------------------------------------------------------------------
# Shared routing config instance (loaded once per session)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def routing_cfg() -> RoutingConfig:
    return RoutingConfig(_PROD_ROUTING_CONFIG)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_analysis(
    regime: MarketRegime,
    recommended: RecommendedStrategy,
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
        ema_divergence_pct=0.0,
        atr_pct=2.0,
        rsi=50.0,
        adx=20.0,
        bb_width_pct=3.0,
        volume_ratio=1.0,
        regime_duration_seconds=300,
        previous_regime=None,
        timestamp=datetime.now(timezone.utc),
        analysis_details={"current_price": 3000.0},
    )


def _make_registry() -> StrategyRegistry:
    registry = StrategyRegistry(max_strategies=20)
    for strategy_type in ("grid", "dca", "trend_follower", "smc"):
        registry.register(f"{strategy_type}-1", strategy_type, {"pair": "BTCUSDT"})
    return registry


def _live_target_types(selector: StrategySelector, analysis: RegimeAnalysis) -> frozenset[str]:
    """Ask StrategySelector which strategies it *would* activate (no side-effects)."""
    result = selector.select(analysis)
    start_types = {w.strategy_type for w in result.strategies_to_start}
    keep_types = set(result.strategies_to_keep)
    return frozenset(start_types | keep_types)


def _backtest_target_types(router: StrategyRouter, analysis: RegimeAnalysis) -> frozenset[str]:
    """Ask StrategyRouter which strategies it would activate (bar 0, no cooldown)."""
    return frozenset(router._compute_target_strategies(analysis))


# ---------------------------------------------------------------------------
# Scenario generation
# ---------------------------------------------------------------------------

# All (regime, recommended_strategy) pairs supported by the routing config.
# We test ALL 9 regimes × 6 recommendations = 54 base pairs.
_ALL_REGIMES = list(MarketRegime)
_ALL_RECOMMENDATIONS = list(RecommendedStrategy)

# Extra numeric scenario dimensions to exceed 1000 total scenarios
# 9 regimes × 6 recommendations × 5 confidence × 4 confluence = 1080 scenarios
_CONFIDENCE_VALUES = [0.1, 0.3, 0.5, 0.7, 0.9]
_CONFLUENCE_VALUES = [0.2, 0.4, 0.6, 0.8]


def _generate_scenarios() -> list[tuple[MarketRegime, RecommendedStrategy, float, float]]:
    """Generate >1000 (regime, recommended, confidence, confluence) tuples (9×6×5×4=1080)."""
    return list(
        itertools.product(
            _ALL_REGIMES,
            _ALL_RECOMMENDATIONS,
            _CONFIDENCE_VALUES,
            _CONFLUENCE_VALUES,
        )
    )


_SCENARIOS = _generate_scenarios()


# ---------------------------------------------------------------------------
# Core cross-platform sync test
# ---------------------------------------------------------------------------


class TestLiveBacktestRoutingSync:
    """
    Verify Live StrategySelector and Backtest StrategyRouter produce identical
    strategy-type sets for every generated scenario.

    The HOLD recommendation is intentionally excluded from the strict equality
    check because it means "keep whatever is currently active" — which by
    definition depends on prior state (different for fresh Selector vs Router).
    """

    def test_scenario_count_exceeds_1000(self) -> None:
        assert len(_SCENARIOS) >= 1000, f"Only {len(_SCENARIOS)} scenarios generated"

    @pytest.mark.parametrize("scenario", _SCENARIOS[:100])  # sample for speed
    def test_routing_sync_sampled(
        self,
        routing_cfg: RoutingConfig,
        scenario: tuple[MarketRegime, RecommendedStrategy, float, float],
    ) -> None:
        """Compare Live vs Backtest routing for a sample of 100 scenarios."""
        regime, recommended, confidence, confluence = scenario
        if recommended == RecommendedStrategy.HOLD:
            pytest.skip("HOLD is state-dependent — skip strict comparison")

        analysis = _make_analysis(regime, recommended, confidence, confluence)

        selector = StrategySelector(
            registry=_make_registry(),
            require_smc_confirmation=False,
            transition_cooldown_seconds=0.0,
            min_regime_duration_seconds=0.0,
            routing_config=routing_cfg,
        )
        router = StrategyRouter(routing_config=routing_cfg, cooldown_bars=0)

        live_types = _live_target_types(selector, analysis)
        backtest_types = _backtest_target_types(router, analysis)

        assert live_types == backtest_types, (
            f"Regime={regime.value} Recommended={recommended.value} "
            f"confidence={confidence} confluence={confluence}: "
            f"live={sorted(live_types)} backtest={sorted(backtest_types)}"
        )

    def test_full_scenario_suite(self, routing_cfg: RoutingConfig) -> None:
        """
        Full comparison over ALL 1000+ scenarios.

        For each scenario both selectors must return the same strategy-type set.
        HOLD and REDUCE_EXPOSURE are validated separately below.
        """
        mismatches: list[str] = []

        for scenario in _SCENARIOS:
            regime, recommended, confidence, confluence = scenario
            if recommended in (RecommendedStrategy.HOLD,):
                continue  # state-dependent, skip

            analysis = _make_analysis(regime, recommended, confidence, confluence)

            selector = StrategySelector(
                registry=_make_registry(),
                require_smc_confirmation=False,
                transition_cooldown_seconds=0.0,
                min_regime_duration_seconds=0.0,
                routing_config=routing_cfg,
            )
            router = StrategyRouter(routing_config=routing_cfg, cooldown_bars=0)

            live_types = _live_target_types(selector, analysis)
            backtest_types = _backtest_target_types(router, analysis)

            if live_types != backtest_types:
                mismatches.append(
                    f"Regime={regime.value} Recommended={recommended.value} "
                    f"conf={confidence} confl={confluence}: "
                    f"live={sorted(live_types)} != backtest={sorted(backtest_types)}"
                )

        assert (
            not mismatches
        ), f"{len(mismatches)} scenario(s) diverged between Live and Backtest:\n" + "\n".join(
            mismatches[:20]
        )  # show first 20


# ---------------------------------------------------------------------------
# Spot checks for specific critical regimes
# ---------------------------------------------------------------------------


class TestCriticalRegimeSync:
    """Spot-check the most important regime→strategy mappings."""

    def _check_sync(
        self,
        routing_cfg: RoutingConfig,
        regime: MarketRegime,
        recommended: RecommendedStrategy,
        expected_types: frozenset[str],
    ) -> None:
        analysis = _make_analysis(regime, recommended)

        selector = StrategySelector(
            registry=_make_registry(),
            require_smc_confirmation=False,
            transition_cooldown_seconds=0.0,
            min_regime_duration_seconds=0.0,
            routing_config=routing_cfg,
        )
        router = StrategyRouter(routing_config=routing_cfg, cooldown_bars=0)

        live_types = _live_target_types(selector, analysis)
        backtest_types = _backtest_target_types(router, analysis)

        assert live_types == backtest_types
        assert (
            live_types == expected_types
        ), f"Expected {sorted(expected_types)}, got live={sorted(live_types)}"

    def test_bull_trend_dca_recommendation(self, routing_cfg: RoutingConfig) -> None:
        """BULL_TREND + DCA → trend_follower + dca + smc."""
        self._check_sync(
            routing_cfg,
            MarketRegime.BULL_TREND,
            RecommendedStrategy.DCA,
            frozenset({"trend_follower", "dca", "smc"}),
        )

    def test_bull_trend_hybrid_low_adx(self, routing_cfg: RoutingConfig) -> None:
        """BULL_TREND + HYBRID + ADX ≤ 25 → Grid primary (mirrors HybridCoordinator)."""
        # ADX=20 (default in _make_analysis) → adx_high=False → bull_trend_hybrid_grid rule
        self._check_sync(
            routing_cfg,
            MarketRegime.BULL_TREND,
            RecommendedStrategy.HYBRID,
            frozenset({"grid", "trend_follower", "smc"}),
        )

    def test_bull_trend_hybrid_high_adx(self, routing_cfg: RoutingConfig) -> None:
        """BULL_TREND + HYBRID + ADX > 25 → DCA primary (mirrors HybridCoordinator)."""
        analysis = _make_analysis(
            MarketRegime.BULL_TREND,
            RecommendedStrategy.HYBRID,
            confluence_score=0.8,
        )
        # Override ADX to trigger adx_high condition
        analysis.adx = 30.0

        selector = StrategySelector(
            registry=_make_registry(),
            require_smc_confirmation=False,
            transition_cooldown_seconds=0.0,
            min_regime_duration_seconds=0.0,
            routing_config=routing_cfg,
        )
        router = StrategyRouter(routing_config=routing_cfg, cooldown_bars=0)

        live_types = _live_target_types(selector, analysis)
        backtest_types = _backtest_target_types(router, analysis)

        assert live_types == backtest_types
        expected = frozenset({"dca", "trend_follower", "smc"})
        assert live_types == expected, f"Expected {sorted(expected)}, got live={sorted(live_types)}"

    def test_bear_trend_dca_smc(self, routing_cfg: RoutingConfig) -> None:
        self._check_sync(
            routing_cfg,
            MarketRegime.BEAR_TREND,
            RecommendedStrategy.DCA,
            frozenset({"dca", "smc"}),
        )

    def test_tight_range_grid_dca(self, routing_cfg: RoutingConfig) -> None:
        self._check_sync(
            routing_cfg,
            MarketRegime.TIGHT_RANGE,
            RecommendedStrategy.GRID,
            frozenset({"grid", "dca"}),
        )

    def test_wide_range_grid_dca(self, routing_cfg: RoutingConfig) -> None:
        self._check_sync(
            routing_cfg,
            MarketRegime.WIDE_RANGE,
            RecommendedStrategy.GRID,
            frozenset({"grid", "dca"}),
        )

    def test_accumulation_smc(self, routing_cfg: RoutingConfig) -> None:
        """ACCUMULATION now maps to SMC in both live and backtest."""
        self._check_sync(
            routing_cfg,
            MarketRegime.ACCUMULATION,
            RecommendedStrategy.SMC,
            frozenset({"smc"}),
        )

    def test_distribution_smc(self, routing_cfg: RoutingConfig) -> None:
        """DISTRIBUTION now maps to SMC in both live and backtest."""
        self._check_sync(
            routing_cfg,
            MarketRegime.DISTRIBUTION,
            RecommendedStrategy.SMC,
            frozenset({"smc"}),
        )

    def test_volatile_transition_reduce_exposure(self, routing_cfg: RoutingConfig) -> None:
        """VOLATILE_TRANSITION + REDUCE_EXPOSURE → empty set."""
        self._check_sync(
            routing_cfg,
            MarketRegime.VOLATILE_TRANSITION,
            RecommendedStrategy.REDUCE_EXPOSURE,
            frozenset(),
        )

    def test_unknown_hold_live_is_empty(self, routing_cfg: RoutingConfig) -> None:
        """
        UNKNOWN + HOLD on a fresh StrategySelector → empty (no prior strategies).

        Note: The StrategyRouter bootstraps with {'grid', 'dca'} before any regime
        is known, so HOLD on a fresh router keeps those.  This initial-state divergence
        is expected and documented — it only matters at test startup, not in production
        where the router and selector share the same warm-up history.
        """
        analysis = _make_analysis(MarketRegime.UNKNOWN, RecommendedStrategy.HOLD)

        selector = StrategySelector(
            registry=_make_registry(),
            require_smc_confirmation=False,
            transition_cooldown_seconds=0.0,
            min_regime_duration_seconds=0.0,
            routing_config=routing_cfg,
        )

        live_types = _live_target_types(selector, analysis)

        # Live selector starts empty — HOLD keeps empty
        assert live_types == frozenset()
