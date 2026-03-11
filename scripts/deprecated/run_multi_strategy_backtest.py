#!/usr/bin/env python3
"""
Multi-Strategy Backtest CLI Runner.

Runs Grid, DCA, Trend-Follower, and SMC strategies on the same data
(CSV or synthetic) and generates HTML comparison reports.

Usage:
    # Synthetic data:
    python scripts/run_multi_strategy_backtest.py --symbol ETH_USDT --days 30

    # CSV data:
    python scripts/run_multi_strategy_backtest.py --csv data/ETH_USDT_5m.csv --timeframe 5m

    # Single strategy:
    python scripts/run_multi_strategy_backtest.py --strategy smc --days 14
"""

import argparse
import asyncio
import logging
import sys
import traceback
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

# Ensure project root is on sys.path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

LOG_DIR = project_root / "logs"


def _setup_logging(timestamp: str, symbol: str) -> Path:
    """Configure logging to both console and file simultaneously."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    symbol_clean = symbol.replace("/", "_").replace(" ", "_")
    log_path = LOG_DIR / f"backtest_{symbol_clean}_{timestamp}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — DEBUG и выше (всё включая ошибки)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler — INFO и выше (не засорять терминал debug-логами)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    return log_path

from bot.tests.backtesting.multi_tf_data_loader import (  # noqa: E402
    MultiTimeframeDataLoader,
)
from bot.tests.backtesting.multi_tf_engine import (  # noqa: E402
    MultiTFBacktestConfig,
)
from bot.tests.backtesting.report_generator import (  # noqa: E402
    ReportGenerator,
)
from bot.tests.backtesting.strategy_comparison import (  # noqa: E402
    StrategyComparison,
)

REPORT_DIR = project_root / "docs" / "backtesting-reports" / "html"


def _build_strategies(names: list[str], balance: Decimal) -> list:
    """Instantiate strategy adapters by name."""
    strategies = []

    for name in names:
        if name == "grid":
            from bot.strategies.grid_adapter import GridAdapter

            strategies.append(GridAdapter(name="grid"))

        elif name == "dca":
            from bot.strategies.dca_adapter import DCAAdapter

            strategies.append(DCAAdapter(name="dca"))

        elif name == "trend_follower":
            from bot.strategies.trend_follower_adapter import TrendFollowerAdapter

            strategies.append(
                TrendFollowerAdapter(
                    initial_capital=balance,
                    name="trend-follower",
                    log_trades=False,
                )
            )

        elif name == "smc":
            from bot.strategies.smc.config import SMCConfig
            from bot.strategies.smc_adapter import SMCStrategyAdapter

            smc_config = SMCConfig()
            smc_config.risk_per_trade_pct = smc_config.risk_per_trade
            smc_config.max_position_size_usd = smc_config.max_position_size
            strategies.append(
                SMCStrategyAdapter(
                    config=smc_config,
                    account_balance=balance,
                    name="smc",
                )
            )

        else:
            print(f"Unknown strategy: {name}")
            sys.exit(1)

    return strategies


async def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Strategy Backtester")
    parser.add_argument(
        "--symbol", default="BTC_USDT", help="Trading pair (e.g. ETH_USDT)"
    )
    parser.add_argument(
        "--no-log-file", action="store_true",
        help="Disable saving logs to file (print to console only)",
    )
    parser.add_argument("--csv", default=None, help="Path to CSV file with OHLCV data")
    parser.add_argument(
        "--timeframe",
        default="5m",
        help="Base timeframe of CSV data (5m, 15m, 1h)",
    )
    parser.add_argument(
        "--days", type=int, default=14, help="Days of synthetic data to generate"
    )
    parser.add_argument(
        "--trend", default="up", choices=["up", "down", "sideways"],
        help="Trend for synthetic data",
    )
    parser.add_argument(
        "--balance", type=float, default=10000, help="Initial balance (USDT)"
    )
    parser.add_argument(
        "--warmup", type=int, default=60, help="Warmup bars before trading"
    )
    parser.add_argument(
        "--lookback", type=int, default=200,
        help="Rolling context window (bars per timeframe). "
             "SMC strategy requires at least 200 for proper swing detection.",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        help="Run single strategy (grid, dca, trend_follower, smc)",
    )
    args = parser.parse_args()

    symbol = args.symbol.replace("_", "/")
    balance = Decimal(str(args.balance))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Setup logging
    log_path = None
    if not args.no_log_file:
        log_path = _setup_logging(timestamp, symbol)
        print(f"Logging to: {log_path}")

    logger = logging.getLogger(__name__)
    logger.info("Backtest started — symbol=%s balance=%s", symbol, balance)

    # Load data
    loader = MultiTimeframeDataLoader()

    if args.csv:
        print(f"Loading CSV data from {args.csv} (base TF: {args.timeframe})...")
        data = loader.load_csv(args.csv, base_timeframe=args.timeframe)
    else:
        end_date = datetime(2024, 6, 1)
        start_date = end_date - timedelta(days=args.days)
        print(
            f"Generating {args.days} days of {args.trend} synthetic data "
            f"for {symbol}..."
        )
        data = loader.load(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            trend=args.trend,
        )

    print(
        f"  M5: {len(data.m5)} bars | M15: {len(data.m15)} | "
        f"H1: {len(data.h1)} | H4: {len(data.h4)} | D1: {len(data.d1)}"
    )

    # Build strategies
    if args.strategy:
        strategy_names = [args.strategy]
    else:
        strategy_names = ["grid", "dca", "trend_follower", "smc"]

    print(f"Strategies: {', '.join(strategy_names)}")
    strategies = _build_strategies(strategy_names, balance)

    # Configure backtest
    config = MultiTFBacktestConfig(
        symbol=symbol,
        initial_balance=balance,
        warmup_bars=args.warmup,
        lookback=args.lookback,
    )

    # Run comparison
    comparison = StrategyComparison(config=config)
    print("\nRunning backtests...")

    comp_result = await comparison.run(strategies, data)

    # Print text report
    text_report = StrategyComparison.format_report(comp_result)
    print(text_report)
    logger.info("Backtest results:\n%s", text_report)

    # Generate HTML reports
    gen = ReportGenerator()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    symbol_clean = symbol.replace("/", "_")

    # Individual strategy reports
    for name, result in comp_result.results.items():
        filename = f"{symbol_clean}_{name}_{timestamp}.html"
        html = gen.generate(result)
        path = gen.save(html, REPORT_DIR / filename)
        print(f"  Report saved: {path}")
        logger.info("HTML report saved: %s", path)

    # Comparison report
    if len(comp_result.results) > 1:
        comp_filename = f"{symbol_clean}_comparison_{timestamp}.html"
        comp_html = gen.generate_comparison(comp_result)
        comp_path = gen.save(comp_html, REPORT_DIR / comp_filename)
        print(f"  Comparison report: {comp_path}")
        logger.info("Comparison report saved: %s", comp_path)

    logger.info("Backtest finished successfully")
    if log_path:
        print(f"\nFull log saved: {log_path}")
    print("\nDone.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning("Interrupted by user (Ctrl+C)")
        sys.exit(0)
    except Exception:
        logging.getLogger(__name__).critical(
            "Unhandled exception:\n%s", traceback.format_exc()
        )
        sys.exit(1)
