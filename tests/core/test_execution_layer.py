"""Tests for ExecutionLayer abstraction (Phase 3)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.core.trading_core import (
    BacktestExecutionLayer,
    ExecutionLayer,
    LiveExecutionLayer,
)


# ---------------------------------------------------------------------------
# BacktestExecutionLayer
# ---------------------------------------------------------------------------


class TestBacktestExecutionLayer:
    """Verify BacktestExecutionLayer simulates exchange operations."""

    def test_is_execution_layer(self) -> None:
        layer = BacktestExecutionLayer()
        assert isinstance(layer, ExecutionLayer)

    @pytest.mark.asyncio
    async def test_create_limit_order(self) -> None:
        layer = BacktestExecutionLayer()
        result = await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 65000.0)
        assert result["symbol"] == "BTC/USDT"
        assert result["side"] == "buy"
        assert result["amount"] == 0.01
        assert result["price"] == 65000.0
        assert result["status"] == "open"
        assert "id" in result

    @pytest.mark.asyncio
    async def test_create_market_order(self) -> None:
        layer = BacktestExecutionLayer()
        result = await layer.create_order("ETH/USDT", "market", "sell", 1.0)
        assert result["type"] == "market"
        assert result["price"] is None

    @pytest.mark.asyncio
    async def test_order_ids_are_unique(self) -> None:
        layer = BacktestExecutionLayer()
        o1 = await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 60000.0)
        o2 = await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 61000.0)
        assert o1["id"] != o2["id"]

    @pytest.mark.asyncio
    async def test_orders_recorded(self) -> None:
        layer = BacktestExecutionLayer()
        await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 60000.0)
        await layer.create_order("BTC/USDT", "limit", "sell", 0.01, 70000.0)
        assert len(layer.orders) == 2

    @pytest.mark.asyncio
    async def test_cancel_order(self) -> None:
        layer = BacktestExecutionLayer()
        order = await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 60000.0)
        canceled = await layer.cancel_order(order["id"], "BTC/USDT")
        assert canceled["status"] == "canceled"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_order(self) -> None:
        layer = BacktestExecutionLayer()
        result = await layer.cancel_order("nonexistent", "BTC/USDT")
        assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_cancel_all_orders(self) -> None:
        layer = BacktestExecutionLayer()
        await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 60000.0)
        await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 61000.0)
        await layer.create_order("ETH/USDT", "limit", "buy", 1.0, 3000.0)

        canceled = await layer.cancel_all_orders("BTC/USDT")
        assert len(canceled) == 2
        # ETH order still open
        eth_orders = await layer.fetch_open_orders("ETH/USDT")
        assert len(eth_orders) == 1

    @pytest.mark.asyncio
    async def test_fetch_open_orders_filters_by_symbol(self) -> None:
        layer = BacktestExecutionLayer()
        await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 60000.0)
        await layer.create_order("ETH/USDT", "limit", "buy", 1.0, 3000.0)

        btc_orders = await layer.fetch_open_orders("BTC/USDT")
        assert len(btc_orders) == 1
        assert btc_orders[0]["symbol"] == "BTC/USDT"

    @pytest.mark.asyncio
    async def test_fetch_open_orders_excludes_canceled(self) -> None:
        layer = BacktestExecutionLayer()
        order = await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 60000.0)
        await layer.cancel_order(order["id"], "BTC/USDT")
        open_orders = await layer.fetch_open_orders("BTC/USDT")
        assert len(open_orders) == 0

    @pytest.mark.asyncio
    async def test_fetch_balance_default(self) -> None:
        layer = BacktestExecutionLayer()
        balance = await layer.fetch_balance()
        assert "USDT" in balance
        assert balance["USDT"]["free"] == 10000.0

    @pytest.mark.asyncio
    async def test_set_balance(self) -> None:
        layer = BacktestExecutionLayer()
        layer.set_balance(50000.0)
        balance = await layer.fetch_balance()
        assert balance["USDT"]["free"] == 50000.0

    @pytest.mark.asyncio
    async def test_get_free_balance_returns_decimal(self) -> None:
        layer = BacktestExecutionLayer()
        layer.set_balance(5000.0)
        free = await layer.get_free_balance("USDT")
        assert isinstance(free, Decimal)
        assert free == Decimal("5000.0")

    @pytest.mark.asyncio
    async def test_fetch_ticker_returns_dict(self) -> None:
        layer = BacktestExecutionLayer()
        ticker = await layer.fetch_ticker("BTC/USDT")
        assert "last" in ticker
        assert "symbol" in ticker


# ---------------------------------------------------------------------------
# LiveExecutionLayer
# ---------------------------------------------------------------------------


class TestLiveExecutionLayer:
    """Verify LiveExecutionLayer delegates to the underlying client."""

    def _make_layer(self) -> tuple[LiveExecutionLayer, MagicMock]:
        client = MagicMock()
        client.create_order = AsyncMock(return_value={"id": "123", "status": "open"})
        client.cancel_order = AsyncMock(return_value={"id": "123", "status": "canceled"})
        client.cancel_all_orders = AsyncMock(return_value=[])
        client.fetch_open_orders = AsyncMock(return_value=[])
        client.fetch_balance = AsyncMock(return_value={"USDT": {"free": 10000.0, "total": 10000.0}})
        client.fetch_ticker = AsyncMock(return_value={"last": 65000.0, "bid": 64999.0, "ask": 65001.0})
        return LiveExecutionLayer(client), client

    def test_is_execution_layer(self) -> None:
        layer, _ = self._make_layer()
        assert isinstance(layer, ExecutionLayer)

    @pytest.mark.asyncio
    async def test_create_order_delegates(self) -> None:
        layer, client = self._make_layer()
        result = await layer.create_order("BTC/USDT", "limit", "buy", 0.01, 65000.0)
        assert result["id"] == "123"
        client.create_order.assert_awaited_once_with(
            "BTC/USDT", "limit", "buy", 0.01, 65000.0, None
        )

    @pytest.mark.asyncio
    async def test_cancel_order_delegates(self) -> None:
        layer, client = self._make_layer()
        await layer.cancel_order("123", "BTC/USDT")
        client.cancel_order.assert_awaited_once_with("123", "BTC/USDT", None)

    @pytest.mark.asyncio
    async def test_cancel_all_orders_delegates(self) -> None:
        layer, client = self._make_layer()
        await layer.cancel_all_orders("BTC/USDT")
        client.cancel_all_orders.assert_awaited_once_with("BTC/USDT")

    @pytest.mark.asyncio
    async def test_fetch_balance_delegates(self) -> None:
        layer, client = self._make_layer()
        balance = await layer.fetch_balance()
        assert balance["USDT"]["free"] == 10000.0
        client.fetch_balance.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_ticker_delegates(self) -> None:
        layer, client = self._make_layer()
        ticker = await layer.fetch_ticker("BTC/USDT")
        assert ticker["last"] == 65000.0
        client.fetch_ticker.assert_awaited_once_with("BTC/USDT")

    @pytest.mark.asyncio
    async def test_get_last_price_returns_decimal(self) -> None:
        layer, _ = self._make_layer()
        price = await layer.get_last_price("BTC/USDT")
        assert isinstance(price, Decimal)
        assert price == Decimal("65000.0")
