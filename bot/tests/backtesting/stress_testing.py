"""
Stress Testing — Evaluate strategy on the most volatile market periods.

Selects high-volatility windows from historical data and runs backtests
on each to assess strategy resilience under adverse conditions.

Usage:
    tester = StressTester()
    result = await tester.run(
        strategy_factory=lambda: MyStrategy(),
        data=data,
        config=StressTestConfig(num_periods=3),
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bot.tests.backtesting.backtesting_engine import BacktestResult
from bot.tests.backtesting.multi_tf_data_loader import MultiTimeframeData
from bot.tests.backtesting.multi_tf_engine import (
    MultiTFBacktestConfig,
    MultiTimeframeBacktestEngine,
)
from bot.tests.backtesting.walk_forward import WalkForwardAnalysis


@dataclass
class StressTestConfig:
    """Configuration for stress testing."""

    num_periods: int = 3
    period_length: int | None = None  # default: max(20, len(m5)//4)
    backtest_config: MultiTFBacktestConfig = field(default_factory=MultiTFBacktestConfig)


@dataclass
class StressPeriod:
    """Result for a single stress test period."""

    start_index: int
    end_index: int
    volatility_score: float
    result: BacktestResult


@dataclass
class StressTestResult:
    """Aggregated stress test results."""

    periods: list[StressPeriod]

    @property
    def worst_return_pct(self) -> float:
        """Return of the worst-performing stress period."""
        if not self.periods:
            return 0.0
        return min(float(p.result.total_return_pct) for p in self.periods)

    @property
    def avg_return_pct(self) -> float:
        """Average return across stress periods."""
        if not self.periods:
            return 0.0
        return sum(float(p.result.total_return_pct) for p in self.periods) / len(self.periods)


class StressTester:
    """
    Stress test a strategy on the most volatile market periods.

    Algorithm:
    1. Sliding window over M5 data: volatility = (max_high - min_low) / avg_close
    2. Sort windows by volatility descending
    3. Select top N non-overlapping windows
    4. Run backtest on each
    """

    async def run(
        self,
        strategy_factory: callable,
        data: MultiTimeframeData,
        config: StressTestConfig | None = None,
    ) -> StressTestResult:
        """Run stress tests on the most volatile periods."""
        config = config or StressTestConfig()
        m5 = data.m5
        total = len(m5)

        period_length = config.period_length or max(20, total // 4)
        warmup = config.backtest_config.warmup_bars

        # Ensure period is large enough for warmup + execution
        if period_length <= warmup + 10:
            period_length = warmup + 20

        # Calculate volatility for each window
        windows: list[tuple[int, int, float]] = []
        step = max(1, period_length // 4)

        for start in range(0, total - period_length + 1, step):
            end = start + period_length
            window = m5.iloc[start:end]
            max_high = window["high"].max()
            min_low = window["low"].min()
            avg_close = window["close"].mean()

            if avg_close > 0:
                volatility = float((max_high - min_low) / avg_close)
            else:
                volatility = 0.0

            windows.append((start, end, volatility))

        # Sort by volatility descending
        windows.sort(key=lambda x: x[2], reverse=True)

        # Select top N non-overlapping windows
        selected: list[tuple[int, int, float]] = []
        for start, end, vol in windows:
            overlaps = any(not (end <= s or start >= e) for s, e, _ in selected)
            if not overlaps:
                selected.append((start, end, vol))
            if len(selected) >= config.num_periods:
                break

        # Run backtests on each selected period
        wf = WalkForwardAnalysis()
        periods: list[StressPeriod] = []

        for start, end, vol in selected:
            slice_data = wf._build_slice_data(data, slice(start, end))
            strategy = strategy_factory()
            engine = MultiTimeframeBacktestEngine(config=config.backtest_config)
            result = await engine.run(strategy, slice_data)

            periods.append(
                StressPeriod(
                    start_index=start,
                    end_index=end,
                    volatility_score=vol,
                    result=result,
                )
            )

        return StressTestResult(periods=periods)
