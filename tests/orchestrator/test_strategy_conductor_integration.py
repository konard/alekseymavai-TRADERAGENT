"""Integration test: SMC phase change → correct directives dispatched to all strategies.

This test verifies the full pipeline:
  RegimeAnalysis (with SMC context) → StrategyConductor.on_regime_change()
  → StrategyDirective dispatched to each active strategy via set_directive()

The goal is to ensure strategies operate with a *unified* set of SMC levels
rather than independently computing their own.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.strategy_conductor import StrategyConductor
from bot.orchestrator.strategy_registry import StrategyRegistry
from bot.strategies.base import (
    StrategyDirective,
    TradingMode,
)
from bot.strategies.dca_adapter import DCAAdapter
from bot.strategies.grid_adapter import GridAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_analysis(
    regime: MarketRegime,
    current_price: float = 30000.0,
    smc_context=None,
) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=regime,
        confidence=0.85,
        recommended_strategy=RecommendedStrategy.GRID,
        confluence_score=0.6,
        trend_strength=0.5,
        volatility_percentile=40.0,
        ema_divergence_pct=0.02,
        atr_pct=0.8,
        rsi=55.0,
        adx=28.0,
        bb_width_pct=2.5,
        volume_ratio=1.1,
        regime_duration_seconds=300,
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
    ctx = MagicMock()
    ctx.structural_levels = structural_levels or []
    ctx.swing_highs = [MagicMock(price=p) for p in (swing_highs or [])]
    ctx.swing_lows = [MagicMock(price=p) for p in (swing_lows or [])]
    ctx.liquidity_levels = []
    return ctx


# ---------------------------------------------------------------------------
# Integration test: SMC ACCUMULATION → all strategies receive directive
# ---------------------------------------------------------------------------


class TestSMCPhaseChangeToAllStrategies:
    """Verify that an SMC phase change dispatches correct directives to each
    active strategy instance so they share unified SMC levels."""

    def test_accumulation_phase_directives_dispatched(self):
        """On ACCUMULATION regime, directives should be pushed to all strategies
        registered in the ``strategy_instances`` mapping."""
        smc_ctx = _make_smc_context(
            structural_levels=[28000.0, 31000.0, 33000.0],
            swing_highs=[33000.0],
            swing_lows=[27000.0],
        )

        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        # Mock strategy objects
        grid = MagicMock()
        dca = MagicMock()
        smc = MagicMock()
        trend_follower = MagicMock()

        analysis = _make_analysis(
            regime=MarketRegime.ACCUMULATION,
            current_price=29000.0,
            smc_context=smc_ctx,
        )

        snapshot = conductor.on_regime_change(
            analysis,
            strategy_instances={
                "grid": grid,
                "dca": dca,
                "smc": smc,
                "trend_follower": trend_follower,
            },
        )

        assert snapshot is not None
        assert snapshot.mode == TradingMode.ACCUMULATION
        assert snapshot.regime == MarketRegime.ACCUMULATION

        # Every strategy should receive exactly one directive call
        grid.set_directive.assert_called_once()
        dca.set_directive.assert_called_once()
        smc.set_directive.assert_called_once()
        trend_follower.set_directive.assert_called_once()

    def test_all_strategies_share_same_key_levels(self):
        """All active strategies receive the same SMC structural levels."""
        smc_ctx = _make_smc_context(structural_levels=[29000.0, 31500.0])
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        strategies = {
            "grid": MagicMock(),
            "dca": MagicMock(),
            "trend_follower": MagicMock(),
        }

        analysis = _make_analysis(
            regime=MarketRegime.BULL_TREND,
            smc_context=smc_ctx,
            current_price=30000.0,
        )
        conductor.on_regime_change(analysis, strategy_instances=strategies)

        received_levels = []
        for strategy in strategies.values():
            directive: StrategyDirective = strategy.set_directive.call_args[0][0]
            received_levels.append(tuple(directive.key_levels))

        # All strategies received the same levels
        assert len(set(received_levels)) == 1
        levels = received_levels[0]
        assert 29000.0 in levels
        assert 31500.0 in levels

    def test_all_strategies_share_same_price_range(self):
        """All active strategies receive the same working price range."""
        smc_ctx = _make_smc_context(
            swing_highs=[34000.0, 33000.0],
            swing_lows=[26000.0, 27000.0],
        )
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        strategies = {"grid": MagicMock(), "dca": MagicMock()}

        analysis = _make_analysis(
            regime=MarketRegime.WIDE_RANGE,
            smc_context=smc_ctx,
            current_price=30000.0,
        )
        conductor.on_regime_change(analysis, strategy_instances=strategies)

        ranges = []
        for strategy in strategies.values():
            directive: StrategyDirective = strategy.set_directive.call_args[0][0]
            ranges.append(directive.price_range)

        # All strategies receive the same range
        assert len(set(ranges)) == 1
        lower, upper = ranges[0]
        assert lower == 26000.0  # min swing low
        assert upper == 34000.0  # max swing high

    def test_distribution_regime_disables_dca_buys(self):
        """In DISTRIBUTION mode DCA must have a no_buy_above restriction."""
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        dca = MagicMock()
        analysis = _make_analysis(
            regime=MarketRegime.DISTRIBUTION, current_price=45000.0
        )

        conductor.on_regime_change(analysis, strategy_instances={"dca": dca})

        directive: StrategyDirective = dca.set_directive.call_args[0][0]
        assert directive.mode == TradingMode.DISTRIBUTION
        assert "no_buy_above" in directive.restrictions

    def test_accumulation_regime_disables_smc_shorts(self):
        """In ACCUMULATION mode SMC must not enter short positions."""
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        smc = MagicMock()
        analysis = _make_analysis(regime=MarketRegime.ACCUMULATION, current_price=29000.0)

        conductor.on_regime_change(analysis, strategy_instances={"smc": smc})

        directive: StrategyDirective = smc.set_directive.call_args[0][0]
        assert directive.mode == TradingMode.ACCUMULATION
        assert directive.restrictions.get("short_entries_disabled") is True

    def test_regime_change_updates_conductor_state(self):
        """Conductor internal state is updated after each on_regime_change call."""
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        mock_strat = MagicMock()
        assert conductor.current_mode is None

        conductor.on_regime_change(
            _make_analysis(MarketRegime.ACCUMULATION),
            strategy_instances={"smc": mock_strat},
        )
        assert conductor.current_mode == TradingMode.ACCUMULATION

        conductor.on_regime_change(
            _make_analysis(MarketRegime.BULL_TREND),
            strategy_instances={"trend_follower": mock_strat},
        )
        assert conductor.current_mode == TradingMode.TREND_FOLLOWING

    def test_multiple_regime_changes_accumulate_history(self):
        """Each call to on_regime_change adds a snapshot to history."""
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        mock_strat = MagicMock()
        regimes = [
            MarketRegime.TIGHT_RANGE,
            MarketRegime.ACCUMULATION,
            MarketRegime.BULL_TREND,
        ]
        for regime in regimes:
            conductor.on_regime_change(
                _make_analysis(regime),
                strategy_instances={"grid": mock_strat},
            )

        assert len(conductor.history) == len(regimes)
        # Most-recent-first order
        assert conductor.history[0].regime == MarketRegime.BULL_TREND
        assert conductor.history[-1].regime == MarketRegime.TIGHT_RANGE


# ---------------------------------------------------------------------------
# Integration test: real adapter objects call set_directive correctly
# ---------------------------------------------------------------------------


class TestRealAdaptersReceiveDirective:
    """Use actual adapter instances to verify set_directive is callable and
    stores the directive without raising."""

    def test_dca_adapter_accepts_directive(self):
        adapter = DCAAdapter(symbol="BTC/USDT")
        directive = StrategyDirective(
            mode=TradingMode.ACCUMULATION,
            price_range=(28000.0, 32000.0),
            key_levels=[29000.0],
            capital_allocation=0.6,
        )
        adapter.set_directive(directive)
        assert adapter._directive is directive

    def test_grid_adapter_accepts_directive(self):
        adapter = GridAdapter(symbol="BTC/USDT")
        directive = StrategyDirective(
            mode=TradingMode.MEAN_REVERSION,
            price_range=(28000.0, 32000.0),
            key_levels=[30000.0],
            capital_allocation=0.7,
        )
        adapter.set_directive(directive)
        assert adapter._directive is directive

    def test_dca_adapter_directive_cleared_on_reset(self):
        adapter = DCAAdapter(symbol="BTC/USDT")
        directive = StrategyDirective(
            mode=TradingMode.ACCUMULATION,
            price_range=(28000.0, 32000.0),
        )
        adapter.set_directive(directive)
        adapter.reset()
        assert adapter._directive is None

    def test_grid_adapter_directive_cleared_on_reset(self):
        adapter = GridAdapter(symbol="BTC/USDT")
        directive = StrategyDirective(
            mode=TradingMode.MEAN_REVERSION,
            price_range=(28000.0, 32000.0),
        )
        adapter.set_directive(directive)
        adapter.reset()
        assert adapter._directive is None

    def test_conductor_dispatches_to_real_dca_adapter(self):
        """Full end-to-end: conductor builds and pushes directive to a real
        DCAAdapter instance."""
        registry = StrategyRegistry()
        conductor = StrategyConductor(registry)

        dca_adapter = DCAAdapter(symbol="BTC/USDT")
        smc_ctx = _make_smc_context(structural_levels=[28500.0, 31500.0])

        analysis = _make_analysis(
            regime=MarketRegime.ACCUMULATION,
            current_price=29500.0,
            smc_context=smc_ctx,
        )

        conductor.on_regime_change(
            analysis,
            strategy_instances={"dca": dca_adapter},
        )

        assert dca_adapter._directive is not None
        assert dca_adapter._directive.mode == TradingMode.ACCUMULATION
        assert 28500.0 in dca_adapter._directive.key_levels
        assert 31500.0 in dca_adapter._directive.key_levels
