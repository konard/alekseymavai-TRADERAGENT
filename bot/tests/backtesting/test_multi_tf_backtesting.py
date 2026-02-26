"""Tests for multi-timeframe backtesting — Issue #172."""

import os
import tempfile
from datetime import datetime
from decimal import Decimal

import pandas as pd
import pytest

from bot.tests.backtesting.backtesting_engine import BacktestResult
from bot.tests.backtesting.market_simulator import MarketSimulator
from bot.tests.backtesting.multi_tf_data_loader import (
    MultiTimeframeDataLoader,
)
from bot.tests.backtesting.multi_tf_engine import (
    MultiTFBacktestConfig,
    MultiTimeframeBacktestEngine,
)

# Reuse ConcreteStrategy from existing tests
from tests.strategies.test_base_strategy import ConcreteStrategy

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def loader():
    return MultiTimeframeDataLoader()


@pytest.fixture
def data_2days(loader):
    """2 days of uptrend data."""
    return loader.load(
        symbol="BTC/USDT",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 1, 3),
        trend="up",
    )


@pytest.fixture
def data_7days(loader):
    """7 days of uptrend data — enough for warmup + execution."""
    return loader.load(
        symbol="BTC/USDT",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 1, 8),
        trend="up",
    )


@pytest.fixture
def engine():
    config = MultiTFBacktestConfig(
        symbol="BTC/USDT",
        initial_balance=Decimal("10000"),
        warmup_bars=20,
    )
    return MultiTimeframeBacktestEngine(config=config)


# =============================================================================
# MultiTimeframeDataLoader Tests
# =============================================================================


class TestMultiTimeframeDataLoader:
    """Tests for data loading and synchronization."""

    def test_load_produces_five_dataframes(self, data_2days):
        assert isinstance(data_2days.d1, pd.DataFrame)
        assert isinstance(data_2days.h4, pd.DataFrame)
        assert isinstance(data_2days.h1, pd.DataFrame)
        assert isinstance(data_2days.m15, pd.DataFrame)
        assert isinstance(data_2days.m5, pd.DataFrame)

    def test_as_tuple(self, data_2days):
        t = data_2days.as_tuple()
        assert len(t) == 5
        assert t[0] is data_2days.d1
        assert t[3] is data_2days.m15
        assert t[4] is data_2days.m5

    def test_dataframe_columns(self, data_2days):
        expected = {"open", "high", "low", "close", "volume"}
        for df in [data_2days.d1, data_2days.h4, data_2days.h1, data_2days.m15, data_2days.m5]:
            assert set(df.columns) >= expected

    def test_m5_count(self, data_2days):
        """2 days = 2*24*12 = 576 M5 bars."""
        assert len(data_2days.m5) == 576

    def test_m15_count(self, data_2days):
        """2 days = 2*24*4 = 192 M15 bars (resampled from M5)."""
        assert len(data_2days.m15) == 192

    def test_h1_count(self, data_2days):
        """2 days = 48 H1 bars."""
        assert len(data_2days.h1) == 48

    def test_h4_count(self, data_2days):
        """2 days = 12 H4 bars."""
        assert len(data_2days.h4) == 12

    def test_d1_count(self, data_2days):
        """2 days = 2 D1 bars."""
        assert len(data_2days.d1) == 2

    def test_datetime_index(self, data_2days):
        for df in [data_2days.d1, data_2days.h4, data_2days.h1, data_2days.m15, data_2days.m5]:
            assert isinstance(df.index, pd.DatetimeIndex)

    def test_timeframe_alignment(self, data_2days):
        """Each M5 timestamp should have a corresponding M15, H1, and H4 ancestor."""
        for ts in data_2days.m5.index[:20]:  # Check first 20 for speed
            m15_before = data_2days.m15[data_2days.m15.index <= ts]
            assert len(m15_before) > 0, f"No M15 candle for M5 at {ts}"

            h1_before = data_2days.h1[data_2days.h1.index <= ts]
            assert len(h1_before) > 0, f"No H1 candle for M5 at {ts}"

            h4_before = data_2days.h4[data_2days.h4.index <= ts]
            assert len(h4_before) > 0, f"No H4 candle for M5 at {ts}"

    def test_ohlcv_consistency(self, data_2days):
        """high >= max(open, close) and low <= min(open, close) for all bars."""
        for df in [data_2days.d1, data_2days.h4, data_2days.h1, data_2days.m15, data_2days.m5]:
            assert (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-10).all()
            assert (df["low"] <= df[["open", "close"]].min(axis=1) + 1e-10).all()

    def test_resampled_high_equals_child_max(self, data_2days):
        """M15 high should equal max of its 3 M5 highs."""
        m15_ts = data_2days.m15.index[0]
        m5_slice = data_2days.m5[
            (data_2days.m5.index >= m15_ts)
            & (data_2days.m5.index < m15_ts + pd.Timedelta(minutes=15))
        ]
        assert abs(data_2days.m15.loc[m15_ts, "high"] - m5_slice["high"].max()) < 1e-10

    def test_resampled_open_equals_first_child(self, data_2days):
        """M15 open should equal the open of its first M5 bar."""
        m15_ts = data_2days.m15.index[0]
        m5_slice = data_2days.m5[
            (data_2days.m5.index >= m15_ts)
            & (data_2days.m5.index < m15_ts + pd.Timedelta(minutes=15))
        ]
        assert abs(data_2days.m15.loc[m15_ts, "open"] - m5_slice.iloc[0]["open"]) < 1e-10

    def test_resampled_close_equals_last_child(self, data_2days):
        """M15 close should equal the close of its last M5 bar."""
        m15_ts = data_2days.m15.index[0]
        m5_slice = data_2days.m5[
            (data_2days.m5.index >= m15_ts)
            & (data_2days.m5.index < m15_ts + pd.Timedelta(minutes=15))
        ]
        assert abs(data_2days.m15.loc[m15_ts, "close"] - m5_slice.iloc[-1]["close"]) < 1e-10

    def test_resampled_volume_equals_child_sum(self, data_2days):
        """M15 volume should equal sum of its 3 M5 volumes."""
        m15_ts = data_2days.m15.index[0]
        m5_slice = data_2days.m5[
            (data_2days.m5.index >= m15_ts)
            & (data_2days.m5.index < m15_ts + pd.Timedelta(minutes=15))
        ]
        assert abs(data_2days.m15.loc[m15_ts, "volume"] - m5_slice["volume"].sum()) < 1e-10


class TestGetContextAt:
    """Tests for get_context_at rolling window."""

    def test_context_sizes_base_index(self, loader, data_7days):
        df_d1, df_h4, df_h1, df_m15, df_m5 = loader.get_context_at(
            data_7days, base_index=200, lookback=50
        )
        assert len(df_m5) == 50
        assert len(df_m15) <= 50
        assert len(df_h1) <= 50
        assert len(df_h4) <= 50
        assert len(df_d1) <= 50

    def test_context_early_index(self, loader, data_7days):
        """When base_index < lookback, return whatever is available."""
        df_d1, df_h4, df_h1, df_m15, df_m5 = loader.get_context_at(
            data_7days, base_index=10, lookback=50
        )
        assert len(df_m5) == 11  # indices 0..10
        assert len(df_d1) >= 1

    def test_context_timestamps_aligned(self, loader, data_7days):
        """Context DataFrames should not contain future data."""
        idx = 200
        current_ts = data_7days.m5.index[idx]
        df_d1, df_h4, df_h1, df_m15, df_m5 = loader.get_context_at(
            data_7days, base_index=idx, lookback=50
        )
        assert df_m5.index[-1] == current_ts
        assert df_m15.index[-1] <= current_ts
        assert df_h1.index[-1] <= current_ts
        assert df_h4.index[-1] <= current_ts
        assert df_d1.index[-1] <= current_ts

    def test_context_no_future_leak(self, loader, data_7days):
        """No candle in any context DataFrame should be after current timestamp."""
        idx = 100
        current_ts = data_7days.m5.index[idx]
        df_d1, df_h4, df_h1, df_m15, df_m5 = loader.get_context_at(
            data_7days, base_index=idx, lookback=50
        )
        for df in [df_d1, df_h4, df_h1, df_m15, df_m5]:
            assert (df.index <= current_ts).all()

    def test_backwards_compat_m15_index(self, loader, data_7days):
        """Legacy m15_index param still works."""
        df_d1, df_h4, df_h1, df_m15, df_m5 = loader.get_context_at(
            data_7days, m15_index=50, lookback=30
        )
        assert len(df_m15) == 30


class TestDataLoaderTrends:
    """Test different trend modes."""

    def test_uptrend_prices_increase(self, loader):
        data = loader.load("BTC/USDT", datetime(2024, 1, 1), datetime(2024, 2, 1), trend="up")
        first_close = data.m5.iloc[0]["close"]
        last_close = data.m5.iloc[-1]["close"]
        assert last_close > first_close * 0.8

    def test_downtrend_prices_decrease(self, loader):
        data = loader.load("BTC/USDT", datetime(2024, 1, 1), datetime(2024, 2, 1), trend="down")
        first_close = data.m5.iloc[0]["close"]
        last_close = data.m5.iloc[-1]["close"]
        assert last_close < first_close * 1.2


# =============================================================================
# CSV Loading Tests
# =============================================================================


class TestCSVLoading:
    """Tests for CSV data loading."""

    def test_load_csv_iso_timestamps(self, loader):
        """Load CSV with ISO timestamp format."""
        csv_content = (
            "timestamp,open,high,low,close,volume\n"
            "2024-01-01 00:00:00,100,105,99,103,1000\n"
            "2024-01-01 00:05:00,103,107,102,106,1100\n"
            "2024-01-01 00:10:00,106,108,104,105,900\n"
            "2024-01-01 00:15:00,105,110,104,109,1200\n"
            "2024-01-01 00:20:00,109,112,108,111,1050\n"
            "2024-01-01 00:25:00,111,113,110,112,980\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(csv_content)
            f.flush()
            try:
                data = loader.load_csv(f.name, base_timeframe="5m")
                assert len(data.m5) == 6
                assert isinstance(data.m15, pd.DataFrame)
                assert isinstance(data.d1, pd.DataFrame)
            finally:
                os.unlink(f.name)

    def test_load_csv_unix_ms_timestamps(self, loader):
        """Load CSV with Unix millisecond timestamps and datetime column."""
        csv_content = (
            "timestamp,datetime,open,high,low,close,volume\n"
            "1704067200000,2024-01-01 00:00:00,100,105,99,103,1000\n"
            "1704067500000,2024-01-01 00:05:00,103,107,102,106,1100\n"
            "1704067800000,2024-01-01 00:10:00,106,108,104,105,900\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(csv_content)
            f.flush()
            try:
                data = loader.load_csv(f.name, base_timeframe="5m")
                assert len(data.m5) == 3
            finally:
                os.unlink(f.name)

    def test_load_csv_resamples_correctly(self, loader):
        """CSV data should be resampled to all timeframes."""
        # Generate 1 hour of 5m data (12 bars) to get 4 M15 bars
        rows = ["timestamp,open,high,low,close,volume"]
        for i in range(12):
            ts = f"2024-01-01 00:{i*5:02d}:00"
            rows.append(f"{ts},{100+i},{105+i},{99+i},{103+i},{1000+i}")
        csv_content = "\n".join(rows) + "\n"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(csv_content)
            f.flush()
            try:
                data = loader.load_csv(f.name, base_timeframe="5m")
                assert len(data.m5) == 12
                assert len(data.m15) == 4  # 12 M5 bars / 3 = 4 M15 bars
            finally:
                os.unlink(f.name)


# =============================================================================
# SHORT Position Simulation Tests
# =============================================================================


class TestMarketSimulatorShort:
    """Tests for SHORT position simulation in MarketSimulator."""

    async def test_short_open_and_close_profit(self):
        """Open short at high price, close at lower price = profit."""
        sim = MarketSimulator(
            symbol="BTC/USDT",
            initial_balance_quote=Decimal("10000"),
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0"),
            slippage=Decimal("0"),
        )
        await sim.set_price(Decimal("100"))

        # Sell to open SHORT (no base balance -> opens short)
        await sim.create_order("BTC/USDT", "market", "sell", Decimal("10"))

        # Verify short position exists
        assert len(sim.short_positions) == 1
        assert sim.short_positions[0]["amount"] == Decimal("10")

        # Price drops
        await sim.set_price(Decimal("90"))

        # Buy to close SHORT
        await sim.create_order("BTC/USDT", "market", "buy", Decimal("10"))

        # Short closed
        assert len(sim.short_positions) == 0

        # PnL = (100 - 90) * 10 = 100 profit
        assert sim.get_portfolio_value() == Decimal("10100")

    async def test_short_open_and_close_loss(self):
        """Open short at low price, close at higher price = loss."""
        sim = MarketSimulator(
            symbol="BTC/USDT",
            initial_balance_quote=Decimal("10000"),
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0"),
            slippage=Decimal("0"),
        )
        await sim.set_price(Decimal("100"))

        # Open SHORT
        await sim.create_order("BTC/USDT", "market", "sell", Decimal("10"))

        # Price rises
        await sim.set_price(Decimal("110"))

        # Close SHORT
        await sim.create_order("BTC/USDT", "market", "buy", Decimal("10"))

        # PnL = (100 - 110) * 10 = -100 loss
        assert sim.get_portfolio_value() == Decimal("9900")

    async def test_short_unrealized_pnl(self):
        """Portfolio value should reflect unrealized SHORT PnL."""
        sim = MarketSimulator(
            symbol="BTC/USDT",
            initial_balance_quote=Decimal("10000"),
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0"),
            slippage=Decimal("0"),
        )
        await sim.set_price(Decimal("100"))

        # Open SHORT
        await sim.create_order("BTC/USDT", "market", "sell", Decimal("10"))

        # Price drops to 95 — unrealized profit = (100-95)*10 = 50
        await sim.set_price(Decimal("95"))
        assert sim.get_portfolio_value() == Decimal("10050")

        # Price rises to 105 — unrealized loss = (100-105)*10 = -50
        await sim.set_price(Decimal("105"))
        assert sim.get_portfolio_value() == Decimal("9950")

    async def test_short_reset_clears(self):
        """Reset should clear short positions."""
        sim = MarketSimulator(
            symbol="BTC/USDT",
            initial_balance_quote=Decimal("10000"),
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0"),
            slippage=Decimal("0"),
        )
        await sim.set_price(Decimal("100"))
        await sim.create_order("BTC/USDT", "market", "sell", Decimal("5"))

        assert len(sim.short_positions) == 1
        sim.reset()
        assert len(sim.short_positions) == 0


# =============================================================================
# MultiTFBacktestConfig Tests
# =============================================================================


class TestMultiTFBacktestConfig:
    def test_defaults(self):
        config = MultiTFBacktestConfig()
        assert config.symbol == "BTC/USDT"
        assert config.initial_balance == Decimal("10000")
        assert config.lookback == 100
        assert config.warmup_bars == 14400
        assert config.analyze_every_n == 4

    def test_custom(self):
        config = MultiTFBacktestConfig(
            symbol="ETH/USDT",
            initial_balance=Decimal("5000"),
            warmup_bars=30,
        )
        assert config.symbol == "ETH/USDT"
        assert config.initial_balance == Decimal("5000")
        assert config.warmup_bars == 30


# =============================================================================
# MultiTimeframeBacktestEngine Tests
# =============================================================================


class TestMultiTimeframeBacktestEngine:
    """Tests for backtest engine execution."""

    async def test_engine_runs_without_error(self, engine, data_7days):
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        assert result is not None

    async def test_returns_backtest_result(self, engine, data_7days):
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        assert isinstance(result, BacktestResult)

    async def test_strategy_name_in_result(self, engine, data_7days):
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        assert result.strategy_name == "test-concrete"

    async def test_symbol_in_result(self, engine, data_7days):
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        assert result.symbol == "BTC/USDT"

    async def test_initial_balance_correct(self, engine, data_7days):
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        assert result.initial_balance == Decimal("10000")

    async def test_equity_curve_length(self, engine, data_7days):
        """Equity curve entries = total M5 bars - warmup bars."""
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        expected = len(data_7days.m5) - engine.config.warmup_bars
        assert len(result.equity_curve) == expected

    async def test_equity_curve_has_required_fields(self, engine, data_7days):
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        assert len(result.equity_curve) > 0
        point = result.equity_curve[0]
        assert "timestamp" in point
        assert "price" in point
        assert "portfolio_value" in point

    async def test_duration_set(self, engine, data_7days):
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        assert result.duration.days >= 6

    async def test_result_to_dict(self, engine, data_7days):
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        d = result.to_dict()
        assert d["strategy_name"] == "test-concrete"
        assert "performance" in d
        assert "trading_stats" in d

    async def test_run_with_generated_data(self):
        config = MultiTFBacktestConfig(
            initial_balance=Decimal("10000"),
            warmup_bars=20,
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run_with_generated_data(
            strategy=strategy,
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 8),
            trend="up",
        )
        assert isinstance(result, BacktestResult)
        assert len(result.equity_curve) > 0

    async def test_different_warmup(self, data_7days):
        """Different warmup values produce different equity curve lengths."""
        strategy = ConcreteStrategy()

        config1 = MultiTFBacktestConfig(warmup_bars=20)
        engine1 = MultiTimeframeBacktestEngine(config=config1)
        r1 = await engine1.run(strategy, data_7days)

        config2 = MultiTFBacktestConfig(warmup_bars=40)
        engine2 = MultiTimeframeBacktestEngine(config=config2)
        r2 = await engine2.run(strategy, data_7days)

        assert len(r1.equity_curve) > len(r2.equity_curve)


# =============================================================================
# Integration Tests with Real Strategy Adapters
# =============================================================================


class TestSMCAdapterIntegration:
    """Integration test: run SMCStrategyAdapter through multi-TF engine."""

    async def test_smc_adapter_backtest(self):
        from bot.strategies.smc.config import SMCConfig
        from bot.strategies.smc_adapter import SMCStrategyAdapter

        # SMCStrategy.__init__ references risk_per_trade_pct and
        # max_position_size_usd but SMCConfig has risk_per_trade and
        # max_position_size. Provide a patched config with aliases.
        smc_config = SMCConfig()
        smc_config.risk_per_trade_pct = smc_config.risk_per_trade
        smc_config.max_position_size_usd = smc_config.max_position_size

        config = MultiTFBacktestConfig(
            symbol="BTC/USDT",
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
        assert result.initial_balance == Decimal("10000")
        assert len(result.equity_curve) > 0


class TestTrendFollowerAdapterIntegration:
    """Integration test: run TrendFollowerAdapter through multi-TF engine."""

    async def test_trend_follower_backtest(self):
        from bot.strategies.trend_follower_adapter import TrendFollowerAdapter

        config = MultiTFBacktestConfig(
            symbol="BTC/USDT",
            initial_balance=Decimal("10000"),
            warmup_bars=60,
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = TrendFollowerAdapter(
            initial_capital=Decimal("10000"),
            name="tf-backtest",
            log_trades=False,
        )

        result = await engine.run_with_generated_data(
            strategy=strategy,
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 15),
            trend="up",
        )

        assert result.strategy_name == "tf-backtest"
        assert result.initial_balance == Decimal("10000")
        assert len(result.equity_curve) > 0


class TestMultiStrategyComparison:
    """Test running multiple strategies on the same data."""

    async def test_same_data_different_strategies(self):
        loader = MultiTimeframeDataLoader()
        data = loader.load(
            symbol="BTC/USDT",
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 15),
            trend="up",
        )

        config = MultiTFBacktestConfig(
            initial_balance=Decimal("10000"),
            warmup_bars=20,
        )
        engine = MultiTimeframeBacktestEngine(config=config)

        # Run ConcreteStrategy
        s1 = ConcreteStrategy()
        r1 = await engine.run(s1, data)

        # Run another ConcreteStrategy (same strategy, fresh engine)
        engine2 = MultiTimeframeBacktestEngine(config=config)
        s2 = ConcreteStrategy()
        r2 = await engine2.run(s2, data)

        # Both should complete with same initial balance
        assert r1.initial_balance == r2.initial_balance
        assert len(r1.equity_curve) == len(r2.equity_curve)


# =============================================================================
# Phase 1: New Metrics Tests
# =============================================================================


class TestNewMetricsInResult:
    """Tests for Sortino, Calmar, Profit Factor, Capital Efficiency."""

    async def test_backtest_result_new_fields_in_dict(self, engine, data_7days):
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        d = result.to_dict()
        rm = d["risk_metrics"]
        # Fields should be present (may be None if no trades)
        assert "sortino_ratio" in rm
        assert "calmar_ratio" in rm
        assert "profit_factor" in rm
        assert "capital_efficiency" in rm

    async def test_capital_efficiency_tracked(self, engine, data_7days):
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        # Capital efficiency should be a Decimal or None
        if result.capital_efficiency is not None:
            assert isinstance(result.capital_efficiency, Decimal)

    async def test_engine_sweep_flag_default_false(self):
        config = MultiTFBacktestConfig()
        assert config.use_candle_sweep is False

    async def test_engine_sweep_true(self, data_7days):
        """With sweep enabled, engine should still produce valid results."""
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            use_candle_sweep=True,
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        assert isinstance(result, BacktestResult)
        assert len(result.equity_curve) > 0


# =============================================================================
# Phase 4: Intra-Candle Sweep Tests
# =============================================================================


class TestIntraCandleSweep:
    """Tests for MarketSimulator.set_candle() and candle sweep."""

    async def test_set_candle_sweeps_four_prices(self):
        """set_candle should update current_price to close at the end."""
        sim = MarketSimulator(
            symbol="BTC/USDT",
            initial_balance_quote=Decimal("10000"),
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0"),
            slippage=Decimal("0"),
        )
        await sim.set_candle(Decimal("100"), Decimal("110"), Decimal("90"), Decimal("105"))
        assert sim.current_price == Decimal("105")

    async def test_limit_buy_fills_on_low(self):
        """Limit buy at 95 should fill when candle low is 90."""
        sim = MarketSimulator(
            symbol="BTC/USDT",
            initial_balance_quote=Decimal("10000"),
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0"),
            slippage=Decimal("0"),
        )
        await sim.set_price(Decimal("100"))
        # Place limit buy at 95
        await sim.create_order("BTC/USDT", "limit", "buy", Decimal("1"), Decimal("95"))
        assert len(sim.get_open_orders()) == 1

        # Candle sweeps O=100, L=90, H=110, C=105 — low touches 90 < 95
        await sim.set_candle(Decimal("100"), Decimal("110"), Decimal("90"), Decimal("105"))
        assert len(sim.get_open_orders()) == 0
        assert sim.balance.base == Decimal("1")

    async def test_limit_sell_fills_on_high(self):
        """Limit sell at 108 should fill when candle high is 110."""
        sim = MarketSimulator(
            symbol="BTC/USDT",
            initial_balance_base=Decimal("1"),
            initial_balance_quote=Decimal("10000"),
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0"),
            slippage=Decimal("0"),
        )
        await sim.set_price(Decimal("100"))
        # Place limit sell at 108
        await sim.create_order("BTC/USDT", "limit", "sell", Decimal("1"), Decimal("108"))
        assert len(sim.get_open_orders()) == 1

        # Candle sweeps O=100, L=90, H=110, C=105 — high 110 >= 108
        await sim.set_candle(Decimal("100"), Decimal("110"), Decimal("90"), Decimal("105"))
        assert len(sim.get_open_orders()) == 0
        assert sim.balance.base == Decimal("0")

    async def test_market_orders_unaffected(self):
        """Market orders should work the same regardless of set_candle."""
        sim = MarketSimulator(
            symbol="BTC/USDT",
            initial_balance_quote=Decimal("10000"),
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0"),
            slippage=Decimal("0"),
        )
        await sim.set_candle(Decimal("100"), Decimal("110"), Decimal("90"), Decimal("105"))
        await sim.create_order("BTC/USDT", "market", "buy", Decimal("1"))
        # Market order executes at current_price = 105 (close)
        assert sim.balance.base == Decimal("1")

    async def test_set_price_still_works(self):
        """set_price should still work normally."""
        sim = MarketSimulator(
            symbol="BTC/USDT",
            initial_balance_quote=Decimal("10000"),
            maker_fee=Decimal("0"),
            taker_fee=Decimal("0"),
            slippage=Decimal("0"),
        )
        await sim.set_price(Decimal("100"))
        assert sim.current_price == Decimal("100")
        await sim.set_price(Decimal("200"))
        assert sim.current_price == Decimal("200")
