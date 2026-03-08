"""Unit tests for StrategyConductor."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.strategy_conductor import (
    DEFAULT_REGIME_TO_MODE,
    DirectiveSnapshot,
    StrategyConductor,
    _compute_price_range,
    _extract_key_levels,
    _extract_liquidity_zones,
)
from bot.orchestrator.strategy_registry import StrategyRegistry
from bot.strategies.base import StrategyDirective, TradingMode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_analysis(
    regime: MarketRegime = MarketRegime.TIGHT_RANGE,
    recommended: RecommendedStrategy = RecommendedStrategy.GRID,
    confidence: float = 0.8,
    regime_duration: int = 300,
    current_price: float = 30000.0,
    smc_context=None,
) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=regime,
        confidence=confidence,
        recommended_strategy=recommended,
        confluence_score=0.5,
        trend_strength=0.0,
        volatility_percentile=50.0,
        ema_divergence_pct=0.0,
        atr_pct=1.0,
        rsi=50.0,
        adx=20.0,
        bb_width_pct=3.0,
        volume_ratio=1.0,
        regime_duration_seconds=regime_duration,
        previous_regime=None,
        timestamp=datetime.now(timezone.utc),
        analysis_details={"current_price": current_price},
        smc_context=smc_context,
    )


def _make_smc_context(
    structural_levels: list[float] | None = None,
    swing_highs: list[float] | None = None,
    swing_lows: list[float] | None = None,
):
    """Build a lightweight fake SMCContext."""
    ctx = MagicMock()
    ctx.structural_levels = structural_levels or []
    # Simulate swing points as objects with .price attribute
    ctx.swing_highs = [MagicMock(price=p) for p in (swing_highs or [])]
    ctx.swing_lows = [MagicMock(price=p) for p in (swing_lows or [])]
    ctx.liquidity_levels = []
    return ctx


def _make_registry_with_strategies() -> StrategyRegistry:
    registry = StrategyRegistry(max_strategies=10)
    registry.register("grid-1", "grid", {})
    registry.register("dca-1", "dca", {})
    registry.register("trend-1", "trend_follower", {})
    registry.register("smc-1", "smc", {})
    return registry


# ---------------------------------------------------------------------------
# Tests: TradingMode mapping
# ---------------------------------------------------------------------------


class TestDefaultRegimeToMode:
    """Verify the default regime → mode mapping."""

    def test_tight_range_maps_to_mean_reversion(self):
        assert DEFAULT_REGIME_TO_MODE[MarketRegime.TIGHT_RANGE] == TradingMode.MEAN_REVERSION

    def test_wide_range_maps_to_mean_reversion(self):
        assert DEFAULT_REGIME_TO_MODE[MarketRegime.WIDE_RANGE] == TradingMode.MEAN_REVERSION

    def test_quiet_transition_maps_to_mean_reversion(self):
        assert DEFAULT_REGIME_TO_MODE[MarketRegime.QUIET_TRANSITION] == TradingMode.MEAN_REVERSION

    def test_volatile_transition_maps_to_trend_following(self):
        assert (
            DEFAULT_REGIME_TO_MODE[MarketRegime.VOLATILE_TRANSITION]
            == TradingMode.TREND_FOLLOWING
        )

    def test_bull_trend_maps_to_trend_following(self):
        assert DEFAULT_REGIME_TO_MODE[MarketRegime.BULL_TREND] == TradingMode.TREND_FOLLOWING

    def test_bear_trend_maps_to_trend_following(self):
        assert DEFAULT_REGIME_TO_MODE[MarketRegime.BEAR_TREND] == TradingMode.TREND_FOLLOWING

    def test_accumulation_maps_to_accumulation(self):
        assert DEFAULT_REGIME_TO_MODE[MarketRegime.ACCUMULATION] == TradingMode.ACCUMULATION

    def test_distribution_maps_to_distribution(self):
        assert DEFAULT_REGIME_TO_MODE[MarketRegime.DISTRIBUTION] == TradingMode.DISTRIBUTION

    def test_unknown_maps_to_mean_reversion(self):
        assert DEFAULT_REGIME_TO_MODE[MarketRegime.UNKNOWN] == TradingMode.MEAN_REVERSION

    def test_all_regimes_covered(self):
        """Every MarketRegime must have a mapping."""
        for regime in MarketRegime:
            assert regime in DEFAULT_REGIME_TO_MODE, f"{regime} missing from DEFAULT_REGIME_TO_MODE"


# ---------------------------------------------------------------------------
# Tests: StrategyConductor init
# ---------------------------------------------------------------------------


class TestStrategyConductorInit:
    def test_defaults(self):
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)
        assert conductor.current_mode is None
        assert conductor.last_regime is None
        assert conductor.history == []

    def test_custom_regime_map(self):
        registry = StrategyRegistry()
        custom_map = {MarketRegime.BULL_TREND: TradingMode.ACCUMULATION}
        conductor = StrategyConductor(registry, regime_to_mode=custom_map)
        assert conductor.determine_mode(MarketRegime.BULL_TREND) == TradingMode.ACCUMULATION

    def test_status_before_first_dispatch(self):
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)
        status = conductor.get_status()
        assert status["current_mode"] is None
        assert status["last_regime"] is None
        assert status["last_dispatch"] is None
        assert status["history_count"] == 0


# ---------------------------------------------------------------------------
# Tests: determine_mode
# ---------------------------------------------------------------------------


class TestDetermineMode:
    def test_known_regime_returns_mapped_mode(self):
        conductor = StrategyConductor(StrategyRegistry())
        assert conductor.determine_mode(MarketRegime.ACCUMULATION) == TradingMode.ACCUMULATION
        assert conductor.determine_mode(MarketRegime.DISTRIBUTION) == TradingMode.DISTRIBUTION
        assert conductor.determine_mode(MarketRegime.BULL_TREND) == TradingMode.TREND_FOLLOWING

    def test_unknown_regime_returns_mean_reversion(self):
        conductor = StrategyConductor(StrategyRegistry())
        assert conductor.determine_mode(MarketRegime.UNKNOWN) == TradingMode.MEAN_REVERSION


# ---------------------------------------------------------------------------
# Tests: create_directives
# ---------------------------------------------------------------------------


class TestCreateDirectives:
    def test_returns_directive_for_each_strategy_type_in_mode(self):
        conductor = StrategyConductor(StrategyRegistry())
        directives = conductor.create_directives(TradingMode.MEAN_REVERSION, None, 30000.0)
        # MEAN_REVERSION should produce directives for at least grid and dca
        assert "grid" in directives
        assert "dca" in directives

    def test_directive_has_correct_mode(self):
        conductor = StrategyConductor(StrategyRegistry())
        directives = conductor.create_directives(TradingMode.ACCUMULATION, None, 30000.0)
        for directive in directives.values():
            assert directive.mode == TradingMode.ACCUMULATION

    def test_directive_price_range_fallback_without_smc(self):
        conductor = StrategyConductor(StrategyRegistry())
        directives = conductor.create_directives(TradingMode.MEAN_REVERSION, None, 40000.0)
        for directive in directives.values():
            lower, upper = directive.price_range
            # Fallback is ±5% around current price
            assert lower == 40000.0 * 0.95
            assert upper == 40000.0 * 1.05

    def test_directive_price_range_from_smc_swings(self):
        smc_ctx = _make_smc_context(swing_highs=[32000.0, 31000.0], swing_lows=[28000.0, 29000.0])
        conductor = StrategyConductor(StrategyRegistry())
        directives = conductor.create_directives(TradingMode.MEAN_REVERSION, smc_ctx, 30000.0)
        for directive in directives.values():
            lower, upper = directive.price_range
            assert lower == 28000.0  # min of swing lows
            assert upper == 32000.0  # max of swing highs

    def test_directive_key_levels_from_smc(self):
        smc_ctx = _make_smc_context(structural_levels=[29500.0, 31000.0])
        conductor = StrategyConductor(StrategyRegistry())
        directives = conductor.create_directives(TradingMode.TREND_FOLLOWING, smc_ctx, 30000.0)
        for directive in directives.values():
            assert 29500.0 in directive.key_levels
            assert 31000.0 in directive.key_levels

    def test_directive_key_levels_empty_without_smc(self):
        conductor = StrategyConductor(StrategyRegistry())
        directives = conductor.create_directives(TradingMode.MEAN_REVERSION, None, 30000.0)
        for directive in directives.values():
            assert directive.key_levels == []

    def test_capital_allocation_sums_to_le_one(self):
        conductor = StrategyConductor(StrategyRegistry())
        for mode in TradingMode:
            directives = conductor.create_directives(mode, None, 30000.0)
            total = sum(d.capital_allocation for d in directives.values())
            assert total <= 1.0 + 1e-9, f"Allocations sum to {total} > 1 for mode {mode}"

    def test_distribution_mode_dca_has_no_buy_above_restriction(self):
        conductor = StrategyConductor(StrategyRegistry())
        directives = conductor.create_directives(TradingMode.DISTRIBUTION, None, 35000.0)
        dca_directive = directives.get("dca")
        assert dca_directive is not None
        assert "no_buy_above" in dca_directive.restrictions

    def test_accumulation_mode_smc_short_entries_disabled(self):
        conductor = StrategyConductor(StrategyRegistry())
        directives = conductor.create_directives(TradingMode.ACCUMULATION, None, 30000.0)
        smc_directive = directives.get("smc")
        assert smc_directive is not None
        assert smc_directive.restrictions.get("short_entries_disabled") is True

    def test_trend_following_with_smc_adds_sl_anchor(self):
        smc_ctx = _make_smc_context(structural_levels=[29000.0, 31000.0])
        conductor = StrategyConductor(StrategyRegistry())
        directives = conductor.create_directives(TradingMode.TREND_FOLLOWING, smc_ctx, 30000.0)
        for directive in directives.values():
            if directive.capital_allocation > 0:
                assert "min_sl_anchor" in directive.restrictions


# ---------------------------------------------------------------------------
# Tests: on_regime_change with explicit strategy_instances
# ---------------------------------------------------------------------------


class TestOnRegimeChangeWithInstances:
    def test_calls_set_directive_on_each_strategy(self):
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        # Create mock strategy objects
        grid_strategy = MagicMock()
        dca_strategy = MagicMock()

        analysis = _make_analysis(regime=MarketRegime.TIGHT_RANGE, current_price=30000.0)

        snapshot = conductor.on_regime_change(
            analysis,
            strategy_instances={"grid": grid_strategy, "dca": dca_strategy},
        )

        assert snapshot is not None
        grid_strategy.set_directive.assert_called_once()
        dca_strategy.set_directive.assert_called_once()

    def test_directive_mode_matches_regime(self):
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        mock_strategy = MagicMock()
        analysis = _make_analysis(regime=MarketRegime.ACCUMULATION, current_price=30000.0)

        conductor.on_regime_change(
            analysis,
            strategy_instances={"smc": mock_strategy},
        )

        call_args = mock_strategy.set_directive.call_args
        directive: StrategyDirective = call_args[0][0]
        assert directive.mode == TradingMode.ACCUMULATION

    def test_returns_none_when_no_strategies(self):
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        analysis = _make_analysis(regime=MarketRegime.TIGHT_RANGE)
        snapshot = conductor.on_regime_change(analysis, strategy_instances={})
        assert snapshot is None

    def test_updates_current_mode_and_last_regime(self):
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        mock_strategy = MagicMock()
        analysis = _make_analysis(regime=MarketRegime.BULL_TREND)

        conductor.on_regime_change(
            analysis,
            strategy_instances={"trend_follower": mock_strategy},
        )

        assert conductor.current_mode == TradingMode.TREND_FOLLOWING
        assert conductor.last_regime == MarketRegime.BULL_TREND

    def test_history_grows_on_each_dispatch(self):
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry, max_history=10)

        mock_strategy = MagicMock()
        for regime in [MarketRegime.TIGHT_RANGE, MarketRegime.BULL_TREND, MarketRegime.ACCUMULATION]:
            analysis = _make_analysis(regime=regime)
            conductor.on_regime_change(
                analysis,
                strategy_instances={"grid": mock_strategy},
            )

        assert len(conductor.history) == 3
        # Most recent first
        assert conductor.history[0].regime == MarketRegime.ACCUMULATION

    def test_history_capped_at_max_history(self):
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry, max_history=3)
        mock_strategy = MagicMock()

        for regime in [
            MarketRegime.TIGHT_RANGE,
            MarketRegime.BULL_TREND,
            MarketRegime.ACCUMULATION,
            MarketRegime.DISTRIBUTION,
            MarketRegime.WIDE_RANGE,
        ]:
            analysis = _make_analysis(regime=regime)
            conductor.on_regime_change(
                analysis,
                strategy_instances={"grid": mock_strategy},
            )

        assert len(conductor.history) <= 3

    def test_smc_key_levels_passed_to_strategy(self):
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        smc_ctx = _make_smc_context(structural_levels=[28000.0, 32000.0])
        mock_strategy = MagicMock()
        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND, smc_context=smc_ctx, current_price=30000.0
        )

        conductor.on_regime_change(
            analysis,
            strategy_instances={"trend_follower": mock_strategy},
        )

        call_args = mock_strategy.set_directive.call_args
        directive: StrategyDirective = call_args[0][0]
        assert 28000.0 in directive.key_levels
        assert 32000.0 in directive.key_levels

    def test_strategy_type_without_allocation_skipped(self):
        """Strategies with 0.0 allocation still receive a directive."""
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        # In ACCUMULATION mode, trend_follower gets 0.0 allocation but a
        # directive should still be built so callers can decide what to do.
        mock_tf = MagicMock()
        analysis = _make_analysis(regime=MarketRegime.ACCUMULATION)

        conductor.on_regime_change(
            analysis,
            strategy_instances={"trend_follower": mock_tf},
        )

        mock_tf.set_directive.assert_called_once()
        call_args = mock_tf.set_directive.call_args
        directive: StrategyDirective = call_args[0][0]
        assert directive.capital_allocation == 0.0


# ---------------------------------------------------------------------------
# Tests: DirectiveSnapshot serialization
# ---------------------------------------------------------------------------


class TestDirectiveSnapshot:
    def test_to_dict_keys(self):
        snapshot = DirectiveSnapshot(
            timestamp=datetime.now(timezone.utc),
            mode=TradingMode.TREND_FOLLOWING,
            regime=MarketRegime.BULL_TREND,
            price_range=(28000.0, 32000.0),
            key_levels=[30000.0],
            strategies_notified=["trend_follower", "dca"],
        )
        d = snapshot.to_dict()
        assert d["mode"] == "trend_following"
        assert d["regime"] == "bull_trend"
        assert d["price_range"] == [28000.0, 32000.0]
        assert d["key_levels"] == [30000.0]
        assert "trend_follower" in d["strategies_notified"]


# ---------------------------------------------------------------------------
# Tests: private helpers
# ---------------------------------------------------------------------------


class TestPrivateHelpers:
    def test_extract_key_levels_none_context(self):
        assert _extract_key_levels(None) == []

    def test_extract_key_levels_from_context(self):
        ctx = _make_smc_context(structural_levels=[29000.0, 31500.0])
        assert _extract_key_levels(ctx) == [29000.0, 31500.0]

    def test_extract_liquidity_zones_none_context(self):
        assert _extract_liquidity_zones(None) == []

    def test_extract_liquidity_zones_filters_swept(self):
        swept = MagicMock(swept=True)
        active = MagicMock(swept=False)
        ctx = MagicMock()
        ctx.structural_levels = []
        ctx.swing_highs = []
        ctx.swing_lows = []
        ctx.liquidity_levels = [swept, active]
        result = _extract_liquidity_zones(ctx)
        assert active in result
        assert swept not in result

    def test_compute_price_range_fallback(self):
        lower, upper = _compute_price_range(None, 40000.0, TradingMode.MEAN_REVERSION)
        assert lower == 40000.0 * 0.95
        assert upper == 40000.0 * 1.05

    def test_compute_price_range_from_swings(self):
        ctx = _make_smc_context(swing_highs=[35000.0], swing_lows=[25000.0])
        lower, upper = _compute_price_range(ctx, 30000.0, TradingMode.MEAN_REVERSION)
        assert lower == 25000.0
        assert upper == 35000.0

    def test_compute_price_range_zero_price_returns_zeros(self):
        lower, upper = _compute_price_range(None, 0.0, TradingMode.MEAN_REVERSION)
        assert lower == 0.0
        assert upper == 0.0


# ---------------------------------------------------------------------------
# Tests: get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_status_after_dispatch(self):
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        mock_strategy = MagicMock()
        analysis = _make_analysis(regime=MarketRegime.TIGHT_RANGE)
        conductor.on_regime_change(
            analysis, strategy_instances={"grid": mock_strategy}
        )

        status = conductor.get_status()
        assert status["current_mode"] == TradingMode.MEAN_REVERSION.value
        assert status["last_regime"] == MarketRegime.TIGHT_RANGE.value
        assert status["last_dispatch"] is not None
        assert status["history_count"] == 1
