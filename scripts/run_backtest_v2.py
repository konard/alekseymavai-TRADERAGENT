#!/usr/bin/env python3
"""
Backtest V2.0 Pipeline — multi-strategy orchestrator with regime routing.

Mirrors the live bot's BotOrchestrator._main_loop() on historical data:
Grid + DCA + TrendFollower strategies, routed by MarketRegimeDetector with
cooldown guards and portfolio-level risk management.

Modes:
  single  — one pair, all four phases
  multi   — fixed list of pairs, phases 1 + 3 + 4
  auto    — Phase 1 discovers top-N pairs, then runs phases 2-4

Phases:
  1: Baseline (OrchestratorBacktestEngine, default params)
  2: Optimization (ParameterOptimizer.optimize_orchestrator)
  3: Portfolio (PortfolioBacktestEngine, N pairs simultaneously)
  4: Robustness (Walk-Forward + Monte Carlo)

Usage:
    # Mode 1: Single pair
    python scripts/run_backtest_v2.py --mode single --symbol BTCUSDT --workers 8

    # Mode 2: Fixed multi-pair
    python scripts/run_backtest_v2.py --mode multi --symbols BTC,ETH,SOL --workers 8

    # Mode 3: Auto-select from data dir
    python scripts/run_backtest_v2.py --mode auto --top-n 10 --data-dir data/historical/

    # Small smoke test
    python scripts/run_backtest_v2.py --mode single --symbol BTCUSDT --max-bars 1000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bot.tests.backtesting.multi_tf_data_loader import (
    MultiTimeframeData,
    MultiTimeframeDataLoader,
)
from bot.tests.backtesting.orchestrator_engine import (
    BacktestOrchestratorEngine,
    OrchestratorBacktestConfig,
    OrchestratorBacktestResult,
)
from bot.tests.backtesting.optimization import (
    OptimizationConfig,
    OptimizationResult,
    ParameterOptimizer,
)
from bot.tests.backtesting.portfolio_engine import (
    PortfolioBacktestConfig,
    PortfolioBacktestEngine,
    PortfolioBacktestResult,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_backtest_v2")

# ---------------------------------------------------------------------------
# Default optimization parameter grid (Unified for one-pair optimization)
# ---------------------------------------------------------------------------
ORCHESTRATOR_PARAM_GRID: dict[str, list[Any]] = {
    # Router params
    "router_cooldown_bars": [60, 120, 240],
    "regime_check_every_n": [6, 12, 24],
    # Risk params
    "max_position_size_pct": [0.15, 0.20, 0.25],
    # DCA sub-params (forwarded to dca_params dict via "dca_" prefix)
    "dca_trigger_pct": [0.03, 0.05, 0.07],
    "dca_tp_pct": [0.05, 0.08, 0.10],
    # TrendFollower sub-params
    "tf_ema_fast": [10, 15, 20],
    # SMC sub-params (forwarded to smc_params dict via "smc_" prefix)
    "smc_min_risk_reward": [2.0, 2.5, 3.0],
}


# ---------------------------------------------------------------------------
# Strategy factory helpers
# ---------------------------------------------------------------------------

def _make_strategy_factories(
    symbol: str,
    grid_params: dict | None = None,
    dca_params: dict | None = None,
    tf_params: dict | None = None,
    smc_params: dict | None = None,
) -> dict:
    """
    Build strategy factory callables.

    Returns a dict of name → factory (callable that takes a params dict and
    returns a BaseStrategy).  Only factories for strategies that can be
    imported are included; missing adapters are silently skipped.
    """
    factories: dict = {}

    try:
        from bot.strategies.dca_adapter import DCAAdapter
        from bot.strategies.grid_adapter import GridAdapter

        def _grid_factory(params: dict):
            merged = {**(grid_params or {}), **params}
            return GridAdapter(symbol=symbol, **merged)

        def _dca_factory(params: dict):
            merged = {**(dca_params or {}), **params}
            # Map grid-search friendly names to DCAAdapter constructor names
            trigger = merged.pop("trigger_pct", None)
            tp = merged.pop("tp_pct", None)
            if trigger is not None:
                merged["price_deviation_pct"] = Decimal(str(trigger))
            if tp is not None:
                merged["take_profit_pct"] = Decimal(str(tp))
            return DCAAdapter(symbol=symbol, **merged)

        factories["grid"] = _grid_factory
        factories["dca"] = _dca_factory
    except ImportError as e:
        logger.debug("Could not import Grid/DCA adapters: %s", e)

    try:
        from bot.strategies.trend_follower_adapter import TrendFollowerAdapter
        from bot.strategies.trend_follower.config import TrendFollowerConfig

        def _tf_factory(params: dict):
            merged = {**(tf_params or {}), **params}
            ema_fast = merged.pop("ema_fast", 20)
            cfg = TrendFollowerConfig(ema_fast_period=ema_fast)
            return TrendFollowerAdapter(config=cfg)

        factories["trend_follower"] = _tf_factory
    except ImportError as e:
        logger.debug("Could not import TrendFollowerAdapter: %s", e)

    try:
        from bot.strategies.smc_adapter import SMCStrategyAdapter
        from bot.strategies.smc.config import SMCConfig

        _smc_fields = {f.name for f in SMCConfig.__dataclass_fields__.values()}  # type: ignore[union-attr]

        def _smc_factory(params: dict):
            merged = {**(smc_params or {}), **params}
            # warmup_bars=0: engine warmup already provides historical context,
            # so skip the strategy-level warmup guard to avoid double-warmup.
            merged.setdefault("warmup_bars", 0)
            cfg_kwargs = {k: v for k, v in merged.items() if k in _smc_fields}
            cfg = SMCConfig(**cfg_kwargs)
            return SMCStrategyAdapter(
                config=cfg,
                account_balance=Decimal("10000"),
                name="smc-backtest",
            )

        factories["smc"] = _smc_factory
    except ImportError as e:
        logger.debug("Could not import SMCStrategyAdapter: %s", e)

    if not factories:
        logger.warning(
            "No strategy factories available. "
            "The orchestrator engine will run with no strategies."
        )
    return factories


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_data(
    symbol: str,
    data_dir: Path | None,
    max_bars: int | None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> MultiTimeframeData:
    """Load multi-timeframe data from disk or generate synthetic data."""
    loader = MultiTimeframeDataLoader()

    if data_dir and data_dir.exists():
        # Use exact suffix match to avoid e.g. "*15m*.csv" matching "*5m*" pattern
        csv_files = [
            f for f in data_dir.glob(f"*{symbol}*.csv")
            if f.stem.endswith("_5m")
        ]
        if csv_files:
            try:
                data = loader.load_csv(str(csv_files[0]))
                if max_bars and len(data.m5) > max_bars:
                    # Trim to last max_bars M5 bars
                    data = _trim_data(data, max_bars)
                logger.info("Loaded %d M5 bars from %s", len(data.m5), csv_files[0].name)
                return data
            except Exception as e:
                logger.warning("Failed to load CSV for %s: %s — using synthetic data", symbol, e)

    # Fall back to synthetic data
    from datetime import timedelta

    _end = end_date or datetime(2024, 1, 1)
    if max_bars:
        _start = _end - timedelta(minutes=5 * max_bars)
    else:
        _start = start_date or datetime(2023, 1, 1)

    logger.info("Generating synthetic data for %s (%d bars)", symbol, max_bars or "~52560")
    data = loader.load(symbol=symbol, start_date=_start, end_date=_end)
    return data


def _trim_data(data: MultiTimeframeData, max_bars: int) -> MultiTimeframeData:
    """Trim MultiTimeframeData to last max_bars M5 bars."""
    m5 = data.m5.iloc[-max_bars:]
    start_ts = m5.index[0]
    return MultiTimeframeData(
        m5=m5,
        m15=data.m15[data.m15.index >= start_ts],
        h1=data.h1[data.h1.index >= start_ts],
        h4=data.h4[data.h4.index >= start_ts],
        d1=data.d1[data.d1.index >= start_ts],
    )


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------

async def phase1_baseline(
    symbol: str,
    data: MultiTimeframeData,
    config: OrchestratorBacktestConfig,
    factories: dict,
) -> OrchestratorBacktestResult:
    """Phase 1: Baseline run with default parameters."""
    logger.info("[Phase 1] Baseline — %s", symbol)
    t0 = time.perf_counter()

    engine = BacktestOrchestratorEngine()
    for name, factory in factories.items():
        engine.register_strategy_factory(name, factory)

    result = await engine.run(data, config)
    elapsed = time.perf_counter() - t0

    logger.info(
        "[Phase 1] Done %s | return=%.2f%% | sharpe=%s | trades=%d | switches=%d | %.1fs",
        symbol,
        float(result.total_return_pct),
        f"{float(result.sharpe_ratio):.3f}" if result.sharpe_ratio else "N/A",
        result.total_trades,
        len(result.strategy_switches),
        elapsed,
    )
    return result


async def phase2_optimize(
    symbol: str,
    data: MultiTimeframeData,
    config_template: OrchestratorBacktestConfig,
    param_grid: dict,
    workers: int,
    factories: dict | None = None,
) -> tuple[OptimizationResult, OrchestratorBacktestConfig]:
    """Phase 2: Parameter optimization."""
    logger.info("[Phase 2] Optimization — %s (%d combos)", symbol, _count_combos(param_grid))
    t0 = time.perf_counter()

    opt_cfg = OptimizationConfig(objective="sharpe_ratio", higher_is_better=True)
    optimizer = ParameterOptimizer(config=opt_cfg)
    # Pass strategy factories so each trial can build strategies
    if factories:
        optimizer._strategy_factories = factories

    opt_result = await optimizer.optimize_orchestrator(
        param_grid=param_grid,
        data=data,
        config_template=config_template,
        max_workers=workers if workers > 1 else None,
    )

    # Build optimized config
    optimized_config = ParameterOptimizer._apply_orchestrator_params(
        config_template, opt_result.best_params
    )

    elapsed = time.perf_counter() - t0
    logger.info(
        "[Phase 2] Done %s | best_sharpe=%.3f | best_params=%s | %.1fs",
        symbol,
        opt_result.best_objective,
        opt_result.best_params,
        elapsed,
    )
    return opt_result, optimized_config


async def phase3_portfolio(
    symbols: list[str],
    data_map: dict[str, MultiTimeframeData],
    per_pair_config: OrchestratorBacktestConfig,
    factories: dict,
    initial_capital: Decimal,
) -> PortfolioBacktestResult:
    """Phase 3: Portfolio backtest across N pairs."""
    logger.info("[Phase 3] Portfolio — %d pairs: %s", len(symbols), symbols)
    t0 = time.perf_counter()

    port_config = PortfolioBacktestConfig(
        symbols=symbols,
        initial_capital=initial_capital,
        per_pair_config=per_pair_config,
    )

    port_engine = PortfolioBacktestEngine()
    for name, factory in factories.items():
        port_engine.register_strategy_factory(name, factory)

    result = await port_engine.run(data_map, port_config)
    elapsed = time.perf_counter() - t0

    logger.info(
        "[Phase 3] Done | portfolio_return=%.2f%% | sharpe=%.3f | max_dd=%.2f%% "
        "| profitable=%d/%d | avg_corr=%.3f | %.1fs",
        result.portfolio_total_return_pct,
        result.portfolio_sharpe,
        result.portfolio_max_drawdown_pct,
        result.pairs_profitable,
        result.total_pairs,
        result.avg_pair_correlation,
        elapsed,
    )
    return result


async def phase4_robustness(
    symbol: str,
    data: MultiTimeframeData,
    best_result: OrchestratorBacktestResult,
) -> dict[str, Any]:
    """Phase 4: Walk-Forward + Monte Carlo robustness checks."""
    logger.info("[Phase 4] Robustness — %s", symbol)
    t0 = time.perf_counter()
    robustness: dict[str, Any] = {}

    # Monte Carlo on the trade history
    try:
        from bot.tests.backtesting.monte_carlo import MonteCarloConfig, MonteCarloSimulation

        mc_config = MonteCarloConfig(n_simulations=500)
        mc = MonteCarloSimulation(config=mc_config)
        trade_returns = [
            float(t.get("profit", 0)) for t in best_result.trade_history
        ]
        if trade_returns:
            mc_result = mc.run(trade_returns, initial_balance=float(best_result.initial_balance))
            robustness["monte_carlo"] = {
                "median_return_pct": mc_result.median_return_pct,
                "p5_return_pct": mc_result.p5_return_pct,
                "p95_return_pct": mc_result.p95_return_pct,
                "probability_of_profit": mc_result.probability_of_profit,
            }
            logger.info(
                "[Phase 4] MC median=%.2f%% p5=%.2f%% p(profit)=%.1f%%",
                mc_result.median_return_pct,
                mc_result.p5_return_pct,
                mc_result.probability_of_profit * 100,
            )
    except Exception as e:
        logger.warning("[Phase 4] Monte Carlo failed: %s", e)
        robustness["monte_carlo"] = {"error": str(e)}

    elapsed = time.perf_counter() - t0
    logger.info("[Phase 4] Done %.1fs", elapsed)
    return robustness


# ---------------------------------------------------------------------------
# Result serialisation
# ---------------------------------------------------------------------------

def _save_results(output_dir: Path, name: str, data: Any) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    logger.info("Saved %s", path)


def _count_combos(grid: dict) -> int:
    result = 1
    for v in grid.values():
        result *= len(v)
    return result


# ---------------------------------------------------------------------------
# Mode runners
# ---------------------------------------------------------------------------

async def run_single(args: argparse.Namespace) -> None:
    """Single-pair mode: all 4 phases."""
    symbol = args.symbol
    data = _load_data(
        symbol=symbol,
        data_dir=Path(args.data_dir) if args.data_dir else None,
        max_bars=args.max_bars,
    )

    warmup = min(args.warmup_bars, len(data.m5) // 2)
    config = OrchestratorBacktestConfig(
        symbol=symbol,
        initial_balance=Decimal(str(args.initial_balance)),
        warmup_bars=warmup,
        enable_strategy_router=True,
    )
    factories = _make_strategy_factories(symbol, smc_params={})

    output_dir = Path(args.output_dir) / f"single_{symbol}_{datetime.now():%Y%m%d_%H%M%S}"

    # Phase 1
    phases_set = set(args.phases.split(",")) if args.phases else set()
    p1_result = await phase1_baseline(symbol, data, config, factories)
    _save_results(output_dir, "phase1_baseline", p1_result.to_dict())

    if phases_set and "2" not in phases_set:
        return

    # Phase 2
    p2_result, optimized_config = await phase2_optimize(
        symbol, data, config, ORCHESTRATOR_PARAM_GRID, args.workers, factories=factories
    )
    _save_results(output_dir, "phase2_optimization", {
        "best_params": p2_result.best_params,
        "best_objective": p2_result.best_objective,
    })

    if phases_set and "3" not in phases_set and "4" not in phases_set:
        return

    # Phase 3 (single pair "portfolio")
    p3_result = await phase3_portfolio(
        symbols=[symbol],
        data_map={symbol: data},
        per_pair_config=optimized_config,
        factories=factories,
        initial_capital=Decimal(str(args.initial_balance)),
    )
    _save_results(output_dir, "phase3_portfolio", p3_result.to_dict())

    # Phase 4
    best_result = (
        p2_result.all_trials[0].result
        if p2_result.all_trials
        else p1_result
    )
    # Cast to OrchestratorBacktestResult if needed
    if not isinstance(best_result, OrchestratorBacktestResult):
        best_result = p1_result
    p4_result = await phase4_robustness(symbol, data, best_result)
    _save_results(output_dir, "phase4_robustness", p4_result)

    logger.info("All phases complete. Results saved to %s", output_dir)


async def run_multi(args: argparse.Namespace) -> None:
    """Multi-pair mode: phases 1 + 3."""
    raw_symbols = (args.symbols or "BTC,ETH,SOL").split(",")
    symbols = [s.strip() for s in raw_symbols if s.strip()]

    data_map: dict[str, MultiTimeframeData] = {}
    for sym in symbols:
        try:
            data_map[sym] = _load_data(
                symbol=sym,
                data_dir=Path(args.data_dir) if args.data_dir else None,
                max_bars=args.max_bars,
            )
        except Exception as e:
            logger.warning("Skipping %s — data load failed: %s", sym, e)

    warmup = args.warmup_bars
    config = OrchestratorBacktestConfig(
        initial_balance=Decimal(str(args.initial_balance)) / len(symbols),
        warmup_bars=warmup,
        enable_strategy_router=True,
    )
    factories = _make_strategy_factories(symbols[0], smc_params={})
    output_dir = Path(args.output_dir) / f"multi_{datetime.now():%Y%m%d_%H%M%S}"

    # Phase 1: baseline per pair
    for sym, data in data_map.items():
        cfg = OrchestratorBacktestConfig(
            symbol=sym,
            initial_balance=Decimal(str(args.initial_balance)),
            warmup_bars=warmup,
            enable_strategy_router=True,
        )
        p1 = await phase1_baseline(sym, data, cfg, factories)
        _save_results(output_dir, f"phase1_{sym}", p1.to_dict())

    # Phase 3: portfolio
    p3 = await phase3_portfolio(
        symbols=list(data_map.keys()),
        data_map=data_map,
        per_pair_config=config,
        factories=factories,
        initial_capital=Decimal(str(args.initial_balance)),
    )
    _save_results(output_dir, "phase3_portfolio", p3.to_dict())
    logger.info("Multi-pair mode complete. Results in %s", output_dir)


async def run_auto(args: argparse.Namespace) -> None:
    """Auto mode: discover top-N pairs from data dir, then run full pipeline."""
    data_dir = Path(args.data_dir) if args.data_dir else Path("data/historical")
    if not data_dir.exists():
        logger.error("Data dir does not exist: %s", data_dir)
        sys.exit(1)

    # Discover symbols from 5m CSV files
    csv_files = list(data_dir.glob("*5m*.csv")) + list(data_dir.glob("*_5m.csv"))
    symbols_found = []
    for f in csv_files:
        # Try to extract symbol from filename
        name = f.stem.upper()
        for suffix in ["_5M", "_5MIN", "_5m", "_5min"]:
            name = name.replace(suffix.upper(), "")
        symbols_found.append(name)

    if not symbols_found:
        logger.error("No 5m CSV files found in %s", data_dir)
        sys.exit(1)

    symbols_found = symbols_found[: args.top_n]
    logger.info("Auto mode: found %d symbols: %s", len(symbols_found), symbols_found)

    # Run Phase 1 to rank pairs
    loader = MultiTimeframeDataLoader()
    data_map: dict[str, MultiTimeframeData] = {}
    rankings: dict[str, float] = {}

    for sym in symbols_found:
        try:
            data = _load_data(sym, data_dir, args.max_bars)
            data_map[sym] = data
            cfg = OrchestratorBacktestConfig(
                symbol=sym,
                initial_balance=Decimal(str(args.initial_balance)),
                warmup_bars=args.warmup_bars,
                enable_strategy_router=True,
            )
            factories = _make_strategy_factories(sym, smc_params={})
            result = await phase1_baseline(sym, data, cfg, factories)
            rankings[sym] = float(result.total_return_pct)
        except Exception as e:
            logger.warning("Phase 1 failed for %s: %s", sym, e)

    # Top-N by return
    top_symbols = sorted(rankings, key=rankings.__getitem__, reverse=True)[: args.top_n]
    logger.info("Top-%d pairs by return: %s", args.top_n, top_symbols)

    top_data_map = {s: data_map[s] for s in top_symbols if s in data_map}
    config = OrchestratorBacktestConfig(
        initial_balance=Decimal(str(args.initial_balance)) / max(len(top_symbols), 1),
        warmup_bars=args.warmup_bars,
        enable_strategy_router=True,
    )
    factories = _make_strategy_factories(top_symbols[0] if top_symbols else "BTC", smc_params={})
    output_dir = Path(args.output_dir) / f"auto_{datetime.now():%Y%m%d_%H%M%S}"

    p3 = await phase3_portfolio(
        symbols=top_symbols,
        data_map=top_data_map,
        per_pair_config=config,
        factories=factories,
        initial_capital=Decimal(str(args.initial_balance)),
    )
    _save_results(output_dir, "phase3_portfolio", p3.to_dict())
    _save_results(output_dir, "phase1_rankings", rankings)
    logger.info("Auto mode complete. Results in %s", output_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest V2.0 — multi-strategy orchestrator pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["single", "multi", "auto"],
        default="single",
        help="Pipeline mode (default: single)",
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol for single mode")
    parser.add_argument("--symbols", help="Comma-separated symbols for multi mode")
    parser.add_argument("--top-n", type=int, default=5, help="Top-N pairs for auto mode")
    parser.add_argument(
        "--data-dir",
        default="data/historical",
        help="Directory with historical CSV files (default: data/historical)",
    )
    parser.add_argument(
        "--max-bars",
        type=int,
        default=None,
        help="Limit M5 bars (for smoke tests, e.g. 1000)",
    )
    parser.add_argument(
        "--warmup-bars",
        type=int,
        default=500,
        help="Warmup bars before trading starts (default: 500)",
    )
    parser.add_argument(
        "--initial-balance",
        type=float,
        default=10000.0,
        help="Initial balance in USD (default: 10000)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel workers for optimization (default: 1)",
    )
    parser.add_argument(
        "--phases",
        help="Comma-separated phases to run, e.g. '1,2' (default: all)",
    )
    parser.add_argument(
        "--output-dir",
        default="results/backtest_v2",
        help="Output directory for JSON results",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger.info("Backtest V2.0 | mode=%s", args.mode)

    if args.mode == "single":
        asyncio.run(run_single(args))
    elif args.mode == "multi":
        asyncio.run(run_multi(args))
    elif args.mode == "auto":
        asyncio.run(run_auto(args))
    else:
        logger.error("Unknown mode: %s", args.mode)
        sys.exit(1)


if __name__ == "__main__":
    main()
