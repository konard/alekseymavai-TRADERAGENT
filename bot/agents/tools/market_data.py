from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class ToolResult:
    """Result returned by any agent tool execution."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    execution_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BaseTool(ABC):
    """Abstract base class for all agent tools."""

    name: str = ""
    description: str = ""

    @abstractmethod
    async def execute(self, params: dict[str, Any]) -> ToolResult: ...


# ---------------------------------------------------------------------------
# LivePriceTool
# ---------------------------------------------------------------------------

PriceProvider = Callable[[str], Awaitable[dict[str, float]]]


class LivePriceTool(BaseTool):
    """Fetches current price data with 1-second caching."""

    name = "live_price"
    description = "Fetch current bid/ask/last price for a trading pair"

    def __init__(self, price_provider: PriceProvider | None = None) -> None:
        self._provider = price_provider
        self._cache: dict[str, tuple[float, dict]] = {}  # symbol -> (ts, data)

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        t0 = time.monotonic()
        symbol = params.get("symbol")
        if not symbol or not isinstance(symbol, str):
            return ToolResult(
                success=False,
                data={},
                error="Missing or invalid 'symbol' parameter",
                execution_time=time.monotonic() - t0,
            )
        symbol = symbol.upper()

        # 1-second cache
        now = time.time()
        cached = self._cache.get(symbol)
        if cached and (now - cached[0]) < 1.0:
            return ToolResult(
                success=True,
                data=cached[1],
                execution_time=time.monotonic() - t0,
            )

        try:
            if self._provider:
                raw = await self._provider(symbol)
            else:
                raw = _mock_price(symbol)

            bid = float(raw["bid"])
            ask = float(raw["ask"])
            last = float(raw["last"])
            spread = ask - bid
            mid = (bid + ask) / 2.0
            spread_pct = (spread / mid * 100) if mid else 0.0

            result_data = {
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "last": last,
                "volume_24h": float(raw.get("volume_24h", 0)),
                "funding_rate": float(raw.get("funding_rate", 0)),
                "spread": round(spread, 8),
                "spread_pct": round(spread_pct, 6),
                "mid_price": round(mid, 8),
            }
            self._cache[symbol] = (now, result_data)
            return ToolResult(
                success=True,
                data=result_data,
                execution_time=time.monotonic() - t0,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                data={},
                error=str(exc),
                execution_time=time.monotonic() - t0,
            )


def _mock_price(symbol: str) -> dict[str, float]:
    prices: dict[str, float] = {
        "BTCUSDT": 65000.0,
        "ETHUSDT": 3500.0,
        "SOLUSDT": 150.0,
    }
    base = prices.get(symbol, 1000.0)
    spread = base * 0.0001
    return {
        "bid": base - spread / 2,
        "ask": base + spread / 2,
        "last": base,
        "volume_24h": 1_000_000.0,
        "funding_rate": 0.0001,
    }


# ---------------------------------------------------------------------------
# OrderbookDepthTool
# ---------------------------------------------------------------------------

OrderbookProvider = Callable[[str, int], Awaitable[dict[str, list]]]


class OrderbookDepthTool(BaseTool):
    """Analyzes orderbook depth and imbalance."""

    name = "orderbook_depth"
    description = "Analyze orderbook bid/ask depth and imbalance"

    def __init__(self, orderbook_provider: OrderbookProvider | None = None) -> None:
        self._provider = orderbook_provider

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        t0 = time.monotonic()
        symbol = params.get("symbol")
        if not symbol or not isinstance(symbol, str):
            return ToolResult(
                success=False,
                data={},
                error="Missing or invalid 'symbol' parameter",
                execution_time=time.monotonic() - t0,
            )
        symbol = symbol.upper()
        depth = int(params.get("depth", 20))

        try:
            if self._provider:
                raw = await self._provider(symbol, depth)
            else:
                raw = _mock_orderbook(symbol, depth)

            bids: list[list[float]] = raw["bids"]
            asks: list[list[float]] = raw["asks"]

            total_bid_vol = sum(q for _, q in bids)
            total_ask_vol = sum(q for _, q in asks)
            total = total_bid_vol + total_ask_vol
            imbalance = ((total_bid_vol - total_ask_vol) / total) if total else 0.0

            bid_wall = max(bids, key=lambda x: x[1]) if bids else [0, 0]
            ask_wall = max(asks, key=lambda x: x[1]) if asks else [0, 0]

            best_bid = bids[0][0] if bids else 0
            best_ask = asks[0][0] if asks else 0
            spread = best_ask - best_bid if (best_ask and best_bid) else 0

            return ToolResult(
                success=True,
                data={
                    "symbol": symbol,
                    "depth": depth,
                    "total_bid_volume": round(total_bid_vol, 4),
                    "total_ask_volume": round(total_ask_vol, 4),
                    "imbalance": round(imbalance, 4),
                    "bid_wall_price": bid_wall[0],
                    "ask_wall_price": ask_wall[0],
                    "spread": round(spread, 8),
                },
                execution_time=time.monotonic() - t0,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                data={},
                error=str(exc),
                execution_time=time.monotonic() - t0,
            )


def _mock_orderbook(symbol: str, depth: int) -> dict[str, list]:
    base = {"BTCUSDT": 65000.0, "ETHUSDT": 3500.0, "SOLUSDT": 150.0}.get(symbol, 1000.0)
    step = base * 0.0001
    bids = [[base - step * (i + 1), 1.0 + i * 0.5] for i in range(depth)]
    asks = [[base + step * (i + 1), 1.0 + i * 0.5] for i in range(depth)]
    return {"bids": bids, "asks": asks}


# ---------------------------------------------------------------------------
# LiquidationMapTool
# ---------------------------------------------------------------------------

LiquidationProvider = Callable[[str, float], Awaitable[list[dict]]]


class LiquidationMapTool(BaseTool):
    """Estimates liquidation clusters around current price."""

    name = "liquidation_map"
    description = "Estimate liquidation clusters based on price levels and round numbers"

    def __init__(self, liquidation_provider: LiquidationProvider | None = None) -> None:
        self._provider = liquidation_provider

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        t0 = time.monotonic()
        symbol = params.get("symbol")
        current_price = params.get("current_price")
        if not symbol or current_price is None:
            return ToolResult(
                success=False,
                data={},
                error="Missing 'symbol' or 'current_price' parameter",
                execution_time=time.monotonic() - t0,
            )

        symbol = str(symbol).upper()
        current_price = float(current_price)
        recent_high = float(params.get("recent_high", current_price * 1.03))
        recent_low = float(params.get("recent_low", current_price * 0.97))

        try:
            if self._provider:
                raw_zones = await self._provider(symbol, current_price)
                zones = raw_zones
            else:
                zones = self._estimate_zones(current_price, recent_high, recent_low)

            return ToolResult(
                success=True,
                data={
                    "symbol": symbol,
                    "current_price": current_price,
                    "zones": zones,
                },
                execution_time=time.monotonic() - t0,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                data={},
                error=str(exc),
                execution_time=time.monotonic() - t0,
            )

    @staticmethod
    def _estimate_zones(
        current_price: float,
        recent_high: float,
        recent_low: float,
    ) -> list[dict[str, Any]]:
        zones: list[dict[str, Any]] = []
        pct_levels = [0.01, 0.02, 0.05]

        for pct in pct_levels:
            # Below price: long liquidations
            lvl = current_price * (1 - pct)
            density = 0.8 if pct == 0.02 else 0.5
            if lvl >= recent_low:
                density = min(density + 0.15, 1.0)
            zones.append(
                {
                    "price": round(lvl, 2),
                    "side": "long",
                    "estimated_density": round(density, 2),
                    "reason": f"{pct*100:.0f}% below current price",
                }
            )

            # Above price: short liquidations
            lvl = current_price * (1 + pct)
            density = 0.8 if pct == 0.02 else 0.5
            if lvl <= recent_high:
                density = min(density + 0.15, 1.0)
            zones.append(
                {
                    "price": round(lvl, 2),
                    "side": "short",
                    "estimated_density": round(density, 2),
                    "reason": f"{pct*100:.0f}% above current price",
                }
            )

        # Round number zones
        magnitude = 10 ** (len(str(int(current_price))) - 2)
        round_below = (int(current_price) // magnitude) * magnitude
        round_above = round_below + magnitude
        if round_below > 0 and round_below != current_price:
            zones.append(
                {
                    "price": float(round_below),
                    "side": "long",
                    "estimated_density": 0.7,
                    "reason": "round number support",
                }
            )
        if round_above != current_price:
            zones.append(
                {
                    "price": float(round_above),
                    "side": "short",
                    "estimated_density": 0.7,
                    "reason": "round number resistance",
                }
            )

        zones.sort(key=lambda z: z["price"])
        return zones


# ---------------------------------------------------------------------------
# OnChainTool
# ---------------------------------------------------------------------------

OnChainProvider = Callable[[str], Awaitable[dict[str, float]]]


class OnChainTool(BaseTool):
    """On-chain exchange flow analysis."""

    name = "onchain_flow"
    description = "Analyze on-chain exchange inflows/outflows and whale activity"

    def __init__(self, onchain_provider: OnChainProvider | None = None) -> None:
        self._provider = onchain_provider

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        t0 = time.monotonic()
        symbol = params.get("symbol")
        if not symbol or not isinstance(symbol, str):
            return ToolResult(
                success=False,
                data={},
                error="Missing or invalid 'symbol' parameter",
                execution_time=time.monotonic() - t0,
            )
        symbol = symbol.upper()

        try:
            if self._provider:
                raw = await self._provider(symbol)
            else:
                raw = _mock_onchain(symbol)

            return ToolResult(
                success=True,
                data={
                    "symbol": symbol,
                    "exchange_inflow": float(raw["exchange_inflow"]),
                    "exchange_outflow": float(raw["exchange_outflow"]),
                    "net_flow": float(raw["net_flow"]),
                    "whale_transactions": int(raw["whale_transactions"]),
                    "exchange_reserve_change_pct": float(raw["exchange_reserve_change_pct"]),
                },
                execution_time=time.monotonic() - t0,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                data={},
                error=str(exc),
                execution_time=time.monotonic() - t0,
            )


def _mock_onchain(symbol: str) -> dict[str, float]:
    return {
        "exchange_inflow": 5000.0,
        "exchange_outflow": 5000.0,
        "net_flow": 0.0,
        "whale_transactions": 12,
        "exchange_reserve_change_pct": 0.0,
    }


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Registry that manages and dispatches agent tools."""

    def __init__(self, register_defaults: bool = True) -> None:
        self._tools: dict[str, BaseTool] = {}
        if register_defaults:
            self.register(LivePriceTool())
            self.register(OrderbookDepthTool())
            self.register(LiquidationMapTool())
            self.register(OnChainTool())

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list(self) -> list[str]:
        return list(self._tools.keys())

    async def execute(self, tool_name: str, params: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult(
                success=False,
                data={},
                error=f"Unknown tool: {tool_name}",
            )
        return await tool.execute(params)
