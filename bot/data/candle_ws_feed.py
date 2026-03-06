"""
CandleWSFeed — WebSocket kline subscriber for Bybit.

Subscribes to ``kline.{interval}.{symbol}`` topics on the Bybit public
WebSocket (wss://stream.bybit.com/v5/public/linear).

When a **confirmed** (closed) candle arrives (``confirm == True``), it is
immediately upserted into the TimescaleDB hypertable via HistoryManager.

Usage (inside BotOrchestrator.start()):

    self._candle_ws_feed = CandleWSFeed(
        history_manager=self.history_manager,
        symbol=self.config.symbol,
        interval="5m",
    )
    asyncio.create_task(self._candle_ws_feed.run())
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from bot.data.history_manager import HistoryManager
from bot.utils.logger import get_logger

logger = get_logger(__name__)

# Bybit public linear perpetual WebSocket endpoint
_BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
_BYBIT_DEMO_WS_URL = "wss://stream-demo.bybit.com/v5/public/linear"

# Reconnect delay on connection loss
_RECONNECT_DELAY = 5  # seconds
_PING_INTERVAL = 20  # seconds


class CandleWSFeed:
    """
    Long-running WebSocket feed that persists confirmed klines to DB.

    Args:
        history_manager: HistoryManager for upsert.
        symbol: Trading pair (e.g. "BTC/USDT").
        interval: Bybit kline interval string (e.g. "5", "15", "60", "D").
        exchange_name: Exchange tag for DB column (default "bybit").
        sandbox: If True, connect to demo WebSocket.
    """

    def __init__(
        self,
        history_manager: HistoryManager,
        symbol: str,
        interval: str,
        exchange_name: str = "bybit",
        sandbox: bool = False,
    ) -> None:
        self._hm = history_manager
        self._symbol = symbol
        self._interval = interval
        self._exchange_name = exchange_name
        self._ws_url = _BYBIT_DEMO_WS_URL if sandbox else _BYBIT_WS_URL
        self._running = False
        self._task: asyncio.Task | None = None

        # Convert CCXT-style intervals to Bybit WS format
        # e.g. "5m" → "5", "1h" → "60", "1d" → "D"
        self._bybit_interval = self._to_bybit_interval(interval)
        # Convert "BTC/USDT" → "BTCUSDT"
        self._bybit_symbol = symbol.replace("/", "")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the WebSocket feed.  Reconnects automatically on errors."""
        self._running = True
        logger.info(
            "candle_ws_feed_starting",
            symbol=self._symbol,
            interval=self._interval,
        )

        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("candle_ws_error", error=str(e), exc_info=True)
                if self._running:
                    await asyncio.sleep(_RECONNECT_DELAY)

        logger.info("candle_ws_feed_stopped")

    async def stop(self) -> None:
        """Signal the feed to stop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _connect_and_listen(self) -> None:
        """Open a single WebSocket session and process messages."""
        try:
            import websockets  # noqa: F401  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("websockets_not_installed", hint="pip install websockets")
            await asyncio.sleep(60)
            return

        topic = f"kline.{self._bybit_interval}.{self._bybit_symbol}"
        subscribe_msg = json.dumps({"op": "subscribe", "args": [topic]})

        async with websockets.connect(self._ws_url) as ws:
            logger.info("candle_ws_connected", url=self._ws_url, topic=topic)
            await ws.send(subscribe_msg)

            # Ping loop keeps the connection alive
            async def _pinger() -> None:
                while True:
                    await asyncio.sleep(_PING_INTERVAL)
                    try:
                        await ws.send(json.dumps({"op": "ping"}))
                    except Exception:
                        break

            ping_task = asyncio.create_task(_pinger())
            try:
                async for raw_msg in ws:
                    if not self._running:
                        break
                    try:
                        msg = json.loads(raw_msg)
                        await self._on_message(msg)
                    except Exception as e:
                        logger.error("candle_ws_msg_error", error=str(e))
            finally:
                ping_task.cancel()

    async def _on_message(self, msg: dict) -> None:
        """Handle a single parsed WebSocket message."""
        # Bybit kline message structure:
        # {
        #   "topic": "kline.5.BTCUSDT",
        #   "data": [{
        #       "start": 1700000000000, "end": ..., "interval": "5",
        #       "open": "...", "close": "...", "high": "...", "low": "...",
        #       "volume": "...", "confirm": true, ...
        #   }]
        # }
        if msg.get("op") in ("pong", "subscribe"):
            return

        data_list = msg.get("data", [])
        if not isinstance(data_list, list):
            return

        candles_to_save: list[dict] = []

        for kline in data_list:
            if not kline.get("confirm", False):
                # Candle not yet closed — skip to avoid incomplete data
                continue

            try:
                ts_ms = int(kline["start"])
                candles_to_save.append(
                    {
                        "time": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                        "exchange": self._exchange_name,
                        "symbol": self._symbol,
                        "interval": self._interval,
                        "open": float(kline["open"]),
                        "high": float(kline["high"]),
                        "low": float(kline["low"]),
                        "close": float(kline["close"]),
                        "volume": float(kline["volume"]),
                    }
                )
            except (KeyError, ValueError) as e:
                logger.warning("candle_ws_bad_kline", error=str(e), kline=str(kline))

        if candles_to_save:
            count = await self._hm.upsert_candles(candles_to_save)
            logger.debug(
                "candle_ws_saved",
                count=count,
                symbol=self._symbol,
                interval=self._interval,
            )

    @staticmethod
    def _to_bybit_interval(ccxt_interval: str) -> str:
        """Convert CCXT-style interval to Bybit WS interval string."""
        mapping = {
            "1m": "1",
            "3m": "3",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "2h": "120",
            "4h": "240",
            "6h": "360",
            "12h": "720",
            "1d": "D",
            "1w": "W",
            "1M": "M",
        }
        return mapping.get(ccxt_interval, ccxt_interval)
