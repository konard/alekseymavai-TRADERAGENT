"""
Unit tests for bot/tests/backtesting/orchestrator_engine.py

Tests cover:
- OrchestratorBacktestConfig defaults
- BacktestOrchestratorEngine with no factories (graceful error)
- BacktestOrchestratorEngine with a minimal mock strategy
- OrchestratorBacktestResult structure
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest

from bot.tests.backtesting.orchestrator_engine import (
    BacktestOrchestratorEngine,
    OrchestratorBacktestConfig,
    OrchestratorBacktestResult,
)
from bot.tests.backtesting.multi_tf_data_loader import MultiTimeframeData


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tiny_df(n: int = 50, base_price: float = 100.0) -> pd.DataFrame:
    rng = pd.date_range("2024-01-01", periods=n, freq="5min")
    np.random.seed(42)
    prices = base_price + np.random.randn(n).cumsum() * 0.5
    prices = np.abs(prices) + 1
    highs = prices * 1.001
    lows = prices * 0.999
    return pd.DataFrame(
        {"open": prices, "high": highs, "low": lows, "close": prices, "volume": 1000.0},
        index=rng,
    )


def _tiny_data(n: int = 200) -> MultiTimeframeData:
    m5 = _tiny_df(n)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return MultiTimeframeData(
        m5=m5,
        m15=m5.resample("15min").agg(agg).dropna(),
        h1=m5.resample("1h").agg(agg).dropna(),
        h4=m5.resample("4h").agg(agg).dropna(),
        d1=m5.resample("1D").agg(agg).dropna(),
    )


def _make_noop_strategy():
    """Return a strategy instance that never generates signals."""
    from bot.strategies.base import BaseStrategy, PositionInfo, StrategyPerformance

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
            return "pos_1"
        def close_position(self, pos_id, reason, price) -> None:
            pass
        def reset(self) -> None:
            pass
        def get_active_positions(self) -> list:
            return []
        def get_performance(self):
            return StrategyPerformance()

    return NoOpStrategy()


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestOrchestratorBacktestConfig:
    def test_defaults(self) -> None:
        cfg = OrchestratorBacktestConfig()
        assert cfg.symbol == "BTC/USDT"
        assert cfg.initial_balance == Decimal("10000")
        assert cfg.enable_grid is True
        assert cfg.enable_dca is True
        assert cfg.enable_trend_follower is True
        assert cfg.enable_smc is True   # P0.4: SMC enabled by default (mirrors live bot)
        assert cfg.enable_strategy_router is True
        assert cfg.router_cooldown_bars == 120  # P0.1: 600s / 5min bars = 120 (live parity)

    def test_custom_params(self) -> None:
        cfg = OrchestratorBacktestConfig(
            symbol="ETH/USDT",
            initial_balance=Decimal("5000"),
            enable_smc=True,
            router_cooldown_bars=30,
        )
        assert cfg.symbol == "ETH/USDT"
        assert cfg.initial_balance == Decimal("5000")
        assert cfg.enable_smc is True
        assert cfg.router_cooldown_bars == 30


# ---------------------------------------------------------------------------
# Engine tests
# ---------------------------------------------------------------------------

class TestBacktestOrchestratorEngine:
    def test_no_factories_raises(self) -> None:
        engine = BacktestOrchestratorEngine()
        data = _tiny_data(100)
        cfg = OrchestratorBacktestConfig(warmup_bars=10)
        with pytest.raises(ValueError, match="No strategies"):
            asyncio.run(engine.run(data, cfg))

    def test_register_factory(self) -> None:
        engine = BacktestOrchestratorEngine()
        engine.register_strategy_factory("grid", lambda p: _make_noop_strategy())
        assert "grid" in engine._strategy_factories

    @pytest.mark.asyncio
    async def test_run_with_mock_strategy(self) -> None:
        """Run the engine with a minimal mock strategy that never generates signals."""
        engine = BacktestOrchestratorEngine()
        # Factory ignores params and returns a no-op strategy
        engine.register_strategy_factory("grid", lambda params: _make_noop_strategy())

        data = _tiny_data(100)
        cfg = OrchestratorBacktestConfig(
            symbol="BTC/USDT",
            initial_balance=Decimal("10000"),
            warmup_bars=10,
            enable_dca=False,
            enable_trend_follower=False,
            enable_strategy_router=False,
            enable_risk_manager=False,
        )

        result = await engine.run(data, cfg)

        assert isinstance(result, OrchestratorBacktestResult)
        assert result.initial_balance == Decimal("10000")
        assert result.strategy_name == "orchestrator_v2"
        assert result.symbol == "BTC/USDT"
        assert result.total_trades == 0
        assert result.final_balance == Decimal("10000")

    @pytest.mark.asyncio
    async def test_result_has_router_fields(self) -> None:
        """Verify OrchestratorBacktestResult has V2.0 extension fields."""
        engine = BacktestOrchestratorEngine()
        engine.register_strategy_factory("grid", lambda p: _make_noop_strategy())

        data = _tiny_data(50)
        cfg = OrchestratorBacktestConfig(
            warmup_bars=5,
            enable_dca=False,
            enable_trend_follower=False,
            enable_strategy_router=True,
            enable_risk_manager=False,
        )

        result = await engine.run(data, cfg)
        assert hasattr(result, "strategy_switches")
        assert hasattr(result, "per_strategy_pnl")
        assert hasattr(result, "regime_routing_stats")
        assert hasattr(result, "cooldown_events")
        assert isinstance(result.strategy_switches, list)
        assert isinstance(result.per_strategy_pnl, dict)

    @pytest.mark.asyncio
    async def test_to_dict_includes_orchestrator_section(self) -> None:
        """to_dict() should include the orchestrator extension."""
        engine = BacktestOrchestratorEngine()
        engine.register_strategy_factory("grid", lambda p: _make_noop_strategy())

        data = _tiny_data(50)
        cfg = OrchestratorBacktestConfig(
            warmup_bars=5,
            enable_dca=False,
            enable_trend_follower=False,
            enable_risk_manager=False,
        )

        result = await engine.run(data, cfg)
        d = result.to_dict()
        assert "orchestrator" in d
        assert "strategy_switches" in d["orchestrator"]
        assert "per_strategy_pnl" in d["orchestrator"]

    @pytest.mark.asyncio
    async def test_equity_curve_populated(self) -> None:
        """Equity curve should have one entry per active bar."""
        engine = BacktestOrchestratorEngine()
        engine.register_strategy_factory("grid", lambda p: _make_noop_strategy())

        n_bars = 60
        warmup = 10
        data = _tiny_data(n_bars)
        cfg = OrchestratorBacktestConfig(
            warmup_bars=warmup,
            enable_dca=False,
            enable_trend_follower=False,
            enable_strategy_router=False,
            enable_risk_manager=False,
        )

        result = await engine.run(data, cfg)
        # Equity curve should have n_bars - warmup entries
        expected = n_bars - warmup
        assert len(result.equity_curve) == expected

    @pytest.mark.asyncio
    async def test_per_strategy_pnl_keys(self) -> None:
        """per_strategy_pnl should have an entry for each enabled strategy."""
        engine = BacktestOrchestratorEngine()
        engine.register_strategy_factory("grid", lambda p: _make_noop_strategy())

        data = _tiny_data(50)
        cfg = OrchestratorBacktestConfig(
            warmup_bars=5,
            enable_dca=False,
            enable_trend_follower=False,
            enable_smc=False,
            enable_strategy_router=False,
            enable_risk_manager=False,
        )

        result = await engine.run(data, cfg)
        assert "grid" in result.per_strategy_pnl
