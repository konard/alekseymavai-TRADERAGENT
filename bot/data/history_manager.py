"""
HistoryManager — persistent OHLCV cache backed by TimescaleDB.

Read path:
    1. Query ``candles`` hypertable for the requested (symbol, interval, limit).
    2. If data is insufficient (< limit), backfill from the exchange REST API.
    3. Return a DataFrame with columns [time, open, high, low, close, volume].

Write path:
    ``upsert_candles`` performs INSERT … ON CONFLICT DO UPDATE, making writes
    idempotent (safe for repeated calls / WebSocket duplicates).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INTERVAL_TO_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}

# Max candles fetched per REST call (Bybit limit)
_REST_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# HistoryManager
# ---------------------------------------------------------------------------


class HistoryManager:
    """
    Transparent OHLCV cache: DB-first, REST API backfill.

    Args:
        session_factory: SQLAlchemy ``async_sessionmaker`` (already initialised).
        exchange: Any object with an async ``fetch_ohlcv(symbol, timeframe, limit, since)``
                  method (CCXT-compatible).
        exchange_name: Tag stored in the ``exchange`` column of ``candles``.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        exchange: Any,
        exchange_name: str = "bybit",
    ) -> None:
        self._session_factory = session_factory
        self._exchange = exchange
        self._exchange_name = exchange_name

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    async def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        since: datetime | None = None,
    ) -> pd.DataFrame:
        """
        Return up to *limit* closed candles for (symbol, interval).

        1. Tries the DB first.
        2. Backfills from the exchange if DB row count < limit.
        3. Returns DataFrame with columns: time, open, high, low, close, volume.
        """
        rows = await self._query_db(symbol, interval, limit, since)

        if len(rows) < limit:
            await self.backfill(symbol, interval, target_bars=limit)
            rows = await self._query_db(symbol, interval, limit, since)

        if not rows:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"], utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col])
        return df.sort_values("time").reset_index(drop=True)

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    async def upsert_candles(self, candles: list[dict]) -> int:
        """
        Upsert a list of candle dicts into the ``candles`` hypertable.

        Each dict must have keys: time (datetime), open, high, low, close, volume.
        ``exchange`` and ``symbol`` can be overridden per-candle; otherwise the
        instance-level ``exchange_name`` and the candle's ``symbol`` key are used.

        Returns:
            Number of rows inserted/updated.
        """
        if not candles:
            return 0

        stmt = text(
            """
            INSERT INTO candles (time, exchange, symbol, interval, open, high, low, close, volume)
            VALUES (:time, :exchange, :symbol, :interval,
                    :open, :high, :low, :close, :volume)
            ON CONFLICT (time, exchange, symbol, interval) DO UPDATE
                SET open   = EXCLUDED.open,
                    high   = EXCLUDED.high,
                    low    = EXCLUDED.low,
                    close  = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """
        )

        async with self._session_factory() as session:
            for candle in candles:
                await session.execute(
                    stmt,
                    {
                        "time":     candle["time"],
                        "exchange": candle.get("exchange", self._exchange_name),
                        "symbol":   candle["symbol"],
                        "interval": candle["interval"],
                        "open":     float(candle["open"]),
                        "high":     float(candle["high"]),
                        "low":      float(candle["low"]),
                        "close":    float(candle["close"]),
                        "volume":   float(candle["volume"]),
                    },
                )
            await session.commit()

        return len(candles)

    async def backfill(
        self,
        symbol: str,
        interval: str,
        target_bars: int = 2000,
        since: datetime | None = None,
    ) -> int:
        """
        Paginated REST backfill until *target_bars* candles exist in DB.

        Each REST call fetches at most ``_REST_PAGE_SIZE`` (200) candles.
        Stops when:
          - DB already has ≥ target_bars rows, OR
          - The exchange returns fewer rows than the page size (start of history).

        Args:
            symbol: Trading pair.
            interval: OHLCV timeframe string (e.g. "1h", "5m").
            target_bars: Desired number of rows in DB.
            since: Optional starting timestamp (defaults to "go as far back as needed").

        Returns:
            Total rows written.
        """
        interval_secs = _INTERVAL_TO_SECONDS.get(interval, 3600)
        total_written = 0

        # Determine where to start fetching
        if since is None:
            # Start from the newest candle in DB and go backward
            latest_ts = await self.get_latest_ts(symbol, interval)
            if latest_ts is not None:
                # fetch forward from latest
                since = latest_ts
            else:
                # no data at all: compute approximate start from target_bars
                oldest_ts_ms = (
                    int(datetime.now(timezone.utc).timestamp() * 1000)
                    - target_bars * interval_secs * 1000
                )
                since = datetime.fromtimestamp(oldest_ts_ms / 1000, tz=timezone.utc)

        current_since = since
        while True:
            db_count = await self._count_db(symbol, interval)
            if db_count >= target_bars:
                break

            since_ms = int(current_since.timestamp() * 1000)
            try:
                raw = await self._exchange.fetch_ohlcv(
                    symbol=symbol,
                    timeframe=interval,
                    limit=_REST_PAGE_SIZE,
                    since=since_ms,
                )
            except Exception as e:
                logger.error(
                    "backfill_fetch_error",
                    symbol=symbol,
                    interval=interval,
                    error=str(e),
                )
                break

            if not raw:
                break

            candles = [
                {
                    "time":     datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                    "exchange": self._exchange_name,
                    "symbol":   symbol,
                    "interval": interval,
                    "open":     row[1],
                    "high":     row[2],
                    "low":      row[3],
                    "close":    row[4],
                    "volume":   row[5],
                }
                for row in raw
            ]

            written = await self.upsert_candles(candles)
            total_written += written

            # Advance cursor to the last candle timestamp + 1 interval
            last_ts_ms = raw[-1][0]
            current_since = datetime.fromtimestamp(
                (last_ts_ms + interval_secs * 1000) / 1000, tz=timezone.utc
            )

            if len(raw) < _REST_PAGE_SIZE:
                # reached the start of available history
                break

        logger.info(
            "backfill_done",
            symbol=symbol,
            interval=interval,
            total_written=total_written,
        )
        return total_written

    async def get_latest_ts(self, symbol: str, interval: str) -> datetime | None:
        """Return the most recent candle timestamp in DB, or None."""
        stmt = text(
            """
            SELECT MAX(time) FROM candles
            WHERE exchange = :exchange AND symbol = :symbol AND interval = :interval
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                stmt,
                {
                    "exchange": self._exchange_name,
                    "symbol":   symbol,
                    "interval": interval,
                },
            )
            row = result.fetchone()
            return row[0] if row and row[0] is not None else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _query_db(
        self,
        symbol: str,
        interval: str,
        limit: int,
        since: datetime | None,
    ) -> list[tuple]:
        """Query candles from DB ordered oldest→newest (last *limit* rows)."""
        if since is not None:
            stmt = text(
                """
                SELECT time, open, high, low, close, volume
                FROM candles
                WHERE exchange = :exchange AND symbol = :symbol AND interval = :interval
                      AND time >= :since
                ORDER BY time DESC
                LIMIT :limit
                """
            )
            params: dict = {
                "exchange": self._exchange_name,
                "symbol":   symbol,
                "interval": interval,
                "since":    since,
                "limit":    limit,
            }
        else:
            stmt = text(
                """
                SELECT time, open, high, low, close, volume
                FROM candles
                WHERE exchange = :exchange AND symbol = :symbol AND interval = :interval
                ORDER BY time DESC
                LIMIT :limit
                """
            )
            params = {
                "exchange": self._exchange_name,
                "symbol":   symbol,
                "interval": interval,
                "limit":    limit,
            }

        async with self._session_factory() as session:
            result = await session.execute(stmt, params)
            return list(result.fetchall())

    async def _count_db(self, symbol: str, interval: str) -> int:
        """Count rows in DB for (symbol, interval)."""
        stmt = text(
            """
            SELECT COUNT(*) FROM candles
            WHERE exchange = :exchange AND symbol = :symbol AND interval = :interval
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                stmt,
                {
                    "exchange": self._exchange_name,
                    "symbol":   symbol,
                    "interval": interval,
                },
            )
            row = result.fetchone()
            return int(row[0]) if row else 0
