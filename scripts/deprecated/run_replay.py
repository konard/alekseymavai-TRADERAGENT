#!/usr/bin/env python3
"""
Accelerated replay — run the real BotOrchestrator against historical data.

Usage::

    # Full 1-year replay (default)
    python scripts/run_replay.py

    # Quick smoke test (first 200 candles)
    python scripts/run_replay.py --candles 200

    # Custom symbol and initial balance
    python scripts/run_replay.py --symbol BTC/USDT --balance 50000

Downloads OHLCV candles via ccxt (cached to data/), then runs the actual
BotOrchestrator with a mock exchange, mock DB, and mock Redis.  Time is
accelerated so that 1 year of data completes in minutes.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time as _real_time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bot.config.schemas import (  # noqa: E402
    BotConfig,
    DCAConfig,
    ExchangeConfig,
    GridConfig,
    RiskManagementConfig,
    StrategyType,
)
from bot.replay.bug_detector import BugDetector  # noqa: E402
from bot.replay.mock_db import create_mock_db  # noqa: E402
from bot.replay.mock_redis import MockRedis  # noqa: E402
from bot.replay.replay_exchange import ReplayExchangeClient  # noqa: E402
from bot.replay.simulated_clock import SimulatedClock, patch_time  # noqa: E402


# =========================================================================
# Data download (via ccxt)
# =========================================================================

def download_candles(
    symbol: str,
    timeframe: str = "5m",
    days: int = 365,
    cache_dir: str = "data",
) -> list[dict]:
    """Download OHLCV candles from Bybit via ccxt. Caches to JSON file."""
    safe_symbol = symbol.replace("/", "_")
    cache_path = Path(PROJECT_ROOT) / cache_dir / f"{safe_symbol}_{timeframe}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists():
        print(f"  Loading cached candles from {cache_path}")
        with open(cache_path) as f:
            candles = json.load(f)
        print(f"  Loaded {len(candles)} candles from cache")
        return candles

    print(f"  Downloading {days} days of {symbol} {timeframe} candles from Bybit...")

    import ccxt

    exchange = ccxt.bybit({"enableRateLimit": True})
    since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000

    all_candles = []
    tf_ms = _timeframe_ms(timeframe)
    batch_size = 1000

    while since < exchange.milliseconds():
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=batch_size)
        if not ohlcv:
            break
        for row in ohlcv:
            all_candles.append({
                "timestamp": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
            })
        since = ohlcv[-1][0] + tf_ms
        print(f"    Downloaded {len(all_candles)} candles...", end="\r")

    # Deduplicate by timestamp
    seen = set()
    unique = []
    for c in all_candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)
    unique.sort(key=lambda c: c["timestamp"])

    print(f"\n  Total: {len(unique)} candles")

    with open(cache_path, "w") as f:
        json.dump(unique, f)
    print(f"  Cached to {cache_path}")

    return unique


def _timeframe_ms(tf: str) -> int:
    mapping = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000,
        "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
        "4h": 14_400_000, "1d": 86_400_000,
    }
    return mapping.get(tf, 300_000)


# =========================================================================
# Bot configuration builder
# =========================================================================

def build_grid_config(symbol: str, candles: list[dict]) -> BotConfig:
    """Create a BotConfig for grid+DCA replay on the given symbol."""
    # Derive price range from candle data
    prices = [c["close"] for c in candles]
    price_min = min(prices)
    price_max = max(prices)
    price_mid = (price_min + price_max) / 2

    # Grid: cover the middle 60% of the price range
    grid_lower = price_mid - (price_max - price_min) * 0.3
    grid_upper = price_mid + (price_max - price_min) * 0.3

    # Ensure grid makes sense
    grid_lower = max(grid_lower, price_min * 0.9)
    grid_upper = min(grid_upper, price_max * 1.1)

    return BotConfig(
        version=1,
        name="replay_bot",
        symbol=symbol,
        strategy=StrategyType.HYBRID,
        exchange=ExchangeConfig(
            exchange_id="bybit",
            credentials_name="replay_fake",
            sandbox=True,
        ),
        grid=GridConfig(
            enabled=True,
            upper_price=Decimal(str(round(grid_upper, 4))),
            lower_price=Decimal(str(round(grid_lower, 4))),
            grid_levels=10,
            amount_per_grid=Decimal("50"),
            profit_per_grid=Decimal("0.01"),
        ),
        dca=DCAConfig(
            enabled=True,
            trigger_percentage=Decimal("0.05"),
            amount_per_step=Decimal("100"),
            max_steps=5,
            take_profit_percentage=Decimal("0.08"),
        ),
        risk_management=RiskManagementConfig(
            max_position_size=Decimal("5000"),
            stop_loss_percentage=Decimal("0.2"),
            max_daily_loss=Decimal("500"),
            min_order_size=Decimal("1"),
        ),
        dry_run=False,
        auto_start=False,
    )


# =========================================================================
# Main replay loop
# =========================================================================

async def run_replay(
    symbol: str,
    initial_balance: Decimal,
    max_candles: int | None,
    days: int,
) -> dict:
    """Execute the full replay and return a report dict."""

    print("=" * 60)
    print(f"  ACCELERATED REPLAY — {symbol}")
    print("=" * 60)

    # 1. Download / load candles
    print("\n[1/5] Loading candle data...")
    candles = download_candles(symbol, timeframe="5m", days=days)

    if max_candles and max_candles < len(candles):
        candles = candles[:max_candles]
        print(f"  Truncated to {len(candles)} candles (--candles flag)")

    if len(candles) < 10:
        print("ERROR: Not enough candle data for replay")
        return {"error": "insufficient_data"}

    # 2. Build configuration
    print("\n[2/5] Building bot configuration...")
    config = build_grid_config(symbol, candles)
    print(f"  Strategy: {config.strategy}")
    print(f"  Grid: {config.grid.lower_price} — {config.grid.upper_price} ({config.grid.grid_levels} levels)")
    print(f"  DCA: trigger={config.dca.trigger_percentage}, step={config.dca.amount_per_step}")

    # 3. Set up replay components
    print("\n[3/5] Initializing replay components...")
    start_ts = candles[0]["timestamp"] / 1000.0
    clock = SimulatedClock(start_time=start_ts)
    exchange = ReplayExchangeClient(
        candles=candles,
        clock=clock,
        initial_balance=initial_balance,
        symbol=symbol,
    )
    db = await create_mock_db()
    detector = BugDetector(exchange, initial_balance=initial_balance, check_interval=100)
    mock_redis = MockRedis()

    print(f"  Clock start: {clock.now()}")
    print(f"  Initial balance: {initial_balance} USDT")
    print(f"  Candles: {len(candles)}")

    # 4. Run the orchestrator
    print("\n[4/5] Running replay...")

    # Save real wall-clock function BEFORE patching
    _wall_monotonic = _real_time.monotonic
    wall_start = _wall_monotonic()

    # Patch redis.from_url so the orchestrator uses our mock
    import redis.asyncio as redis_mod

    from bot.orchestrator.bot_orchestrator import BotOrchestrator

    with patch_time(clock), \
         patch.object(redis_mod, "from_url", return_value=mock_redis):

        orchestrator = BotOrchestrator(
            bot_config=config,
            exchange_client=exchange,
            db_manager=db,
            redis_url="redis://fake:6379",
        )

        # Speed up replay: widen intervals for non-critical monitors
        orchestrator._regime_check_interval = 600.0   # 10 min sim-time
        orchestrator._state_save_interval = 300.0      # 5 min sim-time

        try:
            await orchestrator.initialize()
            await orchestrator.start()

            progress_interval = max(1, len(candles) // 20)  # ~5% steps
            last_progress = 0

            while exchange.has_more_candles() and orchestrator._running:
                await asyncio.sleep(0.001)  # yields control, clock advances 1ms
                detector.check_periodic(orchestrator)

                # Progress reporting (wall-clock based)
                processed = exchange.processed_candles
                if processed - last_progress >= progress_interval:
                    pct = processed / len(candles) * 100
                    elapsed = _wall_monotonic() - wall_start
                    print(f"    {pct:5.1f}%  candle {processed}/{len(candles)}  "
                          f"wall={elapsed:.1f}s  sim_date={clock.now().date()}", flush=True)
                    last_progress = processed

        except Exception as e:
            detector.record_exception(e, context="replay_main_loop")
            print(f"\n  EXCEPTION during replay: {type(e).__name__}: {e}")

        finally:
            try:
                orchestrator._running = False
                await orchestrator.stop()
            except Exception as stop_exc:
                detector.record_exception(stop_exc, context="orchestrator_stop")

    wall_elapsed = _wall_monotonic() - wall_start

    # Clean up DB engine to avoid aiosqlite hang on exit
    try:
        await db._engine.dispose()
    except Exception:
        pass

    # 5. Generate report
    print("\n[5/5] Generating report...")
    stats = exchange.get_statistics()
    bug_report = detector.get_report()

    report = {
        "symbol": symbol,
        "candles_total": len(candles),
        "candles_processed": stats["candles_processed"],
        "wall_time_seconds": round(wall_elapsed, 2),
        "simulated_days": round((candles[-1]["timestamp"] - candles[0]["timestamp"]) / 86_400_000, 1),
        "initial_balance": float(initial_balance),
        "final_free_balance": stats["free_balance"],
        "final_base_balance": stats["base_balance"],
        "total_fills": stats["total_fills"],
        "open_orders_remaining": stats["open_orders"],
        "api_requests": stats["total_requests"],
        "bugs": {
            "exceptions": bug_report["total_exceptions"],
            "anomalies": bug_report["total_anomalies"],
            "severity_counts": bug_report["severity_counts"],
        },
        "exception_details": bug_report["exceptions"],
        "anomaly_details": bug_report["anomalies"],
    }

    _print_report(report)
    return report


def _print_report(report: dict) -> None:
    """Pretty-print the replay report."""
    print("\n" + "=" * 60)
    print("  REPLAY REPORT")
    print("=" * 60)
    print(f"  Symbol:              {report['symbol']}")
    print(f"  Candles:             {report['candles_processed']}/{report['candles_total']}")
    print(f"  Simulated days:      {report['simulated_days']}")
    print(f"  Wall-clock time:     {report['wall_time_seconds']}s")
    print(f"  API requests:        {report['api_requests']}")
    print(f"  Total fills:         {report['total_fills']}")
    print(f"  Open orders left:    {report['open_orders_remaining']}")
    print()
    print(f"  Initial balance:     {report['initial_balance']} USDT")
    print(f"  Final free balance:  {report['final_free_balance']:.4f} USDT")
    print(f"  Final base balance:  {report['final_base_balance']:.6f}")
    print()

    bugs = report["bugs"]
    if bugs["exceptions"] == 0 and bugs["anomalies"] == 0:
        print("  BUGS DETECTED:       NONE — clean run!")
    else:
        print(f"  EXCEPTIONS:          {bugs['exceptions']}")
        print(f"  ANOMALIES:           {bugs['anomalies']}")
        for sev, count in bugs["severity_counts"].items():
            print(f"    {sev}: {count}")

        if report["exception_details"]:
            print("\n  --- Exception Details ---")
            for exc in report["exception_details"]:
                print(f"    [{exc['exception_type']}] candle={exc['candle']}: {exc['message']}")
                if exc.get("context"):
                    print(f"      context: {exc['context']}")

        if report["anomaly_details"]:
            print("\n  --- Anomaly Details (first 20) ---")
            for a in report["anomaly_details"][:20]:
                print(f"    [{a['severity']}] candle={a['candle']}: {a['code']}")

    print("=" * 60)


# =========================================================================
# CLI
# =========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Run accelerated replay of BotOrchestrator against historical data"
    )
    parser.add_argument(
        "--symbol", default="XRP/USDT",
        help="Trading pair symbol (default: XRP/USDT)",
    )
    parser.add_argument(
        "--balance", type=float, default=10000,
        help="Initial USDT balance (default: 10000)",
    )
    parser.add_argument(
        "--candles", type=int, default=None,
        help="Limit number of candles (default: all available, ~105k for 1 year)",
    )
    parser.add_argument(
        "--days", type=int, default=365,
        help="Days of historical data to download (default: 365)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Save report to JSON file",
    )
    args = parser.parse_args()

    report = asyncio.run(run_replay(
        symbol=args.symbol,
        initial_balance=Decimal(str(args.balance)),
        max_candles=args.candles,
        days=args.days,
    ))

    if args.output and "error" not in report:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nReport saved to {out_path}")

    # Exit with non-zero if bugs were found
    bugs = report.get("bugs", {})
    if bugs.get("exceptions", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
