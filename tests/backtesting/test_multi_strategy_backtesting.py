"""Tests for multi-strategy backtesting — Issue #173."""

from datetime import datetime
from decimal import Decimal

import pandas as pd
import pytest

from bot.strategies.base import (
    BaseMarketAnalysis,
    BaseSignal,
    ExitReason,
    SignalDirection,
)
from bot.strategies.dca_adapter import DCAAdapter
from bot.strategies.grid_adapter import GridAdapter
from bot.tests.backtesting.backtesting_engine import BacktestResult
from bot.tests.backtesting.multi_tf_data_loader import (
    MultiTimeframeDataLoader,
)
from bot.tests.backtesting.multi_tf_engine import (
    MultiTFBacktestConfig,
    MultiTimeframeBacktestEngine,
)
from bot.tests.backtesting.strategy_comparison import (
    StrategyComparison,
    StrategyComparisonResult,
)
from tests.strategies.test_base_strategy import ConcreteStrategy, _make_ohlcv

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def loader():
    return MultiTimeframeDataLoader()


@pytest.fixture
def data_7days(loader):
    return loader.load(
        symbol="BTC/USDT",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 1, 8),
        trend="up",
    )


@pytest.fixture
def config():
    return MultiTFBacktestConfig(
        symbol="BTC/USDT",
        initial_balance=Decimal("10000"),
        warmup_bars=20,
    )


@pytest.fixture
def engine(config):
    return MultiTimeframeBacktestEngine(config=config)


# =============================================================================
# GridAdapter Unit Tests
# =============================================================================


class TestGridAdapterUnit:
    """Unit tests for GridAdapter."""

    def test_strategy_name_and_type(self):
        adapter = GridAdapter(name="grid-test")
        assert adapter.get_strategy_name() == "grid-test"
        assert adapter.get_strategy_type() == "grid"

    def test_analyze_market(self):
        adapter = GridAdapter()
        df = _make_ohlcv([100 + i * 0.1 for i in range(50)])
        result = adapter.analyze_market(df)
        assert isinstance(result, BaseMarketAnalysis)
        assert result.strategy_type == "grid"
        assert "upper_price" in result.details
        assert "lower_price" in result.details

    def test_analyze_market_empty_df(self):
        adapter = GridAdapter()
        result = adapter.analyze_market(pd.DataFrame())
        assert result.trend == "unknown"

    def test_generate_signal_requires_analysis(self):
        adapter = GridAdapter()
        df = _make_ohlcv([100, 101, 102])
        # No analysis yet, no grid levels
        signal = adapter.generate_signal(df, Decimal("10000"))
        assert signal is None

    def test_open_and_close_position(self):
        adapter = GridAdapter()
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            take_profit=Decimal("105"),
            confidence=0.6,
            timestamp=datetime(2024, 1, 1),
            strategy_type="grid",
        )
        pos_id = adapter.open_position(signal, Decimal("10"))
        assert len(adapter.get_active_positions()) == 1

        adapter.close_position(pos_id, ExitReason.TAKE_PROFIT, Decimal("105"))
        assert len(adapter.get_active_positions()) == 0
        assert adapter.get_performance().total_trades == 1

    def test_update_positions_take_profit(self):
        adapter = GridAdapter()
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            take_profit=Decimal("105"),
            confidence=0.6,
            timestamp=datetime(2024, 1, 1),
            strategy_type="grid",
        )
        adapter.open_position(signal, Decimal("10"))
        df = _make_ohlcv([100])

        exits = adapter.update_positions(Decimal("106"), df)
        assert len(exits) == 1
        assert exits[0][1] == ExitReason.TAKE_PROFIT

    def test_update_positions_stop_loss(self):
        # Grid SL is grid-boundary based: all positions close when price < _grid_lower_price.
        adapter = GridAdapter()
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            take_profit=Decimal("105"),
            confidence=0.6,
            timestamp=datetime(2024, 1, 1),
            strategy_type="grid",
        )
        adapter.open_position(signal, Decimal("10"))
        adapter._grid_lower_price = Decimal("95")  # simulate initialized grid boundary
        df = _make_ohlcv([100])

        exits = adapter.update_positions(Decimal("94"), df)
        assert len(exits) == 1
        assert exits[0][1] == ExitReason.STOP_LOSS

    def test_reset(self):
        adapter = GridAdapter()
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            take_profit=Decimal("105"),
            confidence=0.6,
            timestamp=datetime(2024, 1, 1),
            strategy_type="grid",
        )
        adapter.open_position(signal, Decimal("10"))
        adapter.reset()
        assert len(adapter.get_active_positions()) == 0

    def test_performance_empty(self):
        adapter = GridAdapter()
        perf = adapter.get_performance()
        assert perf.total_trades == 0


# =============================================================================
# DCAAdapter Unit Tests
# =============================================================================


class TestDCAAdapterUnit:
    """Unit tests for DCAAdapter."""

    def test_strategy_name_and_type(self):
        adapter = DCAAdapter(name="dca-test")
        assert adapter.get_strategy_name() == "dca-test"
        assert adapter.get_strategy_type() == "dca"

    def test_analyze_market(self):
        adapter = DCAAdapter()
        df = _make_ohlcv([100 - i * 0.1 for i in range(50)])
        result = adapter.analyze_market(df)
        assert isinstance(result, BaseMarketAnalysis)
        assert result.strategy_type == "dca"
        assert "recent_high" in result.details
        assert "deviation_from_high" in result.details

    def test_analyze_market_empty_df(self):
        adapter = DCAAdapter()
        result = adapter.analyze_market(pd.DataFrame())
        assert result.trend == "unknown"

    def test_generate_signal_no_drop(self):
        adapter = DCAAdapter()
        # Prices going up — no DCA entry
        prices = [100 + i for i in range(50)]
        df = _make_ohlcv(prices)
        adapter.analyze_market(df)
        signal = adapter.generate_signal(df, Decimal("10000"))
        assert signal is None

    def test_open_and_close_position(self):
        adapter = DCAAdapter()
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("80"),
            take_profit=Decimal("105"),
            confidence=0.7,
            timestamp=datetime(2024, 1, 1),
            strategy_type="dca",
        )
        pos_id = adapter.open_position(signal, Decimal("10"))
        assert len(adapter.get_active_positions()) == 1

        adapter.close_position(pos_id, ExitReason.TAKE_PROFIT, Decimal("105"))
        assert len(adapter.get_active_positions()) == 0

    def test_safety_order_averaging(self):
        """DCA should average down when price drops by safety step."""
        adapter = DCAAdapter(
            safety_step_pct=Decimal("0.02"),
            safety_order_size=Decimal("200"),
        )
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("80"),
            take_profit=Decimal("102"),
            confidence=0.7,
            timestamp=datetime(2024, 1, 1),
            strategy_type="dca",
        )
        adapter.open_position(signal, Decimal("10"))
        df = _make_ohlcv([100])

        # Drop price by 2% to trigger first safety order
        adapter.update_positions(Decimal("98"), df)

        positions = adapter.get_active_positions()
        assert len(positions) == 1
        # Safety order should have increased position size
        assert positions[0].metadata.get("safety_orders_filled") == 1

    def test_reset(self):
        adapter = DCAAdapter()
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("80"),
            take_profit=Decimal("105"),
            confidence=0.7,
            timestamp=datetime(2024, 1, 1),
            strategy_type="dca",
        )
        adapter.open_position(signal, Decimal("10"))
        adapter.reset()
        assert len(adapter.get_active_positions()) == 0

    def test_performance_with_trades(self):
        adapter = DCAAdapter()
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("80"),
            take_profit=Decimal("105"),
            confidence=0.7,
            timestamp=datetime(2024, 1, 1),
            strategy_type="dca",
        )
        adapter.open_position(signal, Decimal("10"))
        adapter.close_position("nonexistent", ExitReason.TAKE_PROFIT, Decimal("105"))
        # That was a non-existent position, so performance should still be 0
        assert adapter.get_performance().total_trades == 0


# =============================================================================
# Grid Backtest Integration Tests
# =============================================================================


class TestGridBacktestIntegration:
    """Test GridAdapter through the backtest engine."""

    async def test_grid_backtest_runs(self, engine, data_7days):
        strategy = GridAdapter(name="grid-backtest")
        result = await engine.run(strategy, data_7days)
        assert isinstance(result, BacktestResult)
        assert result.strategy_name == "grid-backtest"
        assert len(result.equity_curve) > 0

    async def test_grid_backtest_initial_balance(self, engine, data_7days):
        strategy = GridAdapter(name="grid-test")
        result = await engine.run(strategy, data_7days)
        assert result.initial_balance == Decimal("10000")


# =============================================================================
# DCA Backtest Integration Tests
# =============================================================================


class TestDCABacktestIntegration:
    """Test DCAAdapter through the backtest engine."""

    async def test_dca_backtest_runs(self, engine, data_7days):
        strategy = DCAAdapter(name="dca-backtest")
        result = await engine.run(strategy, data_7days)
        assert isinstance(result, BacktestResult)
        assert result.strategy_name == "dca-backtest"
        assert len(result.equity_curve) > 0

    async def test_dca_backtest_initial_balance(self, engine, data_7days):
        strategy = DCAAdapter(name="dca-test")
        result = await engine.run(strategy, data_7days)
        assert result.initial_balance == Decimal("10000")


# =============================================================================
# SMC Integration (with patched config)
# =============================================================================


class TestSMCBacktestIntegration:
    """Test SMCStrategyAdapter through multi-TF engine."""

    async def test_smc_backtest(self):
        from bot.strategies.smc.config import SMCConfig
        from bot.strategies.smc_adapter import SMCStrategyAdapter

        smc_config = SMCConfig()
        smc_config.risk_per_trade_pct = smc_config.risk_per_trade
        smc_config.max_position_size_usd = smc_config.max_position_size

        config = MultiTFBacktestConfig(
            initial_balance=Decimal("10000"),
            warmup_bars=60,
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = SMCStrategyAdapter(
            config=smc_config,
            account_balance=Decimal("10000"),
            name="smc-backtest",
        )

        result = await engine.run_with_generated_data(
            strategy=strategy,
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 5),
            trend="up",
        )

        assert result.strategy_name == "smc-backtest"
        assert len(result.equity_curve) > 0


# =============================================================================
# TrendFollower Integration
# =============================================================================


class TestTrendFollowerBacktestIntegration:
    """Test TrendFollowerAdapter through multi-TF engine."""

    async def test_trend_follower_backtest(self, config):
        from bot.strategies.trend_follower_adapter import TrendFollowerAdapter

        config.warmup_bars = 60
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = TrendFollowerAdapter(
            initial_capital=Decimal("10000"),
            name="tf-backtest",
            log_trades=False,
        )

        result = await engine.run_with_generated_data(
            strategy=strategy,
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 8),
            trend="up",
        )

        assert result.strategy_name == "tf-backtest"
        assert len(result.equity_curve) > 0


# =============================================================================
# Strategy Comparison Tests
# =============================================================================


class TestStrategyComparison:
    """Tests for StrategyComparison utility."""

    async def test_comparison_runs(self, data_7days, config):
        comparison = StrategyComparison(config=config)
        strategies = [
            ConcreteStrategy(),
            GridAdapter(name="grid-compare"),
            DCAAdapter(name="dca-compare"),
        ]

        result = await comparison.run(strategies, data_7days)
        assert isinstance(result, StrategyComparisonResult)
        assert "test-concrete" in result.results
        assert "grid-compare" in result.results
        assert "dca-compare" in result.results

    async def test_comparison_rankings(self, data_7days, config):
        comparison = StrategyComparison(config=config)
        strategies = [
            ConcreteStrategy(),
            GridAdapter(name="grid-rank"),
        ]

        result = await comparison.run(strategies, data_7days)
        assert "total_return_pct" in result.rankings
        assert "win_rate" in result.rankings
        assert "sharpe_ratio" in result.rankings
        assert "max_drawdown_pct" in result.rankings

    async def test_comparison_summary(self, data_7days, config):
        comparison = StrategyComparison(config=config)
        strategies = [ConcreteStrategy()]

        result = await comparison.run(strategies, data_7days)
        assert "test-concrete" in result.summary
        summary = result.summary["test-concrete"]
        assert "total_return_pct" in summary
        assert "final_balance" in summary
        assert "total_trades" in summary
        assert "win_rate" in summary

    async def test_comparison_get_winner(self, data_7days, config):
        comparison = StrategyComparison(config=config)
        strategies = [
            ConcreteStrategy(),
            GridAdapter(name="grid-winner"),
        ]

        result = await comparison.run(strategies, data_7days)
        winner = result.get_winner("total_return_pct")
        assert winner is not None
        assert winner in ("test-concrete", "grid-winner")

    async def test_comparison_format_report(self, data_7days, config):
        comparison = StrategyComparison(config=config)
        strategies = [
            ConcreteStrategy(),
            GridAdapter(name="grid-report"),
            DCAAdapter(name="dca-report"),
        ]

        result = await comparison.run(strategies, data_7days)
        report = StrategyComparison.format_report(result)
        assert "STRATEGY COMPARISON REPORT" in report
        assert "test-concrete" in report
        assert "grid-report" in report
        assert "dca-report" in report
        assert "Rankings:" in report

    async def test_comparison_with_generated_data(self):
        config = MultiTFBacktestConfig(
            initial_balance=Decimal("10000"),
            warmup_bars=20,
        )
        comparison = StrategyComparison(config=config)
        strategies = [
            ConcreteStrategy(),
            GridAdapter(name="grid-gen"),
        ]

        result = await comparison.run_with_generated_data(
            strategies=strategies,
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 8),
            trend="up",
        )

        assert len(result.results) == 2


# =============================================================================
# Multi-Trend Comparison Tests
# =============================================================================


class TestMultiTrendComparison:
    """Test strategies across different market conditions."""

    async def test_uptrend_vs_downtrend(self, config):
        loader = MultiTimeframeDataLoader()
        data_up = loader.load("BTC/USDT", datetime(2024, 1, 1), datetime(2024, 1, 8), trend="up")
        data_down = loader.load(
            "BTC/USDT", datetime(2024, 1, 1), datetime(2024, 1, 8), trend="down"
        )

        strategy = GridAdapter(name="grid-trend-test")

        engine_up = MultiTimeframeBacktestEngine(config=config)
        result_up = await engine_up.run(strategy, data_up)

        strategy2 = GridAdapter(name="grid-trend-test")
        engine_down = MultiTimeframeBacktestEngine(config=config)
        result_down = await engine_down.run(strategy2, data_down)

        # Both should complete
        assert len(result_up.equity_curve) > 0
        assert len(result_down.equity_curve) > 0

    async def test_all_strategies_same_data(self, config, data_7days):
        """All four strategy types should run on the same data without errors."""
        from bot.strategies.trend_follower_adapter import TrendFollowerAdapter

        config.warmup_bars = 60
        strategies = [
            GridAdapter(name="grid-all"),
            DCAAdapter(name="dca-all"),
            TrendFollowerAdapter(
                initial_capital=Decimal("10000"),
                name="tf-all",
                log_trades=False,
            ),
        ]

        # Load longer data for TF warmup
        loader = MultiTimeframeDataLoader()
        data = loader.load("BTC/USDT", datetime(2024, 1, 1), datetime(2024, 1, 15), trend="up")

        for strategy in strategies:
            engine = MultiTimeframeBacktestEngine(config=config)
            result = await engine.run(strategy, data)
            assert isinstance(result, BacktestResult)
            assert len(result.equity_curve) > 0
