"""
Cross-platform integration tests: live StrategySelector vs backtest StrategyRouter (issue #371).

Guarantees that for any combination of market_regime and confluence_score, the
``StrategySelector`` (live bot) and the ``StrategyRouter`` (backtesting engine)
activate exactly the same set of strategy types and return the same parameters.

Both components now share a single source of truth:
- the YAML-based ``RoutingConfig`` (``configs/strategy_routing.yaml``)
- the same ``_build_routing_conditions`` logic

Test structure:
1. ``TestRoutingSync`` — parametrized over 1000+ random scenarios, verifies that
   both components return identical strategy names for every combination.
2. ``TestRoutingParamsSync`` — verifies that strategy weights and priorities also
   match between the live selector and the backtest router.
3. ``TestRoutingEdgeCases`` — explicit edge-case checks (REDUCE_EXPOSURE, HOLD,
   unknown regime, confluence boundary, all six primary regimes).
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timezone
from typing import Any

import pytest

from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.routing_config import RoutingConfig, StrategyConfig
from bot.orchestrator.strategy_registry import StrategyRegistry
from bot.orchestrator.strategy_selector import StrategySelector
from bot.tests.backtesting.strategy_router import StrategyRouter

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_PROD_ROUTING_CONFIG = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "configs",
    "strategy_routing.yaml",
)


def _make_routing_config() -> RoutingConfig:
    return RoutingConfig(_PROD_ROUTING_CONFIG)


def _make_regime(
    regime: MarketRegime,
    recommended: RecommendedStrategy,
    confluence_score: float = 0.5,
    confidence: float = 0.8,
    regime_duration: int = 300,
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
        bb_width_pct=3.0,
        volume_ratio=1.0,
        regime_duration_seconds=regime_duration,
        previous_regime=None,
        timestamp=datetime.now(timezone.utc),
        analysis_details={},
    )


def _make_selector() -> StrategySelector:
    """Create a live StrategySelector backed by the production RoutingConfig YAML."""
    return StrategySelector(
        registry=StrategyRegistry(max_strategies=10),
        transition_cooldown_seconds=0.0,
        min_regime_duration_seconds=0.0,
        require_smc_confirmation=False,
        routing_config=_make_routing_config(),
    )


def _make_router() -> StrategyRouter:
    """Create a backtest StrategyRouter backed by the same production RoutingConfig YAML."""
    return StrategyRouter(
        routing_config=_make_routing_config(),
        cooldown_bars=0,
    )


def _selector_strategy_names(selector: StrategySelector, analysis: RegimeAnalysis) -> set[str]:
    """Return the strategy names that StrategySelector would start for this analysis."""
    result = selector.select(analysis)
    return {w.strategy_type for w in result.strategies_to_start}


def _selector_strategy_configs(
    selector: StrategySelector, analysis: RegimeAnalysis
) -> dict[str, tuple[float, int]]:
    """Return {strategy_type: (weight, priority)} from StrategySelector."""
    result = selector.select(analysis)
    return {w.strategy_type: (w.weight, w.priority) for w in result.strategies_to_start}


def _router_strategy_names(router: StrategyRouter, analysis: RegimeAnalysis) -> set[str]:
    """Return the strategy names that StrategyRouter would activate for this analysis."""
    event = router.on_bar(analysis, current_bar=0)
    return event.active_strategies


def _router_strategy_configs(
    router: StrategyRouter, analysis: RegimeAnalysis
) -> dict[str, tuple[float, int]]:
    """Return {strategy_name: (weight, priority)} from StrategyRouter via RoutingConfig."""
    conditions = StrategyRouter._build_routing_conditions(analysis)
    cfg = _make_routing_config()
    strategy_configs: list[StrategyConfig] = cfg.get_strategies(conditions)
    return {
        sc.name: (sc.params.get("weight", 1.0), sc.params.get("priority", 1))
        for sc in strategy_configs
    }


# ---------------------------------------------------------------------------
# Scenario generation
# ---------------------------------------------------------------------------

# The primary regimes and their canonical RecommendedStrategy values as used by
# MarketRegimeDetector._recommend_strategy().
_REGIME_RECOMMENDATIONS: list[tuple[MarketRegime, RecommendedStrategy]] = [
    (MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID),
    (MarketRegime.WIDE_RANGE, RecommendedStrategy.GRID),
    (MarketRegime.QUIET_TRANSITION, RecommendedStrategy.GRID),
    (MarketRegime.VOLATILE_TRANSITION, RecommendedStrategy.SMC),
    (MarketRegime.BULL_TREND, RecommendedStrategy.DCA),  # low confluence
    (MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID),  # high confluence
    (MarketRegime.BEAR_TREND, RecommendedStrategy.DCA),
    (MarketRegime.ACCUMULATION, RecommendedStrategy.SMC),
    (MarketRegime.DISTRIBUTION, RecommendedStrategy.SMC),
]


def _generate_random_scenarios(n: int = 1000, seed: int = 42) -> list[RegimeAnalysis]:
    """
    Generate *n* random RegimeAnalysis scenarios by picking random regimes and
    confluence scores.  The confluence score determines whether BULL_TREND uses
    the HYBRID or DCA recommendation, matching MarketRegimeDetector logic.
    """
    rng = random.Random(seed)
    scenarios: list[RegimeAnalysis] = []

    # UNKNOWN regime is excluded: router uses a bootstrap set {grid, dca} while
    # selector starts with an empty registry.  This is a known intentional difference
    # (documented in architecture_v2.md section 15).  The synchronisation guarantee
    # covers the 8 primary market regimes only.
    all_regimes = [
        MarketRegime.TIGHT_RANGE,
        MarketRegime.WIDE_RANGE,
        MarketRegime.QUIET_TRANSITION,
        MarketRegime.VOLATILE_TRANSITION,
        MarketRegime.BULL_TREND,
        MarketRegime.BEAR_TREND,
        MarketRegime.ACCUMULATION,
        MarketRegime.DISTRIBUTION,
    ]

    for _ in range(n):
        regime = rng.choice(all_regimes)
        confluence = rng.uniform(0.0, 1.0)

        # Mirror MarketRegimeDetector._recommend_strategy() so recommended is realistic
        if regime == MarketRegime.BULL_TREND:
            recommended = (
                RecommendedStrategy.HYBRID if confluence >= 0.7 else RecommendedStrategy.DCA
            )
        elif regime in (MarketRegime.TIGHT_RANGE, MarketRegime.WIDE_RANGE):
            recommended = RecommendedStrategy.GRID
        elif regime == MarketRegime.QUIET_TRANSITION:
            recommended = RecommendedStrategy.GRID
        elif regime == MarketRegime.VOLATILE_TRANSITION:
            recommended = RecommendedStrategy.REDUCE_EXPOSURE
        elif regime == MarketRegime.BEAR_TREND:
            recommended = RecommendedStrategy.DCA
        elif regime in (MarketRegime.ACCUMULATION, MarketRegime.DISTRIBUTION):
            recommended = RecommendedStrategy.SMC
        else:
            recommended = RecommendedStrategy.HOLD

        scenarios.append(
            _make_regime(regime=regime, recommended=recommended, confluence_score=confluence)
        )

    return scenarios


# ---------------------------------------------------------------------------
# 1. Strategy name synchronisation over 1000+ random scenarios
# ---------------------------------------------------------------------------


class TestRoutingSync:
    """
    StrategySelector (live) and StrategyRouter (backtest) must return identical
    strategy name sets for every market scenario.
    """

    def test_strategy_names_sync_over_1000_scenarios(self) -> None:
        """1000 random scenarios: live selector == backtest router (strategy names)."""
        scenarios = _generate_random_scenarios(n=1000, seed=42)
        mismatches: list[dict[str, Any]] = []

        for i, analysis in enumerate(scenarios):
            selector = _make_selector()
            router = _make_router()

            live_names = _selector_strategy_names(selector, analysis)
            bt_names = _router_strategy_names(router, analysis)

            if live_names != bt_names:
                mismatches.append(
                    {
                        "scenario": i,
                        "regime": analysis.regime.value,
                        "recommended": analysis.recommended_strategy.value,
                        "confluence": analysis.confluence_score,
                        "live": sorted(live_names),
                        "backtest": sorted(bt_names),
                    }
                )

        assert not mismatches, (
            f"{len(mismatches)} / 1000 scenarios had live-backtest routing divergence:\n"
            + "\n".join(str(m) for m in mismatches[:10])
        )

    def test_strategy_names_sync_over_all_primary_regimes(self) -> None:
        """Every primary (regime, recommendation) pair must produce identical strategy sets."""
        for regime, recommended in _REGIME_RECOMMENDATIONS:
            # Use two confluence values that straddle the 0.7 boundary
            for confluence in (0.3, 0.5, 0.75, 0.9):
                analysis = _make_regime(
                    regime=regime,
                    recommended=recommended,
                    confluence_score=confluence,
                )

                selector = _make_selector()
                router = _make_router()

                live_names = _selector_strategy_names(selector, analysis)
                bt_names = _router_strategy_names(router, analysis)

                assert live_names == bt_names, (
                    f"Routing divergence: regime={regime.value}, "
                    f"recommended={recommended.value}, confluence={confluence}: "
                    f"live={sorted(live_names)} vs backtest={sorted(bt_names)}"
                )


# ---------------------------------------------------------------------------
# 2. Strategy weights and priorities synchronisation
# ---------------------------------------------------------------------------


class TestRoutingParamsSync:
    """
    For regimes where routing_config is used (not HOLD / REDUCE_EXPOSURE),
    weights and priorities must also be identical.
    """

    @pytest.mark.parametrize(
        "regime,recommended,confluence",
        [
            (MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID, 0.5),
            (MarketRegime.WIDE_RANGE, RecommendedStrategy.GRID, 0.5),
            (MarketRegime.QUIET_TRANSITION, RecommendedStrategy.GRID, 0.5),
            (MarketRegime.BULL_TREND, RecommendedStrategy.DCA, 0.5),
            (MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID, 0.8),
            (MarketRegime.BEAR_TREND, RecommendedStrategy.DCA, 0.5),
            (MarketRegime.VOLATILE_TRANSITION, RecommendedStrategy.SMC, 0.5),
            (MarketRegime.ACCUMULATION, RecommendedStrategy.SMC, 0.5),
            (MarketRegime.DISTRIBUTION, RecommendedStrategy.SMC, 0.5),
        ],
        ids=[
            "tight_range",
            "wide_range",
            "quiet_transition",
            "bull_trend_low_conf",
            "bull_trend_high_conf",
            "bear_trend",
            "volatile_transition",
            "accumulation",
            "distribution",
        ],
    )
    def test_weights_and_priorities_match(
        self,
        regime: MarketRegime,
        recommended: RecommendedStrategy,
        confluence: float,
    ) -> None:
        """Both components must use identical weights and priorities from the YAML."""
        analysis = _make_regime(regime=regime, recommended=recommended, confluence_score=confluence)

        selector = _make_selector()
        router = _make_router()

        live_configs = _selector_strategy_configs(selector, analysis)
        bt_configs = _router_strategy_configs(router, analysis)

        assert live_configs == bt_configs, (
            f"Weight/priority divergence: regime={regime.value}, "
            f"recommended={recommended.value}, confluence={confluence}: "
            f"live={live_configs} vs backtest={bt_configs}"
        )


# ---------------------------------------------------------------------------
# 3. Edge-case scenarios
# ---------------------------------------------------------------------------


class TestRoutingEdgeCases:
    def test_reduce_exposure_both_return_empty(self) -> None:
        """REDUCE_EXPOSURE → empty set in both live selector and backtest router."""
        analysis = _make_regime(
            MarketRegime.VOLATILE_TRANSITION,
            RecommendedStrategy.REDUCE_EXPOSURE,
        )

        selector = _make_selector()
        router = _make_router()

        live_names = _selector_strategy_names(selector, analysis)
        # router.on_bar from empty initial + REDUCE_EXPOSURE → empty
        router.on_bar(
            _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID), current_bar=0
        )
        event = router.on_bar(analysis, current_bar=1)
        bt_names = event.active_strategies

        assert (
            live_names == set()
        ), f"Selector should return empty on REDUCE_EXPOSURE, got {live_names}"
        assert bt_names == set(), f"Router should return empty on REDUCE_EXPOSURE, got {bt_names}"

    def test_hold_both_keep_current(self) -> None:
        """HOLD → neither live nor backtest changes its active strategy set."""
        # Establish grid as active in both
        first_analysis = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        hold_analysis = _make_regime(MarketRegime.QUIET_TRANSITION, RecommendedStrategy.HOLD)

        selector = _make_selector()
        selector.select(first_analysis)  # prime the selector's regime

        router = _make_router()
        router.on_bar(first_analysis, current_bar=0)

        before_router = router._active_strategies.copy()

        hold_event = router.on_bar(hold_analysis, current_bar=1)
        assert (
            hold_event.active_strategies == before_router
        ), "Router HOLD should keep current strategies unchanged"

        hold_result = selector.select(hold_analysis)
        assert not hold_result.transition_needed, "Selector HOLD should not trigger a transition"

    def test_confluence_boundary_at_07(self) -> None:
        """
        Verify the 0.7 confluence boundary is respected identically in both components.

        confluence=0.699... → low-confluence bull_trend rule (trend_follower + dca)
        confluence=0.700    → high-confluence hybrid rule  (dca + grid + trend_follower)
        """
        low_conf = _make_regime(
            MarketRegime.BULL_TREND, RecommendedStrategy.DCA, confluence_score=0.699
        )
        high_conf = _make_regime(
            MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID, confluence_score=0.700
        )

        for analysis in (low_conf, high_conf):
            selector = _make_selector()
            router = _make_router()
            live_names = _selector_strategy_names(selector, analysis)
            bt_names = _router_strategy_names(router, analysis)
            assert live_names == bt_names, (
                f"Confluence boundary divergence at {analysis.confluence_score}: "
                f"live={sorted(live_names)} vs backtest={sorted(bt_names)}"
            )

        # Low confluence must NOT include grid (hybrid-only)
        low_router = _make_router()
        low_event = low_router.on_bar(low_conf, current_bar=0)
        assert (
            "grid" not in low_event.active_strategies
        ), "Low-confluence BULL_TREND should not activate grid"

        # High confluence must include grid
        high_router = _make_router()
        high_event = high_router.on_bar(high_conf, current_bar=0)
        assert (
            "grid" in high_event.active_strategies
        ), "High-confluence BULL_TREND (hybrid) should include grid"

    def test_unknown_regime_routing_conditions_match(self) -> None:
        """
        UNKNOWN regime: both components build the same routing conditions dict.

        Note: the *strategy sets* intentionally differ for UNKNOWN+HOLD because the
        router has a bootstrap set {grid, dca} while the selector starts with an empty
        registry.  This is a documented design difference (see architecture_v2.md §15).
        What must match is the conditions dict used to query RoutingConfig.
        """
        from bot.orchestrator.strategy_selector import StrategySelector as SS

        analysis = _make_regime(MarketRegime.UNKNOWN, RecommendedStrategy.HOLD)

        router_conds = StrategyRouter._build_routing_conditions(analysis)
        selector_conds = SS._build_routing_conditions(
            analysis.regime, analysis.recommended_strategy, analysis
        )

        assert router_conds == selector_conds, (
            f"UNKNOWN regime condition dicts diverge: "
            f"router={router_conds} vs selector={selector_conds}"
        )

    def test_no_regime_router_returns_bootstrap(self) -> None:
        """When no regime is available yet, the router returns its bootstrap set."""
        router = _make_router()
        event = router.on_bar(None, current_bar=0)
        # Bootstrap default: grid + dca
        assert "grid" in event.active_strategies
        assert "dca" in event.active_strategies
        assert event.cooldown_remaining == 0

    def test_build_routing_conditions_matches_selector(self) -> None:
        """
        _build_routing_conditions output must match StrategySelector._build_routing_conditions
        for all regime combinations.
        """
        from bot.orchestrator.strategy_selector import StrategySelector as SS

        for regime, recommended in _REGIME_RECOMMENDATIONS:
            for confluence in (0.3, 0.7, 0.9):
                analysis = _make_regime(regime, recommended, confluence_score=confluence)

                router_conditions = StrategyRouter._build_routing_conditions(analysis)
                selector_conditions = SS._build_routing_conditions(regime, recommended, analysis)

                assert router_conditions == selector_conditions, (
                    f"Condition divergence for regime={regime.value}, "
                    f"recommended={recommended.value}, confluence={confluence}: "
                    f"router={router_conditions} vs selector={selector_conditions}"
                )
