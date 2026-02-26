"""
Replay exchange client — drop-in replacement for ``ByBitDirectClient``.

Fed with a list of historical OHLCV candles and a ``SimulatedClock``, it
simulates an exchange: ticker prices, order placement/cancellation, limit
order fill matching, and balance management.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from bot.replay.simulated_clock import SimulatedClock
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class ReplayExchangeClient:
    """
    Mock exchange driven by historical 5-minute candles.

    Candle format (list of dicts)::

        {"timestamp": 1700000000000,  # ms
         "open": 0.65, "high": 0.66, "low": 0.64,
         "close": 0.651, "volume": 123456.7}

    Order matching:
    - Limit buy fills when candle ``low <= order price``
    - Limit sell fills when candle ``high >= order price``
    - Market orders fill at current candle close
    - Fee: 0.1 % taker (configurable)
    """

    def __init__(
        self,
        candles: list[dict],
        clock: SimulatedClock,
        initial_balance: Decimal,
        symbol: str = "XRP/USDT",
        fee_rate: Decimal = Decimal("0.001"),
    ) -> None:
        self._candles = candles
        self._clock = clock
        self._symbol = symbol
        self._fee_rate = fee_rate

        # Balance tracking (USDT only for simplicity)
        self._free_balance = initial_balance
        self._used_balance = Decimal("0")
        self._base_balance = Decimal("0")  # base asset held

        # Orders
        self._open_orders: dict[str, dict] = {}
        self._order_history: dict[str, dict] = {}
        self._order_counter = 0
        self._fill_log: list[dict] = []

        # Candle index (cached)
        self._last_candle_idx = 0

        # Statistics
        self._request_count = 0
        self._initialized = False

    # =====================================================================
    # Candle helpers
    # =====================================================================

    def _current_candle_index(self) -> int:
        """Return the index of the candle that corresponds to the current clock time."""
        ts_ms = int(self._clock.current_time * 1000)
        # Fast forward from cached position
        idx = self._last_candle_idx
        while idx < len(self._candles) - 1 and self._candles[idx + 1]["timestamp"] <= ts_ms:
            idx += 1
        self._last_candle_idx = idx
        return idx

    def _current_candle(self) -> dict:
        idx = self._current_candle_index()
        return self._candles[idx]

    def has_more_candles(self) -> bool:
        return self._current_candle_index() < len(self._candles) - 1

    @property
    def candle_count(self) -> int:
        return len(self._candles)

    @property
    def processed_candles(self) -> int:
        return self._current_candle_index() + 1

    # =====================================================================
    # Order matching
    # =====================================================================

    def _match_limit_orders(self) -> None:
        """Check all open limit orders against the current candle's high/low."""
        candle = self._current_candle()
        low = Decimal(str(candle["low"]))
        high = Decimal(str(candle["high"]))

        filled_ids = []
        for order_id, order in self._open_orders.items():
            if order["type"] != "limit":
                continue

            price = Decimal(str(order["price"]))
            amount = Decimal(str(order["amount"]))

            if order["side"] == "buy" and low <= price:
                self._fill_order(order_id, order, price, amount)
                filled_ids.append(order_id)
            elif order["side"] == "sell" and high >= price:
                self._fill_order(order_id, order, price, amount)
                filled_ids.append(order_id)

        for oid in filled_ids:
            filled_order = self._open_orders.pop(oid)
            filled_order["status"] = "closed"
            self._order_history[oid] = filled_order

    def _fill_order(
        self,
        order_id: str,
        order: dict,
        fill_price: Decimal,
        fill_amount: Decimal,
    ) -> None:
        """Update balances for a filled order."""
        cost = fill_price * fill_amount
        fee = cost * self._fee_rate

        if order["side"] == "buy":
            # We already reserved cost in _used_balance when the order was placed
            self._used_balance -= cost
            self._base_balance += fill_amount
            # Fee comes out of free balance
            self._free_balance -= fee
        else:
            # Sell: we reserved base_amount when order was placed
            self._free_balance += cost - fee

        order["filled"] = float(fill_amount)
        order["remaining"] = 0.0
        order["status"] = "closed"

        self._fill_log.append(
            {
                "order_id": order_id,
                "side": order["side"],
                "price": float(fill_price),
                "amount": float(fill_amount),
                "fee": float(fee),
                "timestamp": self._clock.current_time,
            }
        )

        logger.debug(
            "replay_order_filled",
            order_id=order_id,
            side=order["side"],
            price=str(fill_price),
            amount=str(fill_amount),
        )

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"replay-{self._order_counter:06d}"

    # =====================================================================
    # Public API — matches ByBitDirectClient signatures
    # =====================================================================

    async def initialize(self) -> None:
        self._initialized = True

    async def close(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    # -- ticker -----------------------------------------------------------

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        self._request_count += 1
        self._match_limit_orders()

        candle = self._current_candle()
        close = float(candle["close"])
        return {
            "symbol": symbol,
            "last": close,
            "bid": close * 0.9999,
            "ask": close * 1.0001,
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "baseVolume": float(candle["volume"]),
            "percentage": 0.0,
        }

    # -- balance ----------------------------------------------------------

    async def fetch_balance(self) -> dict[str, Any]:
        self._request_count += 1
        total = self._free_balance + self._used_balance
        base = self._symbol.split("/")[0]  # e.g. "XRP"
        return {
            "free": {"USDT": float(self._free_balance), base: float(self._base_balance)},
            "total": {"USDT": float(total), base: float(self._base_balance)},
            "used": {"USDT": float(self._used_balance)},
        }

    # -- orders -----------------------------------------------------------

    async def fetch_open_orders(
        self,
        symbol: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._request_count += 1
        self._match_limit_orders()

        result = []
        for oid, order in self._open_orders.items():
            if symbol and order.get("symbol") != symbol:
                continue
            result.append({**order, "id": oid})
        return result

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        self._request_count += 1
        order_id = self._next_order_id()
        amount_d = Decimal(str(amount))

        if order_type.lower() == "market":
            # Fill immediately at current candle close
            candle = self._current_candle()
            fill_price = Decimal(str(candle["close"]))
            cost = fill_price * amount_d
            fee = cost * self._fee_rate

            if side.lower() == "buy":
                if self._free_balance < cost + fee:
                    raise Exception(
                        f"InsufficientFunds: need {cost + fee} USDT, have {self._free_balance}"
                    )
                self._free_balance -= cost + fee
                self._base_balance += amount_d
            else:
                if self._base_balance < amount_d:
                    raise Exception(
                        f"InsufficientFunds: need {amount_d} base, have {self._base_balance}"
                    )
                self._base_balance -= amount_d
                self._free_balance += cost - fee

            order = {
                "id": order_id,
                "symbol": symbol,
                "type": "market",
                "side": side.lower(),
                "price": float(fill_price),
                "amount": amount,
                "filled": amount,
                "remaining": 0.0,
                "status": "closed",
                "timestamp": int(self._clock.current_time * 1000),
            }
            self._order_history[order_id] = order

            self._fill_log.append(
                {
                    "order_id": order_id,
                    "side": side.lower(),
                    "price": float(fill_price),
                    "amount": amount,
                    "fee": float(fee),
                    "timestamp": self._clock.current_time,
                }
            )

            return {
                "id": order_id,
                "symbol": symbol,
                "type": "market",
                "side": side.lower(),
                "amount": amount,
            }

        elif order_type.lower() == "limit":
            if price is None:
                raise ValueError("Price required for limit orders")

            price_d = Decimal(str(price))
            cost = price_d * amount_d

            # Reserve funds
            if side.lower() == "buy":
                if self._free_balance < cost:
                    raise Exception(
                        f"InsufficientFunds: need {cost} USDT, have {self._free_balance}"
                    )
                self._free_balance -= cost
                self._used_balance += cost
            else:
                if self._base_balance < amount_d:
                    raise Exception(
                        f"InsufficientFunds: need {amount_d} base, have {self._base_balance}"
                    )
                self._base_balance -= amount_d

            order = {
                "id": order_id,
                "symbol": symbol,
                "type": "limit",
                "side": side.lower(),
                "price": price,
                "amount": amount,
                "filled": 0.0,
                "remaining": amount,
                "status": "open",
                "timestamp": int(self._clock.current_time * 1000),
            }
            self._open_orders[order_id] = order

            # Check if it fills immediately against current candle
            self._match_limit_orders()

            return {
                "id": order_id,
                "symbol": symbol,
                "type": "limit",
                "side": side.lower(),
                "amount": amount,
                "price": price,
            }

        else:
            raise ValueError(f"Unknown order type: {order_type}")

    async def cancel_order(
        self,
        order_id: str,
        symbol: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._request_count += 1
        order = self._open_orders.pop(order_id, None)
        if order:
            # Release reserved funds
            price_d = Decimal(str(order["price"]))
            amount_d = Decimal(str(order["amount"]))
            if order["side"] == "buy":
                cost = price_d * amount_d
                self._used_balance -= cost
                self._free_balance += cost
            else:
                self._base_balance += amount_d

            order["status"] = "cancelled"
            self._order_history[order_id] = order
        return {"id": order_id, "symbol": symbol, "status": "cancelled"}

    async def cancel_all_orders(self, symbol: str) -> list[dict[str, Any]]:
        self._request_count += 1
        results = []
        for oid in list(self._open_orders.keys()):
            order = self._open_orders[oid]
            if order.get("symbol") == symbol:
                results.append(await self.cancel_order(oid, symbol))
        return results

    async def fetch_order(
        self,
        order_id: str,
        symbol: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._request_count += 1
        # Check open orders first
        if order_id in self._open_orders:
            order = self._open_orders[order_id]
            return {**order, "id": order_id}
        # Then history
        if order_id in self._order_history:
            order = self._order_history[order_id]
            return {**order, "id": order_id}
        # Not found
        return {
            "id": order_id,
            "symbol": symbol,
            "status": "unknown",
            "filled": 0.0,
            "remaining": 0.0,
        }

    # -- OHLCV ------------------------------------------------------------

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "5m",
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[list]:
        """
        Return candle data from the historical feed.

        Timeframe aggregation: the raw data is 5-minute candles.
        For larger timeframes we aggregate on the fly.
        """
        self._request_count += 1
        current_idx = self._current_candle_index()

        # Determine how many raw candles per requested bar
        tf_minutes = _timeframe_to_minutes(timeframe)
        raw_per_bar = max(1, tf_minutes // 5)

        effective_limit = limit or 100

        # Slice raw candles up to current time
        raw = self._candles[: current_idx + 1]

        if raw_per_bar == 1:
            # No aggregation needed
            raw = raw[-effective_limit:]
            return [
                [c["timestamp"], c["open"], c["high"], c["low"], c["close"], c["volume"]]
                for c in raw
            ]

        # Aggregate
        bars: list[list] = []
        # Work backwards from end, group into chunks of raw_per_bar
        start = len(raw) - (len(raw) % raw_per_bar) if len(raw) % raw_per_bar != 0 else len(raw)
        for i in range(0, len(raw), raw_per_bar):
            chunk = raw[i : i + raw_per_bar]
            if not chunk:
                continue
            bar = [
                chunk[0]["timestamp"],
                chunk[0]["open"],
                max(c["high"] for c in chunk),
                min(c["low"] for c in chunk),
                chunk[-1]["close"],
                sum(c["volume"] for c in chunk),
            ]
            bars.append(bar)

        return bars[-effective_limit:]

    # -- markets ----------------------------------------------------------

    async def fetch_markets(self) -> dict[str, Any]:
        self._request_count += 1
        base, quote = self._symbol.split("/")
        return {
            self._symbol: {
                "id": self._symbol.replace("/", ""),
                "symbol": self._symbol,
                "base": base,
                "quote": quote,
                "active": True,
                "limits": {
                    "amount": {"min": 0.1, "max": 1_000_000},
                    "price": {"min": 0.0001, "max": 100_000},
                    "cost": {"min": 1.0, "max": None},
                },
                "precision": {"amount": 1, "price": 4},
            }
        }

    # -- stats ------------------------------------------------------------

    def get_statistics(self) -> dict[str, Any]:
        return {
            "exchange": "replay",
            "initialized": self._initialized,
            "testnet": True,
            "total_requests": self._request_count,
            "total_errors": 0,
            "error_rate": 0.0,
            "candles_total": len(self._candles),
            "candles_processed": self.processed_candles,
            "open_orders": len(self._open_orders),
            "total_fills": len(self._fill_log),
            "free_balance": float(self._free_balance),
            "base_balance": float(self._base_balance),
        }


def _timeframe_to_minutes(tf: str) -> int:
    """Convert a timeframe string like '1h', '15m', '1d' to minutes."""
    mapping = {
        "1m": 1,
        "3m": 3,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "2h": 120,
        "4h": 240,
        "6h": 360,
        "12h": 720,
        "1d": 1440,
        "1w": 10080,
    }
    return mapping.get(tf, 60)
