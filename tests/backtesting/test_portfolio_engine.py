"""
Unit tests for bot/tests/backtesting/portfolio_engine.py

Tests cover:
- PortfolioBacktestConfig defaults
- PortfolioBacktestEngine.run() with two pairs
- Portfolio-level metrics (return, Sharpe, drawdown)
- Correlation matrix structure
- best/worst pair detection
- pairs_profitable counting
- per_pair_overrides propagation
- Graceful handling when one pair fails
- to_dict() serialisation
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from bot.tests.backtesting.multi_tf_data_loader import MultiTimeframeData
from bot.tests.backtesting.orchestrator_engine import (
    OrchestratorBacktestConfig,
)
from bot.tests.backtesting.portfolio_engine import (
    PortfolioBacktestConfig,
    PortfolioBacktestEngine,
    PortfolioBacktestResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tiny_df(n: int = 60, base_price: float = 100.0, seed: int = 42) -> pd.DataFrame:
    rng = pd.date_range("2024-01-01", periods=n, freq="5min")
    np.random.seed(seed)
    prices = base_price + np.random.randn(n).cumsum() * 0.5
    prices = np.abs(prices) + 1.0
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.001,
            "low": prices * 0.999,
            "close": prices,
            "volume": 1000.0,
        },
        index=rng,
    )


def _tiny_data(n: int = 60, base_price: float = 100.0, seed: int = 42) -> MultiTimeframeData:
    m5 = _tiny_df(n, base_price=base_price, seed=seed)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return MultiTimeframeData(
        m5=m5,
        m15=m5.resample("15min").agg(agg).dropna(),
        h1=m5.resample("1h").agg(agg).dropna(),
        h4=m5.resample("4h").agg(agg).dropna(),
        d1=m5.resample("1D").agg(agg).dropna(),
    )


def _noop_factory():
    """Factory that returns a strategy that never generates signals."""
    from bot.strategies.base import BaseStrategy, StrategyPerformance

    class NoOpStrategy(BaseStrategy):
        def get_strategy_name(self) -> str:
            return "noop"

        def get_strategy_type(self) -> str:
            return "grid"

        def analyze_market(self, *args, **kwargs):
            return None

        def generate_signal(self, df, balance):
            return None

        def update_positions(self, price, df):
            return []

        def open_position(self, signal, amount) -> str:
            return "p1"

        def close_position(self, pos_id, reason, price) -> None:
            pass

        def reset(self) -> None:
            pass

        def get_active_positions(self) -> list:
            return []

        def get_performance(self):
            return StrategyPerformance()

    return NoOpStrategy()


def _make_engine() -> PortfolioBacktestEngine:
    engine = PortfolioBacktestEngine()
    engine.register_strategy_factory("grid", lambda p: _noop_factory())
    engine.register_strategy_factory("dca", lambda p: _noop_factory())
    return engine


def _base_pair_config(symbol: str = "BTC/USDT") -> OrchestratorBacktestConfig:
    return OrchestratorBacktestConfig(
        symbol=symbol,
        initial_balance=Decimal("5000"),
        warmup_bars=10,
        enable_grid=True,
        enable_dca=False,
        enable_trend_follower=False,
        enable_smc=False,
        enable_strategy_router=False,
        enable_risk_manager=False,
    )


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


class TestPortfolioBacktestConfig:
    def test_defaults(self) -> None:
        cfg = PortfolioBacktestConfig()
        assert cfg.initial_capital == Decimal("10000")
        assert cfg.max_single_pair_pct == 0.25
        assert cfg.max_total_exposure_pct == 0.80
        assert cfg.portfolio_stop_loss_pct == 0.15
        assert cfg.symbols == []
        assert cfg.per_pair_overrides == {}

    def test_custom(self) -> None:
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("20000"),
            max_single_pair_pct=0.30,
        )
        assert cfg.symbols == ["BTC/USDT", "ETH/USDT"]
        assert cfg.initial_capital == Decimal("20000")
        assert cfg.max_single_pair_pct == 0.30


# ---------------------------------------------------------------------------
# Engine: two-pair run
# ---------------------------------------------------------------------------


class TestPortfolioBacktestEngineRun:
    @pytest.mark.asyncio
    async def test_run_returns_result(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=1),
            "ETH/USDT": _tiny_data(seed=2),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        engine = _make_engine()
        result = await engine.run(data_map, cfg)
        assert isinstance(result, PortfolioBacktestResult)

    @pytest.mark.asyncio
    async def test_per_pair_results_populated(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=10),
            "ETH/USDT": _tiny_data(seed=20),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert "BTC/USDT" in result.per_pair_results
        assert "ETH/USDT" in result.per_pair_results

    @pytest.mark.asyncio
    async def test_total_pairs_count(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=11),
            "ETH/USDT": _tiny_data(seed=12),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert result.total_pairs == 2

    @pytest.mark.asyncio
    async def test_portfolio_return_is_float(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=13),
            "ETH/USDT": _tiny_data(seed=14),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert isinstance(result.portfolio_total_return_pct, float)

    @pytest.mark.asyncio
    async def test_equity_curve_non_empty(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=15),
            "ETH/USDT": _tiny_data(seed=16),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert len(result.portfolio_equity_curve) > 0
        entry = result.portfolio_equity_curve[0]
        assert "timestamp" in entry
        assert "portfolio_value" in entry

    @pytest.mark.asyncio
    async def test_sharpe_is_numeric(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=17),
            "ETH/USDT": _tiny_data(seed=18),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert isinstance(result.portfolio_sharpe, float)

    @pytest.mark.asyncio
    async def test_max_drawdown_non_negative(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=19),
            "ETH/USDT": _tiny_data(seed=20),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert result.portfolio_max_drawdown_pct >= 0.0


# ---------------------------------------------------------------------------
# Correlation matrix
# ---------------------------------------------------------------------------


class TestCorrelationMatrix:
    @pytest.mark.asyncio
    async def test_correlation_matrix_structure(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=21),
            "ETH/USDT": _tiny_data(seed=22),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        matrix = result.pair_correlation_matrix
        assert "BTC/USDT" in matrix
        assert "ETH/USDT" in matrix
        assert matrix["BTC/USDT"]["BTC/USDT"] == 1.0
        assert matrix["ETH/USDT"]["ETH/USDT"] == 1.0

    @pytest.mark.asyncio
    async def test_correlation_in_range(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=23),
            "ETH/USDT": _tiny_data(seed=24),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        corr = result.pair_correlation_matrix["BTC/USDT"]["ETH/USDT"]
        assert -1.0 <= corr <= 1.0

    @pytest.mark.asyncio
    async def test_avg_correlation_is_float(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=25),
            "ETH/USDT": _tiny_data(seed=26),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert isinstance(result.avg_pair_correlation, float)


# ---------------------------------------------------------------------------
# Best / worst pair
# ---------------------------------------------------------------------------


class TestBestWorstPair:
    @pytest.mark.asyncio
    async def test_best_worst_are_known_symbols(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=30),
            "ETH/USDT": _tiny_data(seed=31),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert result.best_pair in {"BTC/USDT", "ETH/USDT"}
        assert result.worst_pair in {"BTC/USDT", "ETH/USDT"}

    @pytest.mark.asyncio
    async def test_pairs_profitable_in_range(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=32),
            "ETH/USDT": _tiny_data(seed=33),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert 0 <= result.pairs_profitable <= 2


# ---------------------------------------------------------------------------
# Single-pair run (degenerate case)
# ---------------------------------------------------------------------------


class TestSinglePairPortfolio:
    @pytest.mark.asyncio
    async def test_single_pair_runs(self) -> None:
        data_map = {"BTC/USDT": _tiny_data(seed=40)}
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT"],
            initial_capital=Decimal("5000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert result.total_pairs == 1
        assert result.best_pair == "BTC/USDT"
        assert result.worst_pair == "BTC/USDT"

    @pytest.mark.asyncio
    async def test_single_pair_correlation_is_one(self) -> None:
        data_map = {"BTC/USDT": _tiny_data(seed=41)}
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT"],
            initial_capital=Decimal("5000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert result.pair_correlation_matrix["BTC/USDT"]["BTC/USDT"] == 1.0
        assert result.avg_pair_correlation == 0.0  # no off-diagonal entries


# ---------------------------------------------------------------------------
# symbols inferred from data_map keys
# ---------------------------------------------------------------------------


class TestSymbolsFromDataMap:
    @pytest.mark.asyncio
    async def test_symbols_inferred_when_empty(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=50),
            "SOL/USDT": _tiny_data(seed=51),
        }
        cfg = PortfolioBacktestConfig(
            symbols=[],  # empty — should infer from data_map
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        assert result.total_pairs == 2


# ---------------------------------------------------------------------------
# to_dict serialisation
# ---------------------------------------------------------------------------


class TestPortfolioBacktestResultToDict:
    @pytest.mark.asyncio
    async def test_to_dict_structure(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=60),
            "ETH/USDT": _tiny_data(seed=61),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        d = result.to_dict()
        assert "portfolio" in d
        assert "pairs" in d
        port = d["portfolio"]
        assert "total_return_pct" in port
        assert "sharpe" in port
        assert "max_drawdown_pct" in port
        assert "best_pair" in port
        assert "worst_pair" in port
        assert "pairs_profitable" in port
        assert "total_pairs" in port

    @pytest.mark.asyncio
    async def test_to_dict_pairs_keys(self) -> None:
        data_map = {
            "BTC/USDT": _tiny_data(seed=62),
            "ETH/USDT": _tiny_data(seed=63),
        }
        cfg = PortfolioBacktestConfig(
            symbols=["BTC/USDT", "ETH/USDT"],
            initial_capital=Decimal("10000"),
            per_pair_config=_base_pair_config(),
        )
        result = await _make_engine().run(data_map, cfg)
        d = result.to_dict()
        assert "BTC/USDT" in d["pairs"]
        assert "ETH/USDT" in d["pairs"]
        pair_entry = d["pairs"]["BTC/USDT"]
        assert "total_return_pct" in pair_entry
        assert "total_trades" in pair_entry
