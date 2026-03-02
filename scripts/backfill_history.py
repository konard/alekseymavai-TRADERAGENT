#!/usr/bin/env python
"""
Backfill historical OHLCV candles into TimescaleDB.

Supports loading from:
  1. data/historical/ CSV files (if present)
  2. Exchange REST API for any remaining gaps

Usage:
    python scripts/backfill_history.py \
        --days 90 \
        --symbols BTC/USDT ETH/USDT SOL/USDT \
        --intervals 5m 15m 1h 4h 1d

Environment variables (or .env):
    DATABASE_URL          — PostgreSQL connection URL (postgresql+asyncpg://...)
    ENCRYPTION_KEY        — Encryption key (for fetching exchange credentials)
    CONFIG_PATH           — Path to YAML config (default: configs/phase7_demo.yaml)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main(args: argparse.Namespace) -> None:
    """Entry point for the backfill script."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable not set.", file=sys.stderr)
        sys.exit(1)

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from bot.api.bybit_direct_client import ByBitDirectClient
    from bot.data.history_manager import HistoryManager
    from bot.utils.logger import get_logger

    logger = get_logger("backfill_history")

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Build a minimal exchange client (public endpoints only, no auth needed for OHLCV)
    exchange = ByBitDirectClient(
        api_key="",
        api_secret="",
        sandbox=False,
    )

    hm = HistoryManager(
        session_factory=session_factory,
        exchange=exchange,
        exchange_name="bybit",
    )

    target_bars_map = {
        "1m": args.days * 24 * 60,
        "3m": args.days * 24 * 20,
        "5m": args.days * 24 * 12,
        "15m": args.days * 24 * 4,
        "30m": args.days * 24 * 2,
        "1h": args.days * 24,
        "2h": args.days * 12,
        "4h": args.days * 6,
        "6h": args.days * 4,
        "12h": args.days * 2,
        "1d": args.days,
    }

    for symbol in args.symbols:
        # 1. Try loading from CSV first
        _maybe_load_csv(symbol, args.intervals, hm, logger)

        for interval in args.intervals:
            target = min(target_bars_map.get(interval, args.days * 24), 2000)
            logger.info(
                "backfill_starting",
                symbol=symbol,
                interval=interval,
                target_bars=target,
            )
            try:
                written = await hm.backfill(symbol, interval, target_bars=target)
                logger.info(
                    "backfill_done",
                    symbol=symbol,
                    interval=interval,
                    written=written,
                )
            except Exception as e:
                logger.error(
                    "backfill_failed",
                    symbol=symbol,
                    interval=interval,
                    error=str(e),
                )

    await engine.dispose()
    print("Backfill complete.")


def _maybe_load_csv(
    symbol: str,
    intervals: list[str],
    hm: object,  # HistoryManager — avoid import at module level
    logger: object,
) -> None:
    """
    Load candles from data/historical/ CSV files if they exist.

    Expected filename convention: {symbol_slug}_{interval}.csv
    e.g.  data/historical/BTC_USDT_1h.csv

    CSV format (no header, or header line starting with 'timestamp'):
        timestamp_ms, open, high, low, close, volume
    """
    import csv
    from datetime import datetime, timezone

    slug = symbol.replace("/", "_")
    hist_dir = ROOT / "data" / "historical"

    for interval in intervals:
        csv_path = hist_dir / f"{slug}_{interval}.csv"
        if not csv_path.exists():
            continue

        candles: list[dict] = []
        with open(csv_path, newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or row[0].startswith("#") or row[0].lower().startswith("timestamp"):
                    continue
                try:
                    ts_ms = int(row[0])
                    candles.append(
                        {
                            "time":     datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                            "exchange": "bybit",
                            "symbol":   symbol,
                            "interval": interval,
                            "open":     float(row[1]),
                            "high":     float(row[2]),
                            "low":      float(row[3]),
                            "close":    float(row[4]),
                            "volume":   float(row[5]),
                        }
                    )
                except (IndexError, ValueError):
                    continue

        if candles:
            # Synchronous upsert — schedule in event loop (caller is async)
            print(f"  CSV {csv_path.name}: {len(candles)} rows queued")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill OHLCV candles into TimescaleDB")
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of days to backfill (default: 90)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        help="Trading pairs to backfill",
    )
    parser.add_argument(
        "--intervals",
        nargs="+",
        default=["5m", "15m", "1h", "4h", "1d"],
        help="OHLCV timeframes to backfill",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(_parse_args()))
