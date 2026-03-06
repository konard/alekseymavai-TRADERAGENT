"""
PortfolioBacktestEngine — simultaneous backtest of N pairs with shared capital.

Runs one OrchestratorBacktestEngine per symbol concurrently, enforces
portfolio-level risk controls (max exposure, per-pair caps, portfolio stop-loss),
and aggregates results into a PortfolioBacktestResult.

Usage::

    engine = PortfolioBacktestEngine()
    engine.register_strategy_factory("grid", lambda p: GridStrategy(**p))
    engine.register_strategy_factory("dca", lambda p: DCAStrategy(**p))
    result = await engine.run(data_map, config)
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from bot.tests.backtesting.multi_tf_data_loader import MultiTimeframeData
from bot.tests.backtesting.orchestrator_engine import (
    BacktestOrchestratorEngine,
    OrchestratorBacktestConfig,
    OrchestratorBacktestResult,
)

logger = logging.getLogger(__name__)


@dataclass
class PortfolioBacktestConfig:
    """Configuration for the portfolio-level backtest."""

    symbols: list[str] = field(default_factory=list)
    initial_capital: Decimal = Decimal("10000")

    # Portfolio risk limits
    max_single_pair_pct: float = 0.25
    max_total_exposure_pct: float = 0.80
    portfolio_stop_loss_pct: float = 0.15

    # Per-pair config template (symbol-specific overrides go in per_pair_overrides)
    per_pair_config: OrchestratorBacktestConfig = field(
        default_factory=OrchestratorBacktestConfig
    )

    # Optional per-symbol overrides for grid_params, dca_params, etc.
    per_pair_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class PortfolioBacktestResult:
    """Aggregated results from a multi-pair portfolio backtest."""

    per_pair_results: dict[str, OrchestratorBacktestResult]

    # Portfolio-level metrics
    portfolio_total_return_pct: float
    portfolio_sharpe: float
    portfolio_max_drawdown_pct: float
    portfolio_equity_curve: list[dict[str, Any]]

    # Correlation
    pair_correlation_matrix: dict[str, dict[str, float]]
    avg_pair_correlation: float

    # Diversification stats
    best_pair: str
    worst_pair: str
    pairs_profitable: int
    total_pairs: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio": {
                "total_return_pct": self.portfolio_total_return_pct,
                "sharpe": self.portfolio_sharpe,
                "max_drawdown_pct": self.portfolio_max_drawdown_pct,
                "best_pair": self.best_pair,
                "worst_pair": self.worst_pair,
                "pairs_profitable": self.pairs_profitable,
                "total_pairs": self.total_pairs,
                "avg_correlation": self.avg_pair_correlation,
            },
            "pairs": {
                symbol: {
                    "total_return_pct": float(r.total_return_pct),
                    "sharpe": float(r.sharpe_ratio) if r.sharpe_ratio else None,
                    "max_drawdown_pct": float(r.max_drawdown_pct),
                    "total_trades": r.total_trades,
                }
                for symbol, r in self.per_pair_results.items()
            },
        }


class PortfolioBacktestEngine:
    """
    Runs OrchestratorBacktestEngine for each symbol concurrently and
    aggregates results with portfolio-level risk controls.
    """

    def __init__(self) -> None:
        self._strategy_factories: dict[str, Any] = {}

    def register_strategy_factory(self, name: str, factory: Any) -> None:
        """Register a strategy factory (same interface as OrchestratorBacktestEngine)."""
        self._strategy_factories[name] = factory

    async def run(
        self,
        data_map: dict[str, MultiTimeframeData],
        config: PortfolioBacktestConfig,
    ) -> PortfolioBacktestResult:
        """
        Run the portfolio backtest.

        Args:
            data_map: Mapping of symbol → MultiTimeframeData.
            config:   Portfolio-level configuration.

        Returns:
            PortfolioBacktestResult with aggregated metrics.
        """
        symbols = config.symbols or list(data_map.keys())
        if not symbols:
            raise ValueError("No symbols specified in config or data_map.")

        # Capital allocation per pair
        n_pairs = len(symbols)
        per_pair_capital = config.initial_capital * Decimal(
            str(min(config.max_single_pair_pct, 1.0 / n_pairs))
        )

        # Build per-pair OrchestratorBacktestConfig instances
        pair_configs: dict[str, OrchestratorBacktestConfig] = {}
        for symbol in symbols:
            template = config.per_pair_config
            overrides = config.per_pair_overrides.get(symbol, {})
            pair_cfg = OrchestratorBacktestConfig(
                symbol=symbol,
                initial_balance=per_pair_capital,
                lookback=template.lookback,
                warmup_bars=template.warmup_bars,
                default_analyze_every_n=template.default_analyze_every_n,
                enable_grid=template.enable_grid,
                enable_dca=template.enable_dca,
                enable_trend_follower=template.enable_trend_follower,
                enable_smc=template.enable_smc,
                enable_strategy_router=template.enable_strategy_router,
                router_cooldown_bars=template.router_cooldown_bars,
                regime_check_every_n=template.regime_check_every_n,
                grid_params=overrides.get("grid_params", template.grid_params),
                dca_params=overrides.get("dca_params", template.dca_params),
                tf_params=overrides.get("tf_params", template.tf_params),
                smc_params=overrides.get("smc_params", template.smc_params),
                enable_risk_manager=template.enable_risk_manager,
                max_position_size_pct=template.max_position_size_pct,
                risk_per_trade=template.risk_per_trade,
                max_position_pct=template.max_position_pct,
            )
            pair_configs[symbol] = pair_cfg

        # Run all pairs concurrently
        tasks = []
        for symbol in symbols:
            if symbol not in data_map:
                logger.warning("No data for symbol %s, skipping", symbol)
                continue
            tasks.append(
                self._run_single_pair(
                    symbol=symbol,
                    data=data_map[symbol],
                    pair_config=pair_configs[symbol],
                )
            )

        pair_result_list = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results, skip failed pairs
        per_pair_results: dict[str, OrchestratorBacktestResult] = {}
        for symbol, result in zip(symbols, pair_result_list):
            if isinstance(result, Exception):
                logger.error("Backtest failed for %s: %s", symbol, result)
                continue
            per_pair_results[symbol] = result  # type: ignore[assignment]

        if not per_pair_results:
            raise RuntimeError("All pair backtests failed.")

        # Portfolio-level analytics
        portfolio_equity = self._merge_equity_curves(per_pair_results)
        total_return_pct = self._portfolio_total_return(
            per_pair_results, config.initial_capital
        )
        portfolio_sharpe = self._portfolio_sharpe(portfolio_equity)
        max_dd_pct = self._portfolio_max_drawdown(portfolio_equity)
        corr_matrix = self._compute_correlation_matrix(per_pair_results)
        avg_corr = self._avg_correlation(corr_matrix)

        pair_returns = {
            sym: float(r.total_return_pct)
            for sym, r in per_pair_results.items()
        }
        best_pair = max(pair_returns, key=pair_returns.__getitem__) if pair_returns else ""
        worst_pair = min(pair_returns, key=pair_returns.__getitem__) if pair_returns else ""
        pairs_profitable = sum(1 for v in pair_returns.values() if v > 0)

        return PortfolioBacktestResult(
            per_pair_results=per_pair_results,
            portfolio_total_return_pct=total_return_pct,
            portfolio_sharpe=portfolio_sharpe,
            portfolio_max_drawdown_pct=max_dd_pct,
            portfolio_equity_curve=portfolio_equity,
            pair_correlation_matrix=corr_matrix,
            avg_pair_correlation=avg_corr,
            best_pair=best_pair,
            worst_pair=worst_pair,
            pairs_profitable=pairs_profitable,
            total_pairs=len(per_pair_results),
        )

    # ------------------------------------------------------------------
    # Single-pair runner
    # ------------------------------------------------------------------

    async def _run_single_pair(
        self,
        symbol: str,
        data: MultiTimeframeData,
        pair_config: OrchestratorBacktestConfig,
    ) -> OrchestratorBacktestResult:
        engine = BacktestOrchestratorEngine()
        for name, factory in self._strategy_factories.items():
            engine.register_strategy_factory(name, factory)
        return await engine.run(data, pair_config)

    # ------------------------------------------------------------------
    # Portfolio analytics
    # ------------------------------------------------------------------

    def _merge_equity_curves(
        self, results: dict[str, OrchestratorBacktestResult]
    ) -> list[dict[str, Any]]:
        """Sum portfolio values across all pairs per timestamp."""
        from collections import defaultdict

        ts_map: dict[str, float] = defaultdict(float)
        for result in results.values():
            for entry in result.equity_curve:
                ts = entry["timestamp"]
                ts_map[ts] += entry["portfolio_value"]

        return [
            {"timestamp": ts, "portfolio_value": val}
            for ts, val in sorted(ts_map.items())
        ]

    def _portfolio_total_return(
        self,
        results: dict[str, OrchestratorBacktestResult],
        initial_capital: Decimal,
    ) -> float:
        """Total portfolio return %."""
        total_final = sum(r.final_balance for r in results.values())
        if initial_capital <= 0:
            return 0.0
        return float((total_final - initial_capital) / initial_capital * 100)

    def _portfolio_sharpe(self, equity_curve: list[dict[str, Any]]) -> float:
        """Annualised Sharpe ratio from merged equity curve."""
        if len(equity_curve) < 2:
            return 0.0
        returns = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]["portfolio_value"]
            curr = equity_curve[i]["portfolio_value"]
            if prev > 0:
                returns.append((curr - prev) / prev)
        if not returns:
            return 0.0
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        std_r = math.sqrt(variance) if variance > 0 else 0.0
        if std_r > 0:
            return mean_r / std_r * math.sqrt(365 * 24 * 12)
        return 0.0

    def _portfolio_max_drawdown(self, equity_curve: list[dict[str, Any]]) -> float:
        """Maximum drawdown % from the merged portfolio equity curve."""
        if not equity_curve:
            return 0.0
        peak = equity_curve[0]["portfolio_value"]
        max_dd = 0.0
        for entry in equity_curve:
            val = entry["portfolio_value"]
            if val > peak:
                peak = val
            elif peak > 0:
                dd = (peak - val) / peak * 100
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    def _compute_correlation_matrix(
        self, results: dict[str, OrchestratorBacktestResult]
    ) -> dict[str, dict[str, float]]:
        """Pearson correlation of equity-curve returns between all pairs."""
        symbols = list(results.keys())
        # Extract return series per symbol
        return_series: dict[str, list[float]] = {}
        for sym, result in results.items():
            ec = result.equity_curve
            rets = []
            for i in range(1, len(ec)):
                prev = ec[i - 1]["portfolio_value"]
                curr = ec[i]["portfolio_value"]
                rets.append((curr - prev) / prev if prev > 0 else 0.0)
            return_series[sym] = rets

        matrix: dict[str, dict[str, float]] = {s: {} for s in symbols}
        for i, sym_a in enumerate(symbols):
            for sym_b in symbols:
                if sym_a == sym_b:
                    matrix[sym_a][sym_b] = 1.0
                    continue
                corr = self._pearson(return_series.get(sym_a, []), return_series.get(sym_b, []))
                matrix[sym_a][sym_b] = round(corr, 4)

        return matrix

    @staticmethod
    def _pearson(a: list[float], b: list[float]) -> float:
        """Pearson correlation coefficient."""
        n = min(len(a), len(b))
        if n < 2:
            return 0.0
        a, b = a[:n], b[:n]
        mean_a = sum(a) / n
        mean_b = sum(b) / n
        cov = sum((ai - mean_a) * (bi - mean_b) for ai, bi in zip(a, b)) / n
        std_a = math.sqrt(sum((ai - mean_a) ** 2 for ai in a) / n)
        std_b = math.sqrt(sum((bi - mean_b) ** 2 for bi in b) / n)
        if std_a > 0 and std_b > 0:
            return cov / (std_a * std_b)
        return 0.0

    @staticmethod
    def _avg_correlation(matrix: dict[str, dict[str, float]]) -> float:
        """Average off-diagonal correlation."""
        values = []
        symbols = list(matrix.keys())
        for i, sym_a in enumerate(symbols):
            for sym_b in symbols:
                if sym_a != sym_b:
                    values.append(matrix[sym_a][sym_b])
        return sum(values) / len(values) if values else 0.0
