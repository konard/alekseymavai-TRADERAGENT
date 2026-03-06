"""
Unit tests for HistoryManager — DB-first OHLCV cache.

Uses in-memory mocking of session_factory and exchange to avoid a real DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from bot.data.history_manager import HistoryManager

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_raw_ohlcv(n: int = 5, base_ts_ms: int = 1_700_000_000_000) -> list[list]:
    """Build synthetic raw OHLCV list."""
    return [
        [base_ts_ms + i * 3_600_000, 100.0, 105.0, 98.0, 102.0, 1000.0]
        for i in range(n)
    ]


def _make_db_rows(n: int = 5) -> list[tuple]:
    """Build synthetic DB result rows (time, open, high, low, close, volume)."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    import datetime as dt
    return [
        (
            base + dt.timedelta(hours=i),
            "100.0", "105.0", "98.0", "102.0", "1000.0",
        )
        for i in range(n)
    ]


def _make_session_factory(query_rows: list, count: int = 0):
    """Create a mock async_sessionmaker that returns the specified rows."""
    mock_result = MagicMock()
    mock_result.fetchall.return_value = query_rows
    mock_result.fetchone.return_value = (count,)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock()
    factory.return_value = mock_session
    factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    return factory


# ---------------------------------------------------------------------------
# 1. get_candles returns DataFrame with correct columns
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_candles_returns_dataframe_with_correct_columns():
    db_rows = _make_db_rows(5)
    factory = _make_session_factory(query_rows=db_rows, count=5)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[])

    hm = HistoryManager(session_factory=factory, exchange=exchange)

    # Pretend DB has 200 rows so no backfill needed
    with patch.object(hm, "_query_db", AsyncMock(return_value=db_rows)):
        with patch.object(hm, "_count_db", AsyncMock(return_value=200)):
            df = await hm.get_candles("BTC/USDT", "1h", limit=5)

    assert isinstance(df, pd.DataFrame)
    for col in ("time", "open", "high", "low", "close", "volume"):
        assert col in df.columns
    assert len(df) == 5


# ---------------------------------------------------------------------------
# 2. DB has sufficient data → no API call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reads_from_db_when_sufficient_data():
    db_rows = _make_db_rows(200)
    exchange = AsyncMock()

    hm = HistoryManager(session_factory=MagicMock(), exchange=exchange)

    with patch.object(hm, "_query_db", AsyncMock(return_value=db_rows)):
        with patch.object(hm, "_count_db", AsyncMock(return_value=200)):
            df = await hm.get_candles("BTC/USDT", "1h", limit=200)

    # Exchange should NOT have been called
    exchange.fetch_ohlcv.assert_not_called()
    assert len(df) == 200


# ---------------------------------------------------------------------------
# 3. DB empty → backfill from API
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfills_from_api_when_db_empty():
    raw = _make_raw_ohlcv(200)
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=raw)

    hm = HistoryManager(session_factory=MagicMock(), exchange=exchange)

    db_rows_after = _make_db_rows(200)

    # _query_db: first call returns empty (triggers backfill), second returns data
    query_call_count = {"n": 0}
    async def _fake_query(sym, iv, limit, since):
        if query_call_count["n"] == 0:
            query_call_count["n"] += 1
            return []
        return db_rows_after

    backfill_mock = AsyncMock(return_value=200)
    with patch.object(hm, "_query_db", side_effect=_fake_query):
        with patch.object(hm, "backfill", backfill_mock):
            df = await hm.get_candles("BTC/USDT", "1h", limit=200)
            # backfill was called because first _query_db returned empty
            backfill_mock.assert_awaited_once()

    assert len(df) == 200


# ---------------------------------------------------------------------------
# 4. upsert_candles is idempotent (same candles inserted twice → no error)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_is_idempotent():
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)

    exchange = AsyncMock()
    hm = HistoryManager(session_factory=factory, exchange=exchange)

    candles = [
        {
            "time": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "symbol": "BTC/USDT",
            "interval": "1h",
            "open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0, "volume": 1000.0,
        }
    ]

    # Insert twice — should not raise
    count1 = await hm.upsert_candles(candles)
    count2 = await hm.upsert_candles(candles)

    assert count1 == 1
    assert count2 == 1


# ---------------------------------------------------------------------------
# 5. get_candles returns empty DataFrame when no data at all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_candles_returns_empty_df_when_no_data():
    exchange = AsyncMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[])

    hm = HistoryManager(session_factory=MagicMock(), exchange=exchange)

    with patch.object(hm, "_query_db", AsyncMock(return_value=[])):
        with patch.object(hm, "_count_db", AsyncMock(return_value=0)):
            with patch.object(hm, "get_latest_ts", AsyncMock(return_value=None)):
                with patch.object(hm, "upsert_candles", AsyncMock(return_value=0)):
                    df = await hm.get_candles("BTC/USDT", "1h", limit=200)

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 0
    for col in ("time", "open", "high", "low", "close", "volume"):
        assert col in df.columns
