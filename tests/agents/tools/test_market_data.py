from __future__ import annotations

import asyncio
import time

import pytest

from bot.agents.tools.market_data import (
    BaseTool,
    LiquidationMapTool,
    LivePriceTool,
    OnChainTool,
    OrderbookDepthTool,
    ToolRegistry,
    ToolResult,
)


# ── ToolResult ──────────────────────────────────────────────────────────────

class TestToolResult:
    def test_defaults(self):
        r = ToolResult(success=True, data={"x": 1})
        assert r.success is True
        assert r.error is None
        assert r.execution_time == 0.0

    def test_to_dict(self):
        r = ToolResult(success=False, data={}, error="boom", execution_time=0.123)
        d = r.to_dict()
        assert isinstance(d, dict)
        assert d["success"] is False
        assert d["error"] == "boom"
        assert d["execution_time"] == 0.123
        assert d["data"] == {}


# ── BaseTool interface ──────────────────────────────────────────────────────

class TestBaseTool:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            BaseTool()  # type: ignore[abstract]


# ── LivePriceTool ───────────────────────────────────────────────────────────

class TestLivePriceTool:
    @pytest.mark.asyncio
    async def test_mock_fallback(self):
        tool = LivePriceTool()
        r = await tool.execute({"symbol": "ETHUSDT"})
        assert r.success is True
        assert r.data["symbol"] == "ETHUSDT"
        assert r.data["bid"] > 0
        assert r.data["ask"] > r.data["bid"]
        assert r.data["spread"] > 0
        assert r.data["spread_pct"] > 0
        assert r.data["mid_price"] > 0
        assert r.execution_time >= 0

    @pytest.mark.asyncio
    async def test_with_provider(self):
        async def provider(symbol: str) -> dict:
            return {
                "bid": 100.0,
                "ask": 101.0,
                "last": 100.5,
                "volume_24h": 999.0,
                "funding_rate": 0.0005,
            }

        tool = LivePriceTool(price_provider=provider)
        r = await tool.execute({"symbol": "TEST"})
        assert r.success is True
        assert r.data["bid"] == 100.0
        assert r.data["ask"] == 101.0
        assert r.data["spread"] == 1.0
        assert r.data["mid_price"] == 100.5
        assert r.data["volume_24h"] == 999.0

    @pytest.mark.asyncio
    async def test_missing_symbol(self):
        tool = LivePriceTool()
        r = await tool.execute({})
        assert r.success is False
        assert "symbol" in r.error.lower()

    @pytest.mark.asyncio
    async def test_cache_returns_same_result(self):
        call_count = 0

        async def counting_provider(symbol: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"bid": 50.0, "ask": 51.0, "last": 50.5}

        tool = LivePriceTool(price_provider=counting_provider)
        r1 = await tool.execute({"symbol": "X"})
        r2 = await tool.execute({"symbol": "X"})
        assert r1.success and r2.success
        assert call_count == 1  # second call served from cache

    @pytest.mark.asyncio
    async def test_provider_error(self):
        async def failing(symbol: str) -> dict:
            raise ConnectionError("API down")

        tool = LivePriceTool(price_provider=failing)
        r = await tool.execute({"symbol": "BTC"})
        assert r.success is False
        assert "API down" in r.error


# ── OrderbookDepthTool ──────────────────────────────────────────────────────

class TestOrderbookDepthTool:
    @pytest.mark.asyncio
    async def test_mock_fallback(self):
        tool = OrderbookDepthTool()
        r = await tool.execute({"symbol": "BTCUSDT", "depth": 5})
        assert r.success is True
        d = r.data
        assert d["symbol"] == "BTCUSDT"
        assert d["depth"] == 5
        assert d["total_bid_volume"] > 0
        assert d["total_ask_volume"] > 0
        # symmetric mock => imbalance ~0
        assert abs(d["imbalance"]) < 0.01
        assert d["spread"] > 0

    @pytest.mark.asyncio
    async def test_with_provider(self):
        async def provider(symbol: str, depth: int) -> dict:
            return {
                "bids": [[100, 10], [99, 5]],
                "asks": [[101, 3], [102, 2]],
            }

        tool = OrderbookDepthTool(orderbook_provider=provider)
        r = await tool.execute({"symbol": "X", "depth": 2})
        assert r.success is True
        assert r.data["total_bid_volume"] == 15
        assert r.data["total_ask_volume"] == 5
        assert r.data["imbalance"] == 0.5  # (15-5)/20
        assert r.data["bid_wall_price"] == 100  # highest qty bid
        assert r.data["ask_wall_price"] == 101  # highest qty ask

    @pytest.mark.asyncio
    async def test_missing_symbol(self):
        tool = OrderbookDepthTool()
        r = await tool.execute({"depth": 10})
        assert r.success is False

    @pytest.mark.asyncio
    async def test_provider_error(self):
        async def failing(symbol: str, depth: int) -> dict:
            raise RuntimeError("timeout")

        tool = OrderbookDepthTool(orderbook_provider=failing)
        r = await tool.execute({"symbol": "X"})
        assert r.success is False
        assert "timeout" in r.error


# ── LiquidationMapTool ──────────────────────────────────────────────────────

class TestLiquidationMapTool:
    @pytest.mark.asyncio
    async def test_estimate_zones(self):
        tool = LiquidationMapTool()
        r = await tool.execute({
            "symbol": "ETHUSDT",
            "current_price": 3500,
            "recent_high": 3600,
            "recent_low": 3400,
        })
        assert r.success is True
        zones = r.data["zones"]
        assert len(zones) >= 6  # 3 pct_levels * 2 sides + round numbers
        sides = {z["side"] for z in zones}
        assert "long" in sides
        assert "short" in sides
        # zones should be sorted by price
        prices = [z["price"] for z in zones]
        assert prices == sorted(prices)

    @pytest.mark.asyncio
    async def test_with_provider(self):
        async def provider(symbol: str, price: float) -> list:
            return [{"price": 3000, "side": "long", "estimated_density": 0.9, "reason": "custom"}]

        tool = LiquidationMapTool(liquidation_provider=provider)
        r = await tool.execute({"symbol": "ETH", "current_price": 3500})
        assert r.success is True
        assert len(r.data["zones"]) == 1
        assert r.data["zones"][0]["reason"] == "custom"

    @pytest.mark.asyncio
    async def test_missing_params(self):
        tool = LiquidationMapTool()
        r = await tool.execute({"symbol": "ETH"})
        assert r.success is False
        assert "current_price" in r.error

    @pytest.mark.asyncio
    async def test_provider_error(self):
        async def failing(symbol: str, price: float) -> list:
            raise ValueError("bad data")

        tool = LiquidationMapTool(liquidation_provider=failing)
        r = await tool.execute({"symbol": "X", "current_price": 100})
        assert r.success is False
        assert "bad data" in r.error


# ── OnChainTool ─────────────────────────────────────────────────────────────

class TestOnChainTool:
    @pytest.mark.asyncio
    async def test_mock_fallback(self):
        tool = OnChainTool()
        r = await tool.execute({"symbol": "ETH"})
        assert r.success is True
        d = r.data
        assert d["symbol"] == "ETH"
        assert d["net_flow"] == 0.0  # neutral mock
        assert d["whale_transactions"] == 12
        assert d["exchange_reserve_change_pct"] == 0.0

    @pytest.mark.asyncio
    async def test_with_provider(self):
        async def provider(symbol: str) -> dict:
            return {
                "exchange_inflow": 8000,
                "exchange_outflow": 3000,
                "net_flow": 5000,
                "whale_transactions": 42,
                "exchange_reserve_change_pct": 1.5,
            }

        tool = OnChainTool(onchain_provider=provider)
        r = await tool.execute({"symbol": "BTC"})
        assert r.success is True
        assert r.data["net_flow"] == 5000
        assert r.data["whale_transactions"] == 42

    @pytest.mark.asyncio
    async def test_missing_symbol(self):
        tool = OnChainTool()
        r = await tool.execute({})
        assert r.success is False

    @pytest.mark.asyncio
    async def test_provider_error(self):
        async def failing(symbol: str) -> dict:
            raise IOError("network error")

        tool = OnChainTool(onchain_provider=failing)
        r = await tool.execute({"symbol": "SOL"})
        assert r.success is False
        assert "network error" in r.error


# ── ToolRegistry ────────────────────────────────────────────────────────────

class TestToolRegistry:
    def test_default_registration(self):
        reg = ToolRegistry()
        names = reg.list()
        assert "live_price" in names
        assert "orderbook_depth" in names
        assert "liquidation_map" in names
        assert "onchain_flow" in names

    def test_empty_registry(self):
        reg = ToolRegistry(register_defaults=False)
        assert reg.list() == []

    def test_get_existing(self):
        reg = ToolRegistry()
        tool = reg.get("live_price")
        assert tool is not None
        assert isinstance(tool, LivePriceTool)

    def test_get_missing(self):
        reg = ToolRegistry()
        assert reg.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_execute_valid(self):
        reg = ToolRegistry()
        r = await reg.execute("live_price", {"symbol": "BTCUSDT"})
        assert r.success is True
        assert r.data["symbol"] == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        reg = ToolRegistry()
        r = await reg.execute("does_not_exist", {})
        assert r.success is False
        assert "Unknown tool" in r.error

    def test_register_custom_tool(self):
        class DummyTool(BaseTool):
            name = "dummy"
            description = "A dummy tool"

            async def execute(self, params):
                return ToolResult(success=True, data={"ok": True})

        reg = ToolRegistry(register_defaults=False)
        reg.register(DummyTool())
        assert "dummy" in reg.list()
        assert isinstance(reg.get("dummy"), DummyTool)
