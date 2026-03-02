"""
ExecutionLayer — injectable exchange abstraction for live bot and backtesting.

Provides a minimal interface for order management that works identically in:
- Live trading: delegates to ByBitDirectClient
- Backtesting: delegates to MarketSimulator

Usage (live bot)::

    client = ByBitDirectClient(...)
    layer = LiveExecutionLayer(client)
    result = await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 65000.0)

Usage (backtest)::

    simulator = MarketSimulator(config)
    layer = BacktestExecutionLayer(simulator)
    result = await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 65000.0)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any


class ExecutionLayer(ABC):
    """
    Abstract execution layer — swap live/backtest without changing strategy code.

    All methods mirror the ByBitDirectClient API so strategies can call them
    regardless of whether they run live or in a backtest.
    """

    @abstractmethod
    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Create a new order.

        Args:
            symbol:     Trading pair, e.g. "BTC/USDT".
            order_type: "limit" or "market".
            side:       "buy" or "sell".
            amount:     Order quantity in base currency.
            price:      Limit price (required for limit orders).
            params:     Extra exchange-specific parameters.

        Returns:
            Order dict with at least {'id', 'symbol', 'side', 'amount', 'price', 'status'}.
        """
        ...

    @abstractmethod
    async def cancel_order(
        self,
        order_id: str,
        symbol: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Cancel an open order by ID."""
        ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str) -> list[dict[str, Any]]:
        """Cancel all open orders for *symbol*."""
        ...

    @abstractmethod
    async def fetch_open_orders(
        self,
        symbol: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return list of currently open orders for *symbol*."""
        ...

    @abstractmethod
    async def fetch_balance(self) -> dict[str, Any]:
        """
        Return account balance.

        Must include at minimum: ``{'USDT': {'free': float, 'total': float}}``.
        """
        ...

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        """
        Return current ticker for *symbol*.

        Must include at minimum: ``{'last': float, 'bid': float, 'ask': float}``.
        """
        ...

    # ------------------------------------------------------------------
    # Optional convenience helpers (default implementations provided)
    # ------------------------------------------------------------------

    async def get_free_balance(self, currency: str = "USDT") -> Decimal:
        """Return free balance for *currency* as Decimal."""
        balance = await self.fetch_balance()
        raw = balance.get(currency, {}).get("free", 0.0)
        return Decimal(str(raw))

    async def get_last_price(self, symbol: str) -> Decimal:
        """Return last traded price for *symbol* as Decimal."""
        ticker = await self.fetch_ticker(symbol)
        return Decimal(str(ticker["last"]))


class LiveExecutionLayer(ExecutionLayer):
    """
    Production execution layer — wraps ByBitDirectClient.

    Args:
        client: Any object implementing the ByBitDirectClient interface
                (has create_order, cancel_order, fetch_balance, etc.).
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._client.create_order(
            symbol, order_type, side, amount, price, params
        )

    async def cancel_order(
        self,
        order_id: str,
        symbol: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._client.cancel_order(order_id, symbol, params)

    async def cancel_all_orders(self, symbol: str) -> list[dict[str, Any]]:
        return await self._client.cancel_all_orders(symbol)

    async def fetch_open_orders(
        self,
        symbol: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return await self._client.fetch_open_orders(symbol, params or {})

    async def fetch_balance(self) -> dict[str, Any]:
        return await self._client.fetch_balance()

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return await self._client.fetch_ticker(symbol)


class BacktestExecutionLayer(ExecutionLayer):
    """
    Simulated execution layer for backtesting.

    Records all order operations so they can be inspected in tests or passed
    to a MarketSimulator after construction (via ``set_simulator()``).

    If no simulator is attached, orders are simply recorded and acknowledged
    with a synthetic response.  Attach a real MarketSimulator for realistic
    fill simulation.
    """

    def __init__(self) -> None:
        self._simulator: Any = None
        self._orders: list[dict[str, Any]] = []
        self._balance: dict[str, Any] = {"USDT": {"free": 10000.0, "total": 10000.0}}
        self._next_order_id: int = 1

    def set_simulator(self, simulator: Any) -> None:
        """Attach a MarketSimulator for realistic fill simulation."""
        self._simulator = simulator

    def set_balance(self, usdt_balance: float) -> None:
        """Set simulated USDT balance."""
        self._balance = {"USDT": {"free": usdt_balance, "total": usdt_balance}}

    @property
    def orders(self) -> list[dict[str, Any]]:
        """All orders placed through this layer."""
        return list(self._orders)

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        order = {
            "id": str(self._next_order_id),
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "status": "open",
            "params": params or {},
        }
        self._next_order_id += 1
        self._orders.append(order)
        return order

    async def cancel_order(
        self,
        order_id: str,
        symbol: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for order in self._orders:
            if order["id"] == order_id:
                order["status"] = "canceled"
                return order
        return {"id": order_id, "status": "not_found"}

    async def cancel_all_orders(self, symbol: str) -> list[dict[str, Any]]:
        canceled = []
        for order in self._orders:
            if order["symbol"] == symbol and order["status"] == "open":
                order["status"] = "canceled"
                canceled.append(order)
        return canceled

    async def fetch_open_orders(
        self,
        symbol: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            o for o in self._orders
            if o["symbol"] == symbol and o["status"] == "open"
        ]

    async def fetch_balance(self) -> dict[str, Any]:
        return dict(self._balance)

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        # Minimal synthetic ticker — override via set_simulator() for real data
        return {"last": 0.0, "bid": 0.0, "ask": 0.0, "symbol": symbol}
