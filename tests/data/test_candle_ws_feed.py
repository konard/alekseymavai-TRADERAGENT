"""
Unit tests for CandleWSFeed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.data.candle_ws_feed import CandleWSFeed


def _make_feed(sandbox: bool = False) -> tuple[CandleWSFeed, AsyncMock]:
    hm = MagicMock()
    hm.upsert_candles = AsyncMock(return_value=1)
    feed = CandleWSFeed(
        history_manager=hm,
        symbol="BTC/USDT",
        interval="5m",
        exchange_name="bybit",
        sandbox=sandbox,
    )
    return feed, hm


def _kline_msg(confirm: bool, ts_ms: int = 1_700_000_000_000) -> dict:
    return {
        "topic": "kline.5.BTCUSDT",
        "data": [
            {
                "start": ts_ms,
                "end": ts_ms + 300_000,
                "interval": "5",
                "open": "50000",
                "high": "51000",
                "low": "49500",
                "close": "50500",
                "volume": "100",
                "confirm": confirm,
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. Confirmed candle is saved to DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_candle_saved_to_db():
    feed, hm = _make_feed()
    msg = _kline_msg(confirm=True)
    await feed._on_message(msg)
    hm.upsert_candles.assert_awaited_once()
    call_args = hm.upsert_candles.call_args[0][0]
    assert len(call_args) == 1
    assert call_args[0]["symbol"] == "BTC/USDT"
    assert call_args[0]["interval"] == "5m"


# ---------------------------------------------------------------------------
# 2. Unconfirmed candle is NOT saved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfirmed_candle_not_saved():
    feed, hm = _make_feed()
    msg = _kline_msg(confirm=False)
    await feed._on_message(msg)
    hm.upsert_candles.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Pong / subscribe messages ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pong_message_ignored():
    feed, hm = _make_feed()
    await feed._on_message({"op": "pong"})
    await feed._on_message({"op": "subscribe", "success": True})
    hm.upsert_candles.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Multiple confirmed candles in one message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_confirmed_candles_saved():
    feed, hm = _make_feed()
    msg = {
        "topic": "kline.5.BTCUSDT",
        "data": [
            {
                "start": 1_700_000_000_000 + i * 300_000,
                "interval": "5",
                "open": "50000",
                "high": "51000",
                "low": "49500",
                "close": "50500",
                "volume": "100",
                "confirm": True,
            }
            for i in range(3)
        ],
    }
    await feed._on_message(msg)
    hm.upsert_candles.assert_awaited_once()
    saved = hm.upsert_candles.call_args[0][0]
    assert len(saved) == 3


# ---------------------------------------------------------------------------
# 5. Interval conversion
# ---------------------------------------------------------------------------


def test_to_bybit_interval():
    assert CandleWSFeed._to_bybit_interval("5m") == "5"
    assert CandleWSFeed._to_bybit_interval("1h") == "60"
    assert CandleWSFeed._to_bybit_interval("4h") == "240"
    assert CandleWSFeed._to_bybit_interval("1d") == "D"
    assert CandleWSFeed._to_bybit_interval("1w") == "W"


# ---------------------------------------------------------------------------
# 6. stop() cancels internal task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_sets_running_false():
    feed, _ = _make_feed()
    feed._running = True
    await feed.stop()
    assert feed._running is False
