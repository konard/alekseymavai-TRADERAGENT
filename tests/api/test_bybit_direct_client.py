"""
Tests for ByBitDirectClient — V5 API client with mock HTTP responses.
"""

import hashlib
import hmac
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from bot.api.bybit_direct_client import ByBitDirectClient
from bot.api.exceptions import (
    AuthenticationError,
    ExchangeAPIError,
    ExchangeNotAvailableError,
    InsufficientFundsError,
    InvalidOrderError,
    RateLimitError,
)


class TestByBitDirectClientInit:
    def test_production_spot(self):
        client = ByBitDirectClient(
            api_key="test_key", api_secret="test_secret", testnet=False, market_type="spot"
        )
        assert client.base_url == "https://api.bybit.com"
        assert client.category == "spot"
        assert client.market_type == "spot"
        assert client.testnet is False

    def test_production_linear(self):
        client = ByBitDirectClient(
            api_key="key", api_secret="secret", testnet=False, market_type="linear"
        )
        assert client.category == "linear"
        assert client.market_type == "linear"

    def test_testnet_forces_linear(self):
        """Demo trading does NOT support spot, should force linear."""
        client = ByBitDirectClient(
            api_key="key", api_secret="secret", testnet=True, market_type="spot"
        )
        assert client.market_type == "linear"
        assert client.category == "linear"
        assert client.base_url == "https://api-demo.bybit.com"

    def test_testnet_linear(self):
        client = ByBitDirectClient(
            api_key="key", api_secret="secret", testnet=True, market_type="linear"
        )
        assert client.market_type == "linear"
        assert client.base_url == "https://api-demo.bybit.com"

    def test_initial_stats(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        assert client._request_count == 0
        assert client._error_count == 0


class TestSignature:
    def test_create_signature(self):
        client = ByBitDirectClient(api_key="test_api_key", api_secret="test_secret")
        timestamp = 1700000000000
        params = "category=spot&symbol=BTCUSDT"
        sig = client._create_signature(timestamp, params)

        # Verify manually
        payload = f"{timestamp}test_api_key{client.recv_window}{params}"
        expected = hmac.new(b"test_secret", payload.encode("utf-8"), hashlib.sha256).hexdigest()
        assert sig == expected

    def test_build_headers(self):
        client = ByBitDirectClient(api_key="test_key", api_secret="test_secret")
        headers = client._build_headers(1700000000000, "sig123")
        assert headers["X-BAPI-API-KEY"] == "test_key"
        assert headers["X-BAPI-TIMESTAMP"] == "1700000000000"
        assert headers["X-BAPI-SIGN"] == "sig123"
        assert headers["X-BAPI-SIGN-TYPE"] == "2"
        assert headers["Content-Type"] == "application/json"


class TestErrorMapping:
    def test_auth_error(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        err = client._map_error_code(10003, "Invalid API key")
        assert isinstance(err, AuthenticationError)

    def test_rate_limit(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        err = client._map_error_code(10006, "Rate limited")
        assert isinstance(err, RateLimitError)

    def test_insufficient_funds(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        err = client._map_error_code(110003, "Not enough balance")
        assert isinstance(err, InsufficientFundsError)

    def test_invalid_order(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        err = client._map_error_code(110001, "Invalid qty")
        assert isinstance(err, InvalidOrderError)

    def test_unavailable(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        err = client._map_error_code(10016, "Unavailable")
        assert isinstance(err, ExchangeNotAvailableError)

    def test_unknown_error_code(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        err = client._map_error_code(99999, "Unknown")
        assert isinstance(err, ExchangeAPIError)


class TestGetStatistics:
    def test_initial_stats(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        stats = client.get_statistics()
        assert stats["exchange"] == "bybit"
        assert stats["total_requests"] == 0
        assert stats["total_errors"] == 0
        assert stats["error_rate"] == 0

    def test_stats_after_requests(self):
        client = ByBitDirectClient(api_key="k", api_secret="s", testnet=True)
        client._request_count = 10
        client._error_count = 2
        stats = client.get_statistics()
        assert stats["total_requests"] == 10
        assert stats["error_rate"] == 0.2
        assert stats["testnet"] is True


class TestSessionLifecycle:
    async def test_initialize_creates_session(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        assert client._session is None
        with patch.object(client, "fetch_markets", new_callable=AsyncMock, return_value={}):
            await client.initialize()
        assert client._session is not None
        await client.close()

    async def test_close_cleans_session(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        with patch.object(client, "fetch_markets", new_callable=AsyncMock, return_value={}):
            await client.initialize()
        await client.close()
        assert client._session is None

    async def test_close_without_init(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        await client.close()  # Should not raise
        assert client._session is None


class TestRequestNotInitialized:
    async def test_request_without_init(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        with pytest.raises(ExchangeAPIError, match="not initialized"):
            await client._request("GET", "/v5/test")


class TestFetchBalance:
    async def test_fetch_balance_testnet(self):
        client = ByBitDirectClient(api_key="k", api_secret="s", testnet=True)
        mock_response = {
            "list": [
                {
                    "coin": [
                        {"coin": "USDT", "walletBalance": "10000", "availableToWithdraw": "9500"},
                        {"coin": "BTC", "walletBalance": "0.5", "availableToWithdraw": "0.5"},
                    ]
                }
            ]
        }

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            balance = await client.fetch_balance()
            assert balance["total"]["USDT"] == 10000.0
            assert balance["free"]["USDT"] == 9500.0
            assert balance["total"]["BTC"] == 0.5


class TestFetchTicker:
    async def test_fetch_ticker(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        mock_response = {
            "list": [
                {
                    "lastPrice": "45000.5",
                    "bid1Price": "44999.0",
                    "ask1Price": "45001.0",
                    "highPrice24h": "46000.0",
                    "lowPrice24h": "44000.0",
                    "volume24h": "50000.0",
                    "price24hPcnt": "0.025",
                }
            ]
        }

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            ticker = await client.fetch_ticker("BTC/USDT")
            assert ticker["symbol"] == "BTC/USDT"
            assert ticker["last"] == 45000.5
            assert ticker["bid"] == 44999.0
            assert ticker["ask"] == 45001.0
            assert ticker["percentage"] == 2.5


class TestCreateOrder:
    async def test_create_limit_order(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        mock_response = {"orderId": "order123", "orderLinkId": "link456"}

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            result = await client.create_limit_order(
                symbol="BTC/USDT",
                side="buy",
                amount=Decimal("0.001"),
                price=Decimal("45000"),
            )
            assert result["id"] == "order123"
            assert result["type"] == "limit"
            assert result["side"] == "buy"

    async def test_create_market_order(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        mock_response = {"orderId": "order789", "orderLinkId": "link012"}

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            result = await client.create_market_order(
                symbol="BTC/USDT", side="sell", amount=Decimal("0.001")
            )
            assert result["id"] == "order789"
            assert result["type"] == "market"
            assert result["side"] == "sell"

    async def test_create_order_wrapper_limit(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        mock_response = {"orderId": "o1", "orderLinkId": "l1"}

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            result = await client.create_order("BTC/USDT", "limit", "buy", 0.001, 45000)
            assert result["type"] == "limit"

    async def test_create_order_wrapper_market(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        mock_response = {"orderId": "o2", "orderLinkId": "l2"}

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            result = await client.create_order("BTC/USDT", "market", "sell", 0.001)
            assert result["type"] == "market"

    async def test_create_order_invalid_type(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        with pytest.raises(ValueError, match="Unknown order type"):
            await client.create_order("BTC/USDT", "stop", "buy", 0.001)

    async def test_limit_order_requires_price(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        with pytest.raises(ValueError, match="Price required"):
            await client.create_order("BTC/USDT", "limit", "buy", 0.001, None)


class TestFetchOpenOrders:
    async def test_fetch_open_orders(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        mock_response = {
            "list": [
                {
                    "orderId": "123",
                    "orderLinkId": "link",
                    "symbol": "BTCUSDT",
                    "orderType": "Limit",
                    "side": "Buy",
                    "price": "45000",
                    "qty": "0.001",
                    "cumExecQty": "0",
                    "leavesQty": "0.001",
                    "orderStatus": "New",
                    "createdTime": "1700000000000",
                }
            ]
        }

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            orders = await client.fetch_open_orders("BTC/USDT")
            assert len(orders) == 1
            assert orders[0]["id"] == "123"
            assert orders[0]["side"] == "buy"
            assert orders[0]["type"] == "limit"


class TestFetchMarkets:
    async def test_fetch_markets(self):
        client = ByBitDirectClient(api_key="k", api_secret="s")
        mock_response = {
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "status": "Trading",
                    "lotSizeFilter": {
                        "minOrderQty": "0.001",
                        "maxOrderQty": "100",
                        "basePrecision": "0.001",
                        "minOrderAmt": "10",
                    },
                    "priceFilter": {"minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
                }
            ]
        }

        with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
            markets = await client.fetch_markets()
            assert "BTC/USDT" in markets
            assert markets["BTC/USDT"]["active"] is True
            assert markets["BTC/USDT"]["base"] == "BTC"
