"""
ExchangeExecutor — thin wrapper over ExecutionLayer for position open/close.

All *close* methods unconditionally pass ``reduceOnly=True`` to the exchange
so they can never accidentally open a new position in the opposite direction.

Concept::

    ExchangeExecutor.close_long(symbol, qty)
      → create_order(side="sell", reduceOnly=True)

Usage (live)::

    client = ByBitDirectClient(...)
    layer  = LiveExecutionLayer(client)
    executor = ExchangeExecutor(layer)
    await executor.open_long("BTC/USDT", qty=Decimal("0.01"))
    await executor.close_long("BTC/USDT", qty=Decimal("0.01"))

Usage (backtest)::

    layer    = BacktestExecutionLayer()
    executor = ExchangeExecutor(layer)
    # Identical API — no exchange calls are made.

Design
------
* ``open_long`` / ``open_short`` place plain limit or market orders.
* ``close_long`` / ``close_short`` ALWAYS add ``{"reduceOnly": True}`` to
  params — this rule has no exceptions.
* A ``price=None`` argument sends a market order; ``price != None`` sends
  a limit order.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from bot.core.trading_core.execution_layer import ExecutionLayer
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class ExchangeExecutor:
    """
    High-level order executor that maps position semantics to exchange orders.

    Parameters
    ----------
    layer:
        Any :class:`~bot.core.trading_core.execution_layer.ExecutionLayer`
        implementation (live or backtest).
    """

    def __init__(self, layer: ExecutionLayer) -> None:
        self._layer = layer

    # ------------------------------------------------------------------
    # Open helpers
    # ------------------------------------------------------------------

    async def open_long(
        self,
        symbol: str,
        qty: Decimal,
        price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Open a long (buy) position.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTC/USDT"``.
        qty:
            Quantity in base currency (must be > 0).
        price:
            Limit price.  Pass ``None`` for a market order.
        params:
            Extra exchange-specific parameters merged with the order.

        Returns
        -------
        dict
            Raw order response from the exchange / simulator.
        """
        order_type = "limit" if price is not None else "market"
        extra: dict[str, Any] = dict(params or {})
        result = await self._layer.create_order(
            symbol=symbol,
            order_type=order_type,
            side="buy",
            amount=float(qty),
            price=float(price) if price is not None else None,
            params=extra or None,
        )
        logger.info(
            "executor_open_long",
            symbol=symbol,
            qty=float(qty),
            price=float(price) if price is not None else None,
            order_id=result.get("id"),
        )
        return result

    async def open_short(
        self,
        symbol: str,
        qty: Decimal,
        price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Open a short (sell) position.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTC/USDT"``.
        qty:
            Quantity in base currency (must be > 0).
        price:
            Limit price.  Pass ``None`` for a market order.
        params:
            Extra exchange-specific parameters merged with the order.

        Returns
        -------
        dict
            Raw order response from the exchange / simulator.
        """
        order_type = "limit" if price is not None else "market"
        extra: dict[str, Any] = dict(params or {})
        result = await self._layer.create_order(
            symbol=symbol,
            order_type=order_type,
            side="sell",
            amount=float(qty),
            price=float(price) if price is not None else None,
            params=extra or None,
        )
        logger.info(
            "executor_open_short",
            symbol=symbol,
            qty=float(qty),
            price=float(price) if price is not None else None,
            order_id=result.get("id"),
        )
        return result

    # ------------------------------------------------------------------
    # Close helpers — reduceOnly is ALWAYS True
    # ------------------------------------------------------------------

    async def close_long(
        self,
        symbol: str,
        qty: Decimal,
        price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Close a long position (sell with ``reduceOnly=True``).

        ``reduceOnly`` is **always** set regardless of what *params* contains.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTC/USDT"``.
        qty:
            Quantity to close in base currency (must be > 0).
        price:
            Limit price.  Pass ``None`` for a market order.
        params:
            Additional exchange parameters.  ``reduceOnly`` will be forced
            to ``True`` even if caller passes ``False`` here.

        Returns
        -------
        dict
            Raw order response from the exchange / simulator.
        """
        order_type = "limit" if price is not None else "market"
        extra: dict[str, Any] = dict(params or {})
        extra["reduceOnly"] = True  # non-negotiable
        result = await self._layer.create_order(
            symbol=symbol,
            order_type=order_type,
            side="sell",
            amount=float(qty),
            price=float(price) if price is not None else None,
            params=extra,
        )
        logger.info(
            "executor_close_long",
            symbol=symbol,
            qty=float(qty),
            price=float(price) if price is not None else None,
            reduce_only=True,
            order_id=result.get("id"),
        )
        return result

    async def close_short(
        self,
        symbol: str,
        qty: Decimal,
        price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Close a short position (buy with ``reduceOnly=True``).

        ``reduceOnly`` is **always** set regardless of what *params* contains.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTC/USDT"``.
        qty:
            Quantity to close in base currency (must be > 0).
        price:
            Limit price.  Pass ``None`` for a market order.
        params:
            Additional exchange parameters.  ``reduceOnly`` will be forced
            to ``True`` even if caller passes ``False`` here.

        Returns
        -------
        dict
            Raw order response from the exchange / simulator.
        """
        order_type = "limit" if price is not None else "market"
        extra: dict[str, Any] = dict(params or {})
        extra["reduceOnly"] = True  # non-negotiable
        result = await self._layer.create_order(
            symbol=symbol,
            order_type=order_type,
            side="buy",
            amount=float(qty),
            price=float(price) if price is not None else None,
            params=extra,
        )
        logger.info(
            "executor_close_short",
            symbol=symbol,
            qty=float(qty),
            price=float(price) if price is not None else None,
            reduce_only=True,
            order_id=result.get("id"),
        )
        return result
