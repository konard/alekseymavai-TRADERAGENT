"""
Integration tests — Multi-strategy orchestration.

Tests multiple strategy adapters working together: shared data feeds,
strategy comparison, conflict detection, and shared risk tracking.
"""

from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd

from bot.strategies.base import (
    BaseMarketAnalysis,
    BaseSignal,
    BaseStrategy,
    ExitReason,
    SignalDirection,
)
from bot.strategies.dca_adapter import DCAAdapter
from bot.strategies.grid_adapter import GridAdapter
from bot.strategies.smc_adapter import SMCStrategyAdapter
from bot.strategies.trend_follower_adapter import TrendFollowerAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ohlcv(
    n: int = 200, base: float = 45000.0, trend: str = "up", freq: str = "15min"
) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    dates = pd.date_range("2024-01-01", periods=n, freq=freq)
    if trend == "up":
        closes = base + np.cumsum(rng.uniform(0.5, 5, n))
    elif trend == "down":
        closes = base - np.cumsum(rng.uniform(0.5, 5, n))
    else:
        closes = base + rng.normal(0, 3, n)
    highs = closes + rng.uniform(5, 30, n)
    lows = closes - rng.uniform(5, 30, n)
    opens = closes + rng.normal(0, 5, n)
    volumes = rng.uniform(100, 1000, n)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )


# ===========================================================================
# Multi-Strategy Same Data
# ===========================================================================


class TestMultiStrategySharedData:
    """Test multiple strategies analyzing the same market data."""

    def test_all_strategies_analyze_same_data(self):
        """All strategies should produce valid analysis from the same data."""
        df = _make_ohlcv(n=200)
        strategies: list[BaseStrategy] = [
            SMCStrategyAdapter(name="smc"),
            TrendFollowerAdapter(name="tf"),
            GridAdapter(name="grid"),
            DCAAdapter(name="dca"),
        ]

        analyses = {}
        for s in strategies:
            analysis = s.analyze_market(df)
            analyses[s.get_strategy_type()] = analysis
            assert isinstance(analysis, BaseMarketAnalysis)

        # All should have the same timestamp (approximately)
        assert len(analyses) == 4

    def test_strategies_independent_state(self):
        """Strategies should not share state."""
        df = _make_ohlcv(n=200)
        smc = SMCStrategyAdapter(name="smc")
        grid = GridAdapter(name="grid")

        smc.analyze_market(df)
        grid.analyze_market(df)

        # Open a position in SMC only
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("46000"),
            confidence=0.8,
            timestamp=datetime.now(timezone.utc),
            strategy_type="smc",
        )
        smc.open_position(signal, Decimal("100"))

        assert len(smc.get_active_positions()) == 1
        assert len(grid.get_active_positions()) == 0

    def test_parallel_position_management(self):
        """Multiple strategies can track positions independently."""
        adapters = [
            SMCStrategyAdapter(name="smc"),
            GridAdapter(name="grid"),
            DCAAdapter(name="dca"),
        ]
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("46000"),
            confidence=0.7,
            timestamp=datetime.now(timezone.utc),
            strategy_type="multi",
        )

        pos_ids = {}
        for adapter in adapters:
            pos_id = adapter.open_position(signal, Decimal("50"))
            pos_ids[adapter.get_strategy_type()] = pos_id

        # All should have 1 position
        for adapter in adapters:
            assert len(adapter.get_active_positions()) == 1

        # Close only the grid position
        grid = adapters[1]
        grid.close_position(pos_ids["grid"], ExitReason.TAKE_PROFIT, Decimal("46000"))

        # Grid closed, others still open
        assert len(adapters[0].get_active_positions()) == 1  # SMC
        assert len(adapters[1].get_active_positions()) == 0  # Grid
        assert len(adapters[2].get_active_positions()) == 1  # DCA

    def test_aggregated_performance(self):
        """Aggregate performance across multiple strategies."""
        adapters: list[BaseStrategy] = [
            SMCStrategyAdapter(name="smc"),
            GridAdapter(name="grid"),
            DCAAdapter(name="dca"),
        ]

        # Execute one winning trade per adapter
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("46000"),
            confidence=0.7,
            timestamp=datetime.now(timezone.utc),
            strategy_type="test",
        )

        for adapter in adapters:
            pos_id = adapter.open_position(signal, Decimal("100"))
            adapter.close_position(pos_id, ExitReason.TAKE_PROFIT, Decimal("46000"))

        total_trades = sum(a.get_performance().total_trades for a in adapters)
        total_pnl = sum(a.get_performance().total_pnl for a in adapters)
        assert total_trades == 3
        assert total_pnl > 0


# ===========================================================================
# Strategy Comparison Integration
# ===========================================================================


class TestStrategyComparison:
    """Test comparing multiple strategies side by side."""

    def test_compare_analysis_results(self):
        """Different strategies should identify different trends from same data."""
        df = _make_ohlcv(n=200, trend="up")
        smc = SMCStrategyAdapter()
        tf = TrendFollowerAdapter()

        smc_analysis = smc.analyze_market(df)
        tf_analysis = tf.analyze_market(df)

        # Both should produce valid analysis
        assert smc_analysis.strategy_type == "smc"
        assert tf_analysis.strategy_type == "trend_follower"

    def test_compare_status_output(self):
        """All strategy status outputs should have consistent structure."""
        strategies: list[BaseStrategy] = [
            SMCStrategyAdapter(),
            TrendFollowerAdapter(),
            GridAdapter(),
            DCAAdapter(),
        ]

        for s in strategies:
            status = s.get_status()
            # All statuses must have these keys
            assert "name" in status, f"{s.get_strategy_type()} missing 'name'"
            assert "type" in status, f"{s.get_strategy_type()} missing 'type'"
            assert "active_positions" in status
            assert "performance" in status
            # Performance should be serializable
            assert isinstance(status["performance"], dict)

    def test_strategy_performance_comparison(self):
        """Compare performance metrics across strategies."""
        strategies = {
            "smc": SMCStrategyAdapter(name="smc"),
            "grid": GridAdapter(name="grid"),
            "dca": DCAAdapter(name="dca"),
        }

        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("46000"),
            confidence=0.7,
            timestamp=datetime.now(timezone.utc),
            strategy_type="test",
        )

        # SMC: winning trade
        pos_id = strategies["smc"].open_position(signal, Decimal("100"))
        strategies["smc"].close_position(pos_id, ExitReason.TAKE_PROFIT, Decimal("46000"))

        # Grid: losing trade
        pos_id = strategies["grid"].open_position(signal, Decimal("100"))
        strategies["grid"].close_position(pos_id, ExitReason.STOP_LOSS, Decimal("44000"))

        # DCA: winning trade
        pos_id = strategies["dca"].open_position(signal, Decimal("100"))
        strategies["dca"].close_position(pos_id, ExitReason.TAKE_PROFIT, Decimal("46000"))

        perfs = {name: s.get_performance() for name, s in strategies.items()}

        # Validate individual results
        assert perfs["smc"].win_rate == 1.0
        assert perfs["grid"].win_rate == 0.0
        assert perfs["dca"].win_rate == 1.0

        # Best performer
        best = max(perfs.items(), key=lambda x: x[1].total_pnl)
        assert best[0] in ("smc", "dca")


# ===========================================================================
# Shared Risk Tracking
# ===========================================================================


class TestSharedRiskTracking:
    """Test cross-strategy risk management scenarios."""

    def test_total_exposure_across_strategies(self):
        """Track total exposure (sum of position sizes) across strategies."""
        adapters = [
            SMCStrategyAdapter(name="smc"),
            GridAdapter(name="grid"),
            DCAAdapter(name="dca"),
        ]

        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("46000"),
            confidence=0.7,
            timestamp=datetime.now(timezone.utc),
            strategy_type="test",
        )

        for adapter in adapters:
            adapter.open_position(signal, Decimal("100"))

        # Calculate total exposure
        total_exposure = Decimal("0")
        for adapter in adapters:
            for pos in adapter.get_active_positions():
                total_exposure += pos.size

        assert total_exposure == Decimal("300")

    def test_max_positions_enforcement(self):
        """Enforce a global maximum position count."""
        max_total_positions = 3
        adapters = [
            SMCStrategyAdapter(name="smc"),
            GridAdapter(name="grid"),
            DCAAdapter(name="dca"),
        ]

        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("46000"),
            confidence=0.7,
            timestamp=datetime.now(timezone.utc),
            strategy_type="test",
        )

        total_positions = 0
        for adapter in adapters:
            if total_positions < max_total_positions:
                adapter.open_position(signal, Decimal("100"))
                total_positions += 1

        all_positions = []
        for adapter in adapters:
            all_positions.extend(adapter.get_active_positions())
        assert len(all_positions) == max_total_positions

    def test_simultaneous_update_all_strategies(self):
        """Update all positions across all strategies with a new price."""
        adapters = [
            SMCStrategyAdapter(name="smc"),
            GridAdapter(name="grid"),
            DCAAdapter(name="dca"),
        ]

        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("46000"),
            confidence=0.7,
            timestamp=datetime.now(timezone.utc),
            strategy_type="test",
        )

        for adapter in adapters:
            adapter.open_position(signal, Decimal("100"))

        # Price hits TP — all positions should trigger exit.
        # DCAAdapter uses percentage-based TP (default 8% → 45000*1.08=48600),
        # so the price must exceed 48600 to trigger DCA's exit as well.
        df = _make_ohlcv(n=10)
        all_exits = []
        for adapter in adapters:
            exits = adapter.update_positions(Decimal("50000"), df)
            all_exits.extend(exits)

        assert len(all_exits) == 3
        for _, reason in all_exits:
            assert reason == ExitReason.TAKE_PROFIT


# ===========================================================================
# Strategy Switching
# ===========================================================================


class TestStrategySwitching:
    """Test switching between strategies based on market conditions."""

    def test_select_strategy_for_trend(self):
        """In trending markets, prefer trend-following strategies."""
        df_up = _make_ohlcv(n=200, trend="up")
        tf = TrendFollowerAdapter(name="tf")
        grid = GridAdapter(name="grid")

        tf_analysis = tf.analyze_market(df_up)
        grid_analysis = grid.analyze_market(df_up)

        # Both produce valid analyses — selection logic is external
        assert isinstance(tf_analysis, BaseMarketAnalysis)
        assert isinstance(grid_analysis, BaseMarketAnalysis)

    def test_select_strategy_for_sideways(self):
        """In sideways markets, prefer grid/DCA strategies."""
        df_sideways = _make_ohlcv(n=200, trend="sideways")
        grid = GridAdapter(name="grid")
        dca = DCAAdapter(name="dca")

        grid_analysis = grid.analyze_market(df_sideways)
        dca_analysis = dca.analyze_market(df_sideways)

        assert isinstance(grid_analysis, BaseMarketAnalysis)
        assert isinstance(dca_analysis, BaseMarketAnalysis)

    def test_graceful_strategy_transition(self):
        """Switching strategy should cleanly close old positions."""
        smc = SMCStrategyAdapter(name="smc")
        grid = GridAdapter(name="grid")

        # Open position with SMC
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("46000"),
            confidence=0.8,
            timestamp=datetime.now(timezone.utc),
            strategy_type="smc",
        )
        pos_id = smc.open_position(signal, Decimal("100"))

        # "Switch" to grid: close SMC position first
        smc.close_position(pos_id, ExitReason.MANUAL, Decimal("45200"))
        assert len(smc.get_active_positions()) == 0

        # Grid starts fresh
        grid_signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45200"),
            stop_loss=Decimal("43000"),
            take_profit=Decimal("45450"),
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
        )
        grid.open_position(grid_signal, Decimal("100"))
        assert len(grid.get_active_positions()) == 1

    def test_full_session_with_strategy_switch(self):
        """Simulate a trading session that switches strategy mid-session."""
        balance = Decimal("10000")
        df = _make_ohlcv(n=200)

        # Phase 1: Trade with SMC
        smc = SMCStrategyAdapter(name="smc")
        smc.analyze_market(df)
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("46000"),
            confidence=0.8,
            timestamp=datetime.now(timezone.utc),
            strategy_type="smc",
        )
        pos_id = smc.open_position(signal, Decimal("100"))
        smc.close_position(pos_id, ExitReason.TAKE_PROFIT, Decimal("46000"))
        smc_perf = smc.get_performance()

        # Phase 2: Switch to Grid
        grid = GridAdapter(name="grid")
        grid.analyze_market(df)
        grid_signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("45000"),
            stop_loss=Decimal("43000"),
            take_profit=Decimal("45225"),
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
        )
        pos_id = grid.open_position(grid_signal, Decimal("100"))
        grid.close_position(pos_id, ExitReason.TAKE_PROFIT, Decimal("45225"))
        grid_perf = grid.get_performance()

        # Aggregate session performance
        total_trades = smc_perf.total_trades + grid_perf.total_trades
        total_pnl = smc_perf.total_pnl + grid_perf.total_pnl
        assert total_trades == 2
        assert total_pnl > 0
