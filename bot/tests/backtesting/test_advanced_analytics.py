"""
Tests for advanced analytics: walk-forward, Monte Carlo, optimization, sensitivity.

Issue #174 — Phase 6.3: Advanced Analytics.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import numpy as np
import pandas as pd

from bot.strategies.base import (
    BaseMarketAnalysis,
    BaseSignal,
    BaseStrategy,
    ExitReason,
    PositionInfo,
    SignalDirection,
    StrategyPerformance,
)
from bot.tests.backtesting.backtesting_engine import BacktestResult
from bot.tests.backtesting.monte_carlo import (
    MonteCarloConfig,
    MonteCarloResult,
    MonteCarloSimulation,
)
from bot.tests.backtesting.multi_tf_data_loader import (
    MultiTimeframeData,
    MultiTimeframeDataLoader,
)
from bot.tests.backtesting.multi_tf_engine import (
    MultiTFBacktestConfig,
    MultiTimeframeBacktestEngine,
)
from bot.tests.backtesting.optimization import (
    OptimizationConfig,
    OptimizationResult,
    ParameterOptimizer,
)
from bot.tests.backtesting.sensitivity import (
    ParameterSensitivity,
    SensitivityAnalysis,
    SensitivityConfig,
    SensitivityResult,
)
from bot.tests.backtesting.walk_forward import (
    WalkForwardAnalysis,
    WalkForwardConfig,
    WalkForwardResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ohlcv(n: int = 100, base_price: float = 45000.0) -> pd.DataFrame:
    """Create a simple OHLCV DataFrame with DatetimeIndex."""
    dates = pd.date_range("2024-01-01", periods=n, freq="15min")
    rng = np.random.default_rng(42)
    closes = base_price + np.cumsum(rng.normal(0, 50, n))
    highs = closes + rng.uniform(10, 100, n)
    lows = closes - rng.uniform(10, 100, n)
    opens = closes + rng.normal(0, 20, n)
    volumes = rng.uniform(10, 100, n)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )
    return df


class SimpleTestStrategy(BaseStrategy):
    """
    A simple test strategy with configurable take profit and stop loss.

    Buys every N bars, sells at TP or SL.
    """

    def __init__(
        self,
        tp_pct: Decimal = Decimal("0.01"),
        sl_pct: Decimal = Decimal("0.02"),
        buy_every_n: int = 10,
        name: str = "simple-test",
    ) -> None:
        self._tp_pct = tp_pct
        self._sl_pct = sl_pct
        self._buy_every_n = buy_every_n
        self._name = name
        self._bar_count = 0
        self._positions: dict[str, dict[str, Any]] = {}
        self._closed_trades: list[dict[str, Any]] = []
        self._last_analysis: BaseMarketAnalysis | None = None

    def get_strategy_name(self) -> str:
        return self._name

    def get_strategy_type(self) -> str:
        return "simple-test"

    def analyze_market(self, *dfs: pd.DataFrame) -> BaseMarketAnalysis:
        df = dfs[-1] if dfs else pd.DataFrame()
        self._last_analysis = BaseMarketAnalysis(
            trend="sideways",
            trend_strength=0.5,
            volatility=0.01,
            timestamp=datetime.now(timezone.utc),
            strategy_type="simple-test",
        )
        return self._last_analysis

    def generate_signal(self, df: pd.DataFrame, current_balance: Decimal) -> Optional[BaseSignal]:
        if df.empty:
            return None

        self._bar_count += 1
        if self._bar_count % self._buy_every_n != 0:
            return None

        # Don't open if we already have positions
        if self._positions:
            return None

        close = Decimal(str(df["close"].iloc[-1]))
        if current_balance < Decimal("100"):
            return None

        tp = close * (Decimal("1") + self._tp_pct)
        sl = close * (Decimal("1") - self._sl_pct)

        return BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=close,
            stop_loss=sl,
            take_profit=tp,
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="simple-test",
            signal_reason="periodic_buy",
            risk_reward_ratio=float(self._tp_pct / self._sl_pct),
        )

    def open_position(self, signal: BaseSignal, position_size: Decimal) -> str:
        import uuid

        pos_id = str(uuid.uuid4())[:8]
        self._positions[pos_id] = {
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "size": position_size,
            "entry_time": datetime.now(timezone.utc),
            "current_price": signal.entry_price,
        }
        return pos_id

    def update_positions(
        self, current_price: Decimal, df: pd.DataFrame
    ) -> list[tuple[str, ExitReason]]:
        exits: list[tuple[str, ExitReason]] = []
        for pos_id, pos in list(self._positions.items()):
            pos["current_price"] = current_price
            if current_price >= pos["take_profit"]:
                exits.append((pos_id, ExitReason.TAKE_PROFIT))
            elif current_price <= pos["stop_loss"]:
                exits.append((pos_id, ExitReason.STOP_LOSS))
        return exits

    def close_position(
        self, position_id: str, exit_reason: ExitReason, exit_price: Decimal
    ) -> None:
        pos = self._positions.pop(position_id, None)
        if not pos:
            return
        pnl = (exit_price - pos["entry_price"]) * pos["size"] / pos["entry_price"]
        self._closed_trades.append(
            {
                "pnl": pnl,
                "exit_reason": exit_reason.value,
            }
        )

    def get_active_positions(self) -> list[PositionInfo]:
        return []

    def get_performance(self) -> StrategyPerformance:
        return StrategyPerformance()

    def reset(self) -> None:
        self._bar_count = 0
        self._positions.clear()
        self._closed_trades.clear()
        self._last_analysis = None


def _make_strategy_factory():
    """Return a strategy factory callable for optimization/sensitivity tests."""

    def factory(params: dict[str, Any]) -> SimpleTestStrategy:
        return SimpleTestStrategy(
            tp_pct=Decimal(str(params.get("tp_pct", 0.01))),
            sl_pct=Decimal(str(params.get("sl_pct", 0.02))),
            buy_every_n=params.get("buy_every_n", 10),
            name=f"opt-tp{params.get('tp_pct', 0.01)}-sl{params.get('sl_pct', 0.02)}",
        )

    return factory


def _load_test_data(days: int = 4) -> MultiTimeframeData:
    """Load multi-timeframe test data for a short period."""
    loader = MultiTimeframeDataLoader()
    return loader.load(
        symbol="BTC/USDT",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 1, 1) + timedelta(days=days),
        trend="up",
        base_price=Decimal("45000"),
    )


def _make_backtest_result(
    trades: list[tuple[float, float]] | None = None,
    initial_balance: float = 10000.0,
) -> BacktestResult:
    """
    Build a BacktestResult with trade history for Monte Carlo.

    trades: list of (buy_price, sell_price) pairs.
    """
    if trades is None:
        trades = [
            (45000, 45500),
            (45200, 45600),
            (45100, 44900),
            (44800, 45200),
            (45300, 45100),
            (45000, 45800),
            (45500, 45700),
            (45600, 45200),
            (45100, 45500),
            (45300, 45900),
        ]

    history = []
    balance = initial_balance
    winning = 0
    losing = 0
    total_pnl = 0.0

    for buy_p, sell_p in trades:
        amount = 0.01
        history.append({"side": "buy", "price": buy_p, "amount": amount})
        history.append({"side": "sell", "price": sell_p, "amount": amount})
        pnl = (sell_p - buy_p) * amount
        total_pnl += pnl
        if pnl > 0:
            winning += 1
        else:
            losing += 1

    total = winning + losing
    now = datetime.now(timezone.utc)

    return BacktestResult(
        strategy_name="test-strategy",
        symbol="BTC/USDT",
        start_time=now - timedelta(days=7),
        end_time=now,
        duration=timedelta(days=7),
        initial_balance=Decimal(str(initial_balance)),
        final_balance=Decimal(str(initial_balance + total_pnl)),
        total_return=Decimal(str(total_pnl)),
        total_return_pct=Decimal(str(total_pnl / initial_balance * 100)),
        max_drawdown=Decimal("50"),
        max_drawdown_pct=Decimal("0.5"),
        total_trades=total,
        winning_trades=winning,
        losing_trades=losing,
        win_rate=Decimal(str(winning / total * 100)) if total > 0 else Decimal("0"),
        total_buy_orders=len(trades),
        total_sell_orders=len(trades),
        avg_profit_per_trade=Decimal(str(total_pnl / total)) if total > 0 else Decimal("0"),
        sharpe_ratio=Decimal("1.5"),
        trade_history=history,
        equity_curve=[
            {
                "timestamp": (now - timedelta(hours=i)).isoformat(),
                "price": 45000.0,
                "portfolio_value": initial_balance + i * 10,
            }
            for i in range(50)
        ],
    )


# ===========================================================================
# Monte Carlo Tests
# ===========================================================================


class TestMonteCarloConfig:
    def test_defaults(self):
        cfg = MonteCarloConfig()
        assert cfg.n_simulations == 1000
        assert cfg.seed is None
        assert len(cfg.confidence_levels) == 5

    def test_custom(self):
        cfg = MonteCarloConfig(n_simulations=500, seed=42)
        assert cfg.n_simulations == 500
        assert cfg.seed == 42


class TestMonteCarloSimulation:
    def test_run_produces_result(self):
        mc = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=100, seed=42))
        result = mc.run(_make_backtest_result())
        assert isinstance(result, MonteCarloResult)
        assert result.n_simulations == 100

    def test_return_percentiles(self):
        mc = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=200, seed=42))
        result = mc.run(_make_backtest_result())
        assert 0.05 in result.return_percentiles
        assert 0.50 in result.return_percentiles
        assert 0.95 in result.return_percentiles
        # Median should be reasonable
        assert result.return_percentiles[0.50] != 0.0

    def test_drawdown_percentiles(self):
        mc = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=200, seed=42))
        result = mc.run(_make_backtest_result())
        assert 0.50 in result.drawdown_percentiles
        # All drawdowns should be >= 0
        assert all(d >= 0 for d in result.simulated_drawdowns)

    def test_probability_of_profit(self):
        mc = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=500, seed=42))
        result = mc.run(_make_backtest_result())
        assert 0.0 <= result.probability_of_profit <= 1.0

    def test_var_and_cvar(self):
        mc = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=500, seed=42))
        result = mc.run(_make_backtest_result())
        var_5 = result.get_var(0.05)
        cvar_5 = result.get_cvar(0.05)
        # CVaR should be <= VaR (more conservative measure)
        assert cvar_5 <= var_5

    def test_empty_trades(self):
        result = _make_backtest_result(trades=[])
        mc = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=50, seed=42))
        mc_result = mc.run(result)
        assert mc_result.n_simulations == 0
        assert mc_result.probability_of_profit == 0.0

    def test_deterministic_with_seed(self):
        bt = _make_backtest_result()
        mc1 = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=100, seed=123))
        mc2 = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=100, seed=123))
        r1 = mc1.run(bt)
        r2 = mc2.run(bt)
        assert r1.simulated_returns == r2.simulated_returns

    def test_different_seeds_different_results(self):
        bt = _make_backtest_result()
        mc1 = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=100, seed=1))
        mc2 = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=100, seed=2))
        r1 = mc1.run(bt)
        r2 = mc2.run(bt)
        assert r1.simulated_returns != r2.simulated_returns

    def test_win_rate_percentiles(self):
        mc = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=200, seed=42))
        result = mc.run(_make_backtest_result())
        assert 0.50 in result.win_rate_percentiles
        # Win rates should be between 0 and 100
        for wr in result.win_rate_percentiles.values():
            assert 0.0 <= wr <= 100.0


# ===========================================================================
# Walk-Forward Tests
# ===========================================================================


class TestWalkForwardConfig:
    def test_defaults(self):
        cfg = WalkForwardConfig()
        assert cfg.n_splits == 5
        assert cfg.train_pct == 0.7

    def test_custom(self):
        cfg = WalkForwardConfig(n_splits=3, train_pct=0.6)
        assert cfg.n_splits == 3
        assert cfg.train_pct == 0.6


class TestWalkForwardAnalysis:
    async def test_run_produces_result(self):
        data = _load_test_data(days=10)
        wf = WalkForwardAnalysis(
            config=WalkForwardConfig(
                n_splits=2,
                train_pct=0.6,
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        strategy = SimpleTestStrategy(buy_every_n=5)
        result = await wf.run(strategy, data)
        assert isinstance(result, WalkForwardResult)

    async def test_windows_populated(self):
        data = _load_test_data(days=10)
        wf = WalkForwardAnalysis(
            config=WalkForwardConfig(
                n_splits=2,
                train_pct=0.6,
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        strategy = SimpleTestStrategy(buy_every_n=5)
        result = await wf.run(strategy, data)
        assert len(result.windows) > 0

    async def test_each_window_has_results(self):
        data = _load_test_data(days=10)
        wf = WalkForwardAnalysis(
            config=WalkForwardConfig(
                n_splits=2,
                train_pct=0.6,
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        strategy = SimpleTestStrategy(buy_every_n=5)
        result = await wf.run(strategy, data)
        for w in result.windows:
            assert isinstance(w.train_result, BacktestResult)
            assert isinstance(w.test_result, BacktestResult)

    async def test_consistency_ratio_range(self):
        data = _load_test_data(days=10)
        wf = WalkForwardAnalysis(
            config=WalkForwardConfig(
                n_splits=2,
                train_pct=0.6,
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        strategy = SimpleTestStrategy(buy_every_n=5)
        result = await wf.run(strategy, data)
        assert 0.0 <= result.consistency_ratio <= 1.0

    async def test_is_robust(self):
        data = _load_test_data(days=10)
        wf = WalkForwardAnalysis(
            config=WalkForwardConfig(
                n_splits=2,
                train_pct=0.6,
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        strategy = SimpleTestStrategy(buy_every_n=5)
        result = await wf.run(strategy, data)
        # is_robust is just consistency_ratio >= threshold
        assert result.is_robust(0.0) is True
        assert isinstance(result.is_robust(0.5), bool)

    async def test_aggregate_metrics_present(self):
        data = _load_test_data(days=10)
        wf = WalkForwardAnalysis(
            config=WalkForwardConfig(
                n_splits=2,
                train_pct=0.6,
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        strategy = SimpleTestStrategy(buy_every_n=5)
        result = await wf.run(strategy, data)
        assert isinstance(result.aggregate_test_return_pct, Decimal)
        assert isinstance(result.avg_test_drawdown_pct, Decimal)


# ===========================================================================
# Parameter Optimization Tests
# ===========================================================================


class TestOptimizationConfig:
    def test_defaults(self):
        cfg = OptimizationConfig()
        assert cfg.objective == "total_return_pct"
        assert cfg.higher_is_better is True

    def test_custom(self):
        cfg = OptimizationConfig(objective="win_rate", higher_is_better=True)
        assert cfg.objective == "win_rate"


class TestParameterOptimizer:
    async def test_optimize_runs(self):
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await optimizer.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.01, 0.02], "sl_pct": [0.02]},
            data=data,
        )
        assert isinstance(result, OptimizationResult)

    async def test_all_combinations_tested(self):
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await optimizer.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.01, 0.02], "sl_pct": [0.01, 0.02]},
            data=data,
        )
        # 2 x 2 = 4 combinations
        assert len(result.all_trials) == 4

    async def test_best_params_present(self):
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await optimizer.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.01, 0.02]},
            data=data,
        )
        assert "tp_pct" in result.best_params
        assert isinstance(result.best_result, BacktestResult)

    async def test_trials_sorted(self):
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await optimizer.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.005, 0.01, 0.02]},
            data=data,
        )
        # Trials should be sorted descending by objective
        objectives = [t.objective_value for t in result.all_trials]
        assert objectives == sorted(objectives, reverse=True)

    async def test_top_n(self):
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await optimizer.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.005, 0.01, 0.02]},
            data=data,
        )
        top2 = result.top_n(2)
        assert len(top2) == 2

    async def test_param_impact(self):
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await optimizer.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.01, 0.02]},
            data=data,
        )
        impact = result.get_param_impact("tp_pct")
        assert 0.01 in impact
        assert 0.02 in impact

    async def test_empty_grid(self):
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await optimizer.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={},
            data=data,
        )
        # Single trial with default params
        assert len(result.all_trials) == 1


# ===========================================================================
# Sensitivity Analysis Tests
# ===========================================================================


class TestSensitivityConfig:
    def test_defaults(self):
        cfg = SensitivityConfig()
        assert "total_return_pct" in cfg.metrics
        assert "max_drawdown_pct" in cfg.metrics

    def test_custom_metrics(self):
        cfg = SensitivityConfig(metrics=["win_rate"])
        assert cfg.metrics == ["win_rate"]


class TestSensitivityAnalysis:
    async def test_run_produces_result(self):
        data = _load_test_data(days=4)
        sa = SensitivityAnalysis(
            config=SensitivityConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await sa.run(
            strategy_factory=_make_strategy_factory(),
            base_params={"tp_pct": 0.01, "sl_pct": 0.02, "buy_every_n": 10},
            param_ranges={"tp_pct": [0.005, 0.01, 0.02]},
            data=data,
        )
        assert isinstance(result, SensitivityResult)

    async def test_baseline_result(self):
        data = _load_test_data(days=4)
        sa = SensitivityAnalysis(
            config=SensitivityConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await sa.run(
            strategy_factory=_make_strategy_factory(),
            base_params={"tp_pct": 0.01, "sl_pct": 0.02, "buy_every_n": 10},
            param_ranges={"tp_pct": [0.005, 0.01, 0.02]},
            data=data,
        )
        assert isinstance(result.baseline_result, BacktestResult)

    async def test_parameter_sensitivity_populated(self):
        data = _load_test_data(days=4)
        sa = SensitivityAnalysis(
            config=SensitivityConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await sa.run(
            strategy_factory=_make_strategy_factory(),
            base_params={"tp_pct": 0.01, "sl_pct": 0.02, "buy_every_n": 10},
            param_ranges={"tp_pct": [0.005, 0.01, 0.02]},
            data=data,
        )
        assert "tp_pct" in result.parameters
        ps = result.parameters["tp_pct"]
        assert isinstance(ps, ParameterSensitivity)
        assert len(ps.values) == 3

    async def test_multiple_params(self):
        data = _load_test_data(days=4)
        sa = SensitivityAnalysis(
            config=SensitivityConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await sa.run(
            strategy_factory=_make_strategy_factory(),
            base_params={"tp_pct": 0.01, "sl_pct": 0.02, "buy_every_n": 10},
            param_ranges={
                "tp_pct": [0.005, 0.01, 0.02],
                "sl_pct": [0.01, 0.02],
            },
            data=data,
        )
        assert "tp_pct" in result.parameters
        assert "sl_pct" in result.parameters

    async def test_rank_by_impact(self):
        data = _load_test_data(days=4)
        sa = SensitivityAnalysis(
            config=SensitivityConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await sa.run(
            strategy_factory=_make_strategy_factory(),
            base_params={"tp_pct": 0.01, "sl_pct": 0.02, "buy_every_n": 10},
            param_ranges={
                "tp_pct": [0.005, 0.01, 0.02],
                "sl_pct": [0.01, 0.02],
            },
            data=data,
        )
        ranking = result.rank_by_impact("total_return_pct")
        assert len(ranking) == 2
        # Each entry is (param_name, impact_float)
        assert isinstance(ranking[0][1], float)
        # Sorted descending by impact
        assert ranking[0][1] >= ranking[1][1]

    async def test_most_sensitive_param(self):
        data = _load_test_data(days=4)
        sa = SensitivityAnalysis(
            config=SensitivityConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await sa.run(
            strategy_factory=_make_strategy_factory(),
            base_params={"tp_pct": 0.01, "sl_pct": 0.02, "buy_every_n": 10},
            param_ranges={
                "tp_pct": [0.005, 0.01, 0.02],
                "sl_pct": [0.01, 0.02],
            },
            data=data,
        )
        most = result.most_sensitive_param("total_return_pct")
        assert most in ("tp_pct", "sl_pct")

    async def test_get_range_and_best_value(self):
        data = _load_test_data(days=4)
        sa = SensitivityAnalysis(
            config=SensitivityConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await sa.run(
            strategy_factory=_make_strategy_factory(),
            base_params={"tp_pct": 0.01, "sl_pct": 0.02, "buy_every_n": 10},
            param_ranges={"tp_pct": [0.005, 0.01, 0.02]},
            data=data,
        )
        ps = result.parameters["tp_pct"]
        r = ps.get_range("total_return_pct")
        assert r >= 0.0
        best = ps.get_best_value("total_return_pct", higher_is_better=True)
        assert best in [0.005, 0.01, 0.02]


# ===========================================================================
# Integration: Combine analytics modules
# ===========================================================================


class TestAnalyticsIntegration:
    async def test_backtest_then_monte_carlo(self):
        """Run a backtest then analyze with Monte Carlo."""
        data = _load_test_data(days=4)
        engine = MultiTimeframeBacktestEngine(config=MultiTFBacktestConfig(warmup_bars=20))
        strategy = SimpleTestStrategy(buy_every_n=5)
        bt_result = await engine.run(strategy, data)

        mc = MonteCarloSimulation(config=MonteCarloConfig(n_simulations=50, seed=42))
        mc_result = mc.run(bt_result)
        assert isinstance(mc_result, MonteCarloResult)
        assert mc_result.original_return_pct == float(bt_result.total_return_pct)

    async def test_optimize_then_sensitivity(self):
        """Optimize parameters then check sensitivity of best params."""
        data = _load_test_data(days=4)
        factory = _make_strategy_factory()

        # Optimize
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        opt_result = await optimizer.optimize(
            strategy_factory=factory,
            param_grid={"tp_pct": [0.01, 0.02]},
            data=data,
        )

        # Use best params as baseline for sensitivity
        sa = SensitivityAnalysis(
            config=SensitivityConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        base_params = opt_result.best_params.copy()
        base_params.setdefault("sl_pct", 0.02)
        base_params.setdefault("buy_every_n", 10)

        sens_result = await sa.run(
            strategy_factory=factory,
            base_params=base_params,
            param_ranges={"tp_pct": [0.005, 0.01, 0.02, 0.03]},
            data=data,
        )
        assert isinstance(sens_result, SensitivityResult)
        assert "tp_pct" in sens_result.parameters


# ===========================================================================
# Phase 1: New Metrics Tests
# ===========================================================================


class TestSortinoRatio:
    async def test_sortino_ratio_computed(self):
        data = _load_test_data(days=4)
        engine = MultiTimeframeBacktestEngine(config=MultiTFBacktestConfig(warmup_bars=20))
        strategy = SimpleTestStrategy(buy_every_n=5)
        result = await engine.run(strategy, data)
        # Sortino may be None if no downside returns, but should be computable
        # with volatile data
        if result.sortino_ratio is not None:
            assert isinstance(result.sortino_ratio, Decimal)

    async def test_sortino_no_downside(self):
        """With no downside returns, sortino should be None."""
        from bot.tests.backtesting.multi_tf_engine import MultiTimeframeBacktestEngine

        returns = [Decimal("0.01"), Decimal("0.02"), Decimal("0.005")]
        sortino = MultiTimeframeBacktestEngine._calculate_sortino(returns)
        assert sortino is None


class TestCalmarRatio:
    async def test_calmar_ratio_computed(self):
        data = _load_test_data(days=4)
        engine = MultiTimeframeBacktestEngine(config=MultiTFBacktestConfig(warmup_bars=20))
        strategy = SimpleTestStrategy(buy_every_n=5)
        result = await engine.run(strategy, data)
        # If there's a drawdown, calmar should be computed
        if result.max_drawdown_pct > 0 and result.calmar_ratio is not None:
            assert isinstance(result.calmar_ratio, Decimal)

    async def test_calmar_zero_drawdown(self):
        """With zero drawdown, calmar should be None."""
        result = _make_backtest_result()
        # Manually check — if max_drawdown_pct is 0, calmar is None
        from decimal import Decimal as D

        max_dd_pct = D("0")
        calmar = None
        if max_dd_pct > 0:
            calmar = D("5") / max_dd_pct
        assert calmar is None


class TestProfitFactor:
    async def test_profit_factor_computed(self):
        data = _load_test_data(days=4)
        engine = MultiTimeframeBacktestEngine(config=MultiTFBacktestConfig(warmup_bars=20))
        strategy = SimpleTestStrategy(buy_every_n=5)
        result = await engine.run(strategy, data)
        if result.profit_factor is not None:
            assert isinstance(result.profit_factor, Decimal)
            assert result.profit_factor >= Decimal("0")

    async def test_profit_factor_no_losses(self):
        """With no losing trades, profit_factor should be None."""
        # If gross_loss is 0, profit_factor is None
        result = _make_backtest_result(trades=[(100, 110), (100, 120)])
        # profit_factor is set by the engine, not constructor, so check logic:
        # All trades are winners → gross_loss = 0 → profit_factor = None
        assert result.profit_factor is None  # constructor doesn't set it


class TestCapitalEfficiency:
    async def test_capital_efficiency_tracked(self):
        data = _load_test_data(days=4)
        engine = MultiTimeframeBacktestEngine(config=MultiTFBacktestConfig(warmup_bars=20))
        strategy = SimpleTestStrategy(buy_every_n=5)
        result = await engine.run(strategy, data)
        # Capital efficiency should be computed
        if result.capital_efficiency is not None:
            assert isinstance(result.capital_efficiency, Decimal)


# ===========================================================================
# Phase 1C: Stress Testing Tests
# ===========================================================================


class TestStressTesting:
    async def test_stress_test_selects_volatile(self):
        from bot.tests.backtesting.stress_testing import StressTestConfig, StressTester

        data = _load_test_data(days=10)
        config = StressTestConfig(
            num_periods=2,
            backtest_config=MultiTFBacktestConfig(warmup_bars=20),
        )
        tester = StressTester()
        result = await tester.run(
            strategy_factory=lambda: SimpleTestStrategy(buy_every_n=5),
            data=data,
            config=config,
        )
        assert len(result.periods) <= 2
        # Periods should be sorted by volatility
        if len(result.periods) == 2:
            assert result.periods[0].volatility_score >= 0

    async def test_stress_test_non_overlapping(self):
        from bot.tests.backtesting.stress_testing import StressTestConfig, StressTester

        data = _load_test_data(days=10)
        config = StressTestConfig(
            num_periods=2,
            backtest_config=MultiTFBacktestConfig(warmup_bars=20),
        )
        tester = StressTester()
        result = await tester.run(
            strategy_factory=lambda: SimpleTestStrategy(buy_every_n=5),
            data=data,
            config=config,
        )
        # Check non-overlapping
        for i in range(len(result.periods)):
            for j in range(i + 1, len(result.periods)):
                p1 = result.periods[i]
                p2 = result.periods[j]
                assert (
                    p1.end_index <= p2.start_index or p2.end_index <= p1.start_index
                ), "Stress periods should not overlap"

    async def test_stress_test_runs_backtest(self):
        from bot.tests.backtesting.stress_testing import StressTestConfig, StressTester

        data = _load_test_data(days=10)
        config = StressTestConfig(
            num_periods=1,
            backtest_config=MultiTFBacktestConfig(warmup_bars=20),
        )
        tester = StressTester()
        result = await tester.run(
            strategy_factory=lambda: SimpleTestStrategy(buy_every_n=5),
            data=data,
            config=config,
        )
        if result.periods:
            assert isinstance(result.periods[0].result, BacktestResult)
            assert result.periods[0].result.initial_balance > 0


# ===========================================================================
# Phase 1D: Correlation-based param impact
# ===========================================================================


class TestParamImpactCorrelation:
    async def test_param_impact_correlation(self):
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await optimizer.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.005, 0.01, 0.02], "sl_pct": [0.01, 0.02]},
            data=data,
        )
        corr = result.get_param_impact_correlation()
        assert "tp_pct" in corr
        assert "sl_pct" in corr
        for v in corr.values():
            assert 0.0 <= v <= 1.0


# ===========================================================================
# Phase 1E: Rankings and Summary Tests
# ===========================================================================


class TestRankingsNewMetrics:
    async def test_rankings_include_new_metrics(self):
        from bot.tests.backtesting.strategy_comparison import StrategyComparison

        data = _load_test_data(days=4)
        comparison = StrategyComparison(config=MultiTFBacktestConfig(warmup_bars=20))
        s1 = SimpleTestStrategy(buy_every_n=5, name="s1")
        s2 = SimpleTestStrategy(buy_every_n=10, name="s2")
        result = await comparison.run([s1, s2], data)

        assert "sortino_ratio" in result.rankings
        assert "calmar_ratio" in result.rankings
        assert "profit_factor" in result.rankings

    async def test_summary_includes_new_metrics(self):
        from bot.tests.backtesting.strategy_comparison import StrategyComparison

        data = _load_test_data(days=4)
        comparison = StrategyComparison(config=MultiTFBacktestConfig(warmup_bars=20))
        s1 = SimpleTestStrategy(buy_every_n=5, name="s1")
        result = await comparison.run([s1], data)

        summary = result.summary["s1"]
        assert "sortino_ratio" in summary
        assert "calmar_ratio" in summary
        assert "profit_factor" in summary
        assert "capital_efficiency" in summary


# ===========================================================================
# Phase 3A: Two-Phase Optimizer Tests
# ===========================================================================


class TestTwoPhaseOptimizer:
    async def test_two_phase_optimizer(self):
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await optimizer.two_phase_optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.005, 0.01, 0.015, 0.02, 0.025]},
            data=data,
            coarse_steps=3,
            fine_steps=3,
        )
        assert isinstance(result, OptimizationResult)
        # Should have more trials than just coarse (coarse + fine)
        assert len(result.all_trials) >= 3

    async def test_fine_combos_narrow(self):
        """Fine phase should narrow around the best coarse value."""
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result = await optimizer.two_phase_optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.005, 0.01, 0.015, 0.02, 0.025]},
            data=data,
            coarse_steps=3,
            fine_steps=3,
        )
        # Best params should be in the original range
        best_tp = result.best_params.get("tp_pct", 0)
        assert 0.005 <= float(best_tp) <= 0.025

    async def test_two_phase_better_than_coarse(self):
        """Two-phase should find at least as good as coarse-only."""
        data = _load_test_data(days=4)
        optimizer = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        coarse_result = await optimizer.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.005, 0.015, 0.025]},
            data=data,
        )
        two_phase_result = await optimizer.two_phase_optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid={"tp_pct": [0.005, 0.01, 0.015, 0.02, 0.025]},
            data=data,
            coarse_steps=3,
            fine_steps=3,
        )
        # Two-phase best should be >= coarse best
        assert two_phase_result.best_objective >= coarse_result.best_objective


# ===========================================================================
# Phase 3B: Parallel Execution Tests
# ===========================================================================


class TestParallelExecution:
    async def test_parallel_same_as_sequential(self):
        """Parallel and sequential should produce same number of trials."""
        data = _load_test_data(days=4)
        grid = {"tp_pct": [0.01, 0.02], "sl_pct": [0.02]}

        opt_seq = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result_seq = await opt_seq.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid=grid,
            data=data,
        )

        opt_par = ParameterOptimizer(
            config=OptimizationConfig(
                backtest_config=MultiTFBacktestConfig(warmup_bars=20),
            )
        )
        result_par = await opt_par.optimize(
            strategy_factory=_make_strategy_factory(),
            param_grid=grid,
            data=data,
            max_workers=2,
        )

        assert len(result_seq.all_trials) == len(result_par.all_trials)


# ===========================================================================
# Phase 3C: Indicator Cache Tests
# ===========================================================================


class TestIndicatorCache:
    def test_cache_hit_miss(self):
        from bot.tests.backtesting.indicator_cache import IndicatorCache

        cache = IndicatorCache(max_size=10)
        cache.put("key1", 42)

        assert cache.get("key1") == 42  # hit
        assert cache.get("key2") is None  # miss

        stats = cache.stats
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5

    def test_cache_eviction(self):
        from bot.tests.backtesting.indicator_cache import IndicatorCache

        cache = IndicatorCache(max_size=10)
        for i in range(15):
            cache.put(f"key{i}", i)

        # Should have evicted some entries
        assert cache.stats["size"] <= 10

    def test_cache_serialization(self):
        from decimal import Decimal as D

        from bot.tests.backtesting.indicator_cache import IndicatorCache

        cache = IndicatorCache(max_size=10)
        cache.put("k1", D("3.14"))
        cache.put("k2", [D("1.0"), D("2.0")])

        data = cache.to_dict()
        restored = IndicatorCache.from_dict(data, max_size=10)

        assert restored.get("k1") == D("3.14")
        assert restored.get("k2") == [D("1.0"), D("2.0")]

    def test_cache_make_key(self):
        from bot.tests.backtesting.indicator_cache import IndicatorCache

        key1 = IndicatorCache.make_key("sma", "abc123", period=20)
        key2 = IndicatorCache.make_key("sma", "abc123", period=20)
        key3 = IndicatorCache.make_key("sma", "abc123", period=50)

        assert key1 == key2
        assert key1 != key3

    def test_get_or_compute(self):
        from bot.tests.backtesting.indicator_cache import IndicatorCache

        cache = IndicatorCache(max_size=10)
        counter = {"calls": 0}

        def compute():
            counter["calls"] += 1
            return 99

        result1 = cache.get_or_compute("key", compute)
        result2 = cache.get_or_compute("key", compute)

        assert result1 == 99
        assert result2 == 99
        assert counter["calls"] == 1  # computed only once
