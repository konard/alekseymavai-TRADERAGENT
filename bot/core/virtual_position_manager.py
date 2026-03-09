"""
VirtualPositionManager — single source of truth for all positions across all strategies.

The exchange only receives simple buy/sell orders with qty.
All SL/TP/trailing logic is tracked bot-side in VirtualPosition objects.

Concept::

    VirtualPositionManager.check_exits(price)
      → [(pos, "take_profit"), (pos, "stop_loss"), ...]

    ExchangeExecutor.close_long(symbol, qty)
      → create_order(side="sell", reduceOnly=True)

Design goals
------------
* Thread-safe: all mutations are protected by ``asyncio.Lock()`` so concurrent
  WebSocket ticks cannot corrupt position state.
* Strategy-agnostic: any of Grid / DCA / TF / SMC can register positions.
* Archive: closed positions are kept in ``_archive`` for PnL reporting.
* Aggregate SL for Grid: if total unrealised loss / total invested exceeds
  ``max_grid_loss_pct``, all Grid positions are returned for closure.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from bot.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# VirtualPosition
# ---------------------------------------------------------------------------


@dataclass
class VirtualPosition:
    """
    A single bot-side (virtual) position for one strategy fill.

    The exchange is unaware of SL/TP prices — the bot tracks them here and
    issues close orders when prices are hit.

    Fields
    ------
    pos_id
        Globally unique identifier (UUID4 hex).
    strategy
        Strategy that owns this position: ``"grid" | "dca" | "tf" | "smc"``.
    symbol
        Trading pair in CCXT notation, e.g. ``"BTC/USDT"``.
    side
        ``"long"`` or ``"short"``.
    qty
        Position size in base currency (positive, non-zero).
    entry_price
        Average fill price.
    tp_price
        Take-profit price.  ``None`` = no TP.
    sl_price
        Stop-loss price.   ``None`` = no SL.
    opened_at
        UTC timestamp of position open.
    meta
        Arbitrary strategy-specific metadata, e.g.
        ``{"grid_level": 5, "safety_order": 2}``.
    """

    pos_id: str
    strategy: str
    symbol: str
    side: str
    qty: Decimal
    entry_price: Decimal
    tp_price: Decimal | None = None
    sl_price: Decimal | None = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"qty must be positive, got {self.qty}")
        if self.entry_price <= 0:
            raise ValueError(f"entry_price must be positive, got {self.entry_price}")
        if self.side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got {self.side!r}")

    # ------------------------------------------------------------------
    # Price checks
    # ------------------------------------------------------------------

    def is_tp_hit(self, price: Decimal) -> bool:
        """Return True when *price* reaches or passes the take-profit level."""
        if self.tp_price is None:
            return False
        if self.side == "long":
            return price >= self.tp_price
        return price <= self.tp_price  # short

    def is_sl_hit(self, price: Decimal) -> bool:
        """Return True when *price* reaches or passes the stop-loss level."""
        if self.sl_price is None:
            return False
        if self.side == "long":
            return price <= self.sl_price
        return price >= self.sl_price  # short

    # ------------------------------------------------------------------
    # PnL helpers
    # ------------------------------------------------------------------

    def unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Unrealised PnL at *current_price* (not including fees)."""
        if self.side == "long":
            return (current_price - self.entry_price) * self.qty
        return (self.entry_price - current_price) * self.qty

    def calc_pnl(self, exit_price: Decimal) -> Decimal:
        """Realised PnL at *exit_price* (not including fees)."""
        return self.unrealized_pnl(exit_price)

    def __repr__(self) -> str:
        return (
            f"VirtualPosition(pos_id={self.pos_id!r}, strategy={self.strategy!r}, "
            f"symbol={self.symbol!r}, side={self.side!r}, qty={self.qty}, "
            f"entry={self.entry_price}, tp={self.tp_price}, sl={self.sl_price})"
        )


# ---------------------------------------------------------------------------
# VirtualPositionManager
# ---------------------------------------------------------------------------


class VirtualPositionManager:
    """
    Central registry for all open virtual positions.

    All public methods that mutate state are async and acquire an
    ``asyncio.Lock`` to prevent race conditions from concurrent WebSocket ticks.

    Parameters
    ----------
    max_grid_loss_pct:
        Maximum allowed aggregate loss for Grid positions as a fraction of
        total invested capital (e.g. ``0.10`` = −10 %).  When exceeded,
        ``check_exits`` marks all Grid positions for closure.
    """

    def __init__(self, max_grid_loss_pct: float = 0.10) -> None:
        self._positions: dict[str, VirtualPosition] = {}
        self._archive: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._max_grid_loss_pct = Decimal(str(max_grid_loss_pct))

    # ------------------------------------------------------------------
    # Open
    # ------------------------------------------------------------------

    async def open(self, pos: VirtualPosition) -> str:
        """
        Register a new virtual position.

        Parameters
        ----------
        pos:
            A ``VirtualPosition`` instance.  Its ``pos_id`` is used as-is;
            callers should use :func:`new_position` to generate a fresh UUID.

        Returns
        -------
        str
            The ``pos_id`` of the registered position.
        """
        async with self._lock:
            if pos.pos_id in self._positions:
                raise ValueError(f"Position {pos.pos_id} already registered")
            self._positions[pos.pos_id] = pos
            logger.info(
                "vpm_position_opened",
                pos_id=pos.pos_id,
                strategy=pos.strategy,
                symbol=pos.symbol,
                side=pos.side,
                qty=float(pos.qty),
                entry_price=float(pos.entry_price),
                tp_price=float(pos.tp_price) if pos.tp_price else None,
                sl_price=float(pos.sl_price) if pos.sl_price else None,
            )
            return pos.pos_id

    # ------------------------------------------------------------------
    # Check exits
    # ------------------------------------------------------------------

    async def check_exits(
        self, price: Decimal
    ) -> list[tuple[VirtualPosition, str]]:
        """
        Scan all open positions and return those whose SL or TP is hit.

        Called on every WebSocket price tick (or REST fallback).

        For Grid positions an additional *aggregate SL* check is performed:
        if the total unrealised loss across all Grid positions exceeds
        ``max_grid_loss_pct × total invested``, all Grid positions are
        returned for closure with reason ``"aggregate_sl"``.

        Parameters
        ----------
        price:
            Current market price.

        Returns
        -------
        list[tuple[VirtualPosition, str]]
            Each tuple is ``(position, reason)`` where reason is one of
            ``"take_profit"``, ``"stop_loss"``, or ``"aggregate_sl"``.
            Positions are NOT removed here — call :meth:`close` after
            executing the actual exchange order.
        """
        exits: list[tuple[VirtualPosition, str]] = []
        exits_set: set[str] = set()

        async with self._lock:
            for pos in list(self._positions.values()):
                if pos.is_tp_hit(price):
                    exits.append((pos, "take_profit"))
                    exits_set.add(pos.pos_id)
                    logger.debug(
                        "vpm_tp_hit",
                        pos_id=pos.pos_id,
                        symbol=pos.symbol,
                        price=float(price),
                        tp=float(pos.tp_price),  # type: ignore[arg-type]
                    )
                elif pos.is_sl_hit(price):
                    exits.append((pos, "stop_loss"))
                    exits_set.add(pos.pos_id)
                    logger.debug(
                        "vpm_sl_hit",
                        pos_id=pos.pos_id,
                        symbol=pos.symbol,
                        price=float(price),
                        sl=float(pos.sl_price),  # type: ignore[arg-type]
                    )

            # ---- Aggregate SL for Grid ----
            grid_positions = [
                p for p in self._positions.values() if p.strategy == "grid"
            ]
            if grid_positions:
                total_invested = sum(p.entry_price * p.qty for p in grid_positions)
                total_unrealized = sum(p.unrealized_pnl(price) for p in grid_positions)
                if total_invested > 0:
                    loss_pct = -total_unrealized / total_invested
                    if loss_pct >= self._max_grid_loss_pct:
                        logger.warning(
                            "vpm_aggregate_sl_triggered",
                            loss_pct=float(loss_pct),
                            max_loss_pct=float(self._max_grid_loss_pct),
                            grid_position_count=len(grid_positions),
                        )
                        for pos in grid_positions:
                            if pos.pos_id not in exits_set:
                                exits.append((pos, "aggregate_sl"))
                                exits_set.add(pos.pos_id)

        return exits

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    async def close(
        self,
        pos_id: str,
        exit_price: Decimal,
        reason: str,
    ) -> Decimal:
        """
        Mark a position as closed, archive it, and return the realised PnL.

        Parameters
        ----------
        pos_id:
            ID of the position to close.
        exit_price:
            Price at which the position was closed.
        reason:
            Human-readable close reason (e.g. ``"take_profit"``, ``"stop_loss"``).

        Returns
        -------
        Decimal
            Realised PnL (positive = profit, negative = loss).

        Raises
        ------
        KeyError
            If *pos_id* is not found in open positions.
        """
        async with self._lock:
            pos = self._positions.pop(pos_id, None)
            if pos is None:
                raise KeyError(f"Position {pos_id!r} not found in open positions")

            pnl = pos.calc_pnl(exit_price)
            record: dict[str, Any] = {
                "pos_id": pos.pos_id,
                "strategy": pos.strategy,
                "symbol": pos.symbol,
                "side": pos.side,
                "qty": pos.qty,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "tp_price": pos.tp_price,
                "sl_price": pos.sl_price,
                "opened_at": pos.opened_at,
                "closed_at": datetime.now(timezone.utc),
                "reason": reason,
                "pnl": pnl,
                "meta": pos.meta,
            }
            self._archive.append(record)

            logger.info(
                "vpm_position_closed",
                pos_id=pos_id,
                strategy=pos.strategy,
                symbol=pos.symbol,
                side=pos.side,
                exit_price=float(exit_price),
                pnl=float(pnl),
                reason=reason,
            )
            return pnl

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def net_qty(self, symbol: str, side: str) -> Decimal:
        """
        Return total open quantity for a given *symbol* and *side*.

        Used by reconcile logic and for placing the backup SL.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTC/USDT"``.
        side:
            ``"long"`` or ``"short"``.

        Returns
        -------
        Decimal
            Sum of ``qty`` for all matching open positions.
        """
        async with self._lock:
            return sum(
                (
                    p.qty
                    for p in self._positions.values()
                    if p.symbol == symbol and p.side == side
                ),
                Decimal("0"),
            )

    async def get_by_strategy(self, strategy: str) -> list[VirtualPosition]:
        """
        Return a snapshot of all open positions for the given *strategy*.

        Parameters
        ----------
        strategy:
            One of ``"grid"``, ``"dca"``, ``"tf"``, ``"smc"``.

        Returns
        -------
        list[VirtualPosition]
            Copies of matching open positions (safe to iterate outside the lock).
        """
        async with self._lock:
            return [p for p in self._positions.values() if p.strategy == strategy]

    async def get_all(self) -> list[VirtualPosition]:
        """Return a snapshot of all open positions."""
        async with self._lock:
            return list(self._positions.values())

    # ------------------------------------------------------------------
    # Archive
    # ------------------------------------------------------------------

    @property
    def archive(self) -> list[dict[str, Any]]:
        """
        Read-only view of closed-position records.

        Each record is a plain dict with keys: pos_id, strategy, symbol, side,
        qty, entry_price, exit_price, tp_price, sl_price, opened_at, closed_at,
        reason, pnl, meta.
        """
        return list(self._archive)

    # ------------------------------------------------------------------
    # Factory helper
    # ------------------------------------------------------------------

    @staticmethod
    def new_position(
        strategy: str,
        symbol: str,
        side: str,
        qty: Decimal,
        entry_price: Decimal,
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
        meta: dict[str, Any] | None = None,
    ) -> VirtualPosition:
        """
        Convenience factory that creates a :class:`VirtualPosition` with a fresh UUID.

        Parameters
        ----------
        strategy:
            ``"grid"`` | ``"dca"`` | ``"tf"`` | ``"smc"``.
        symbol:
            Trading pair, e.g. ``"BTC/USDT"``.
        side:
            ``"long"`` or ``"short"``.
        qty:
            Quantity in base currency (must be > 0).
        entry_price:
            Average fill price (must be > 0).
        tp_price:
            Optional take-profit price.
        sl_price:
            Optional stop-loss price.
        meta:
            Optional strategy-specific metadata dict.

        Returns
        -------
        VirtualPosition
            A new position with a unique ``pos_id``.
        """
        return VirtualPosition(
            pos_id=uuid.uuid4().hex,
            strategy=strategy,
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
            meta=meta or {},
        )
