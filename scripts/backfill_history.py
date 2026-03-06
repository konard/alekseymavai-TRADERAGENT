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

    data_dir = Path(args.data_dir).resolve() if args.data_dir else ROOT / "data" / "historical"

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
        # 1. Load from CSV files
        await _maybe_load_csv(symbol, args.intervals, hm, logger, data_dir=data_dir)

        if args.csv_only:
            continue

        # 2. REST backfill for any remaining gaps
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


async def _maybe_load_csv(
    symbol: str,
    intervals: list[str],
    hm: object,  # HistoryManager
    logger: object,
    data_dir: Path | None = None,
) -> None:
    """
    Load candles from CSV files into TimescaleDB.

    Searches for files using multiple naming conventions:
      - {SLUG}_{interval}.csv          e.g. BTC_USDT_5m.csv
      - bybit_{SLUG}_{interval}.csv    e.g. bybit_BTC_USDT_5m.csv
      - {BARE}_{interval}.csv          e.g. BTCUSDT_5m.csv
      - bybit_{BARE}_{interval}.csv    e.g. bybit_BTCUSDT_5m.csv

    CSV format — auto-detects column layout from header:
      timestamp, [datetime,] open, high, low, close, volume
    """
    import csv
    from datetime import datetime, timezone

    slug = symbol.replace("/", "_")          # BTC/USDT → BTC_USDT
    bare = symbol.replace("/", "")           # BTC/USDT → BTCUSDT
    hist_dir = data_dir if data_dir is not None else ROOT / "data" / "historical"

    for interval in intervals:
        # Try all naming conventions, use the first match
        candidates = [
            hist_dir / f"{slug}_{interval}.csv",
            hist_dir / f"bybit_{slug}_{interval}.csv",
            hist_dir / f"{bare}_{interval}.csv",
            hist_dir / f"bybit_{bare}_{interval}.csv",
        ]
        csv_path = next((p for p in candidates if p.exists()), None)
        if csv_path is None:
            continue

        # Detect column layout from header
        # Supported: timestamp,[datetime,]open,high,low,close,volume
        ohlcv_offset = 1  # default: no datetime column
        with open(csv_path, newline="") as fh:
            first = fh.readline().strip()
        if first.lower().startswith("timestamp"):
            cols = [c.strip().lower() for c in first.split(",")]
            if len(cols) > 1 and cols[1] in ("datetime", "date", "time"):
                ohlcv_offset = 2  # skip timestamp + datetime

        candles: list[dict] = []
        with open(csv_path, newline="") as fh:
            reader = csv.reader(fh)
            for row in reader:
                if not row or row[0].startswith("#") or row[0].lower().startswith("timestamp"):
                    continue
                try:
                    ts_ms = int(row[0])
                    o = ohlcv_offset
                    candles.append(
                        {
                            "time":     datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                            "exchange": "bybit",
                            "symbol":   symbol,
                            "interval": interval,
                            "open":     float(row[o]),
                            "high":     float(row[o + 1]),
                            "low":      float(row[o + 2]),
                            "close":    float(row[o + 3]),
                            "volume":   float(row[o + 4]),
                        }
                    )
                except (IndexError, ValueError):
                    continue

        if not candles:
            continue

        # Upsert in batches of 10 000 rows to avoid huge transactions
        batch_size = 10_000
        total_written = 0
        for i in range(0, len(candles), batch_size):
            batch = candles[i : i + batch_size]
            written = await hm.upsert_candles(batch)
            total_written += written

        print(f"  CSV {csv_path.name}: {total_written}/{len(candles)} rows upserted")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill OHLCV candles into TimescaleDB")
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of days to backfill via REST (default: 90). Ignored with --csv-only.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        help="Trading pairs to backfill (e.g. BTC/USDT ETH/USDT)",
    )
    parser.add_argument(
        "--intervals",
        nargs="+",
        default=["5m", "15m", "1h", "4h", "1d"],
        help="OHLCV timeframes to backfill (default: 5m 15m 1h 4h 1d)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Override directory for CSV files (default: data/historical/)",
    )
    parser.add_argument(
        "--csv-only",
        action="store_true",
        help="Load from CSV only — skip REST API backfill. "
             "Use this to bulk-import historical CSVs without exchange API calls.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(_parse_args()))
