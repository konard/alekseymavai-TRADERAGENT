"""
Unit tests for VirtualPosition and VirtualPositionManager.

Issue #378 — Virtual Position Manager (VPM) — ядро V3.0

Coverage
--------
* VirtualPosition: field validation, TP/SL hit logic for long and short,
  unrealised/realised PnL helpers.
* VirtualPositionManager:
  - open() registers position and returns pos_id
  - check_exits() triggers take_profit / stop_loss correctly
  - check_exits() triggers aggregate_sl when total Grid loss > threshold
  - close() returns PnL and archives record; raises on unknown pos_id
  - net_qty() sums correctly across multiple positions
  - get_by_strategy() filters correctly
  - asyncio.Lock() — concurrent ticks do not corrupt state
* ExchangeExecutor:
  - open_long / open_short place correct side
  - close_long / close_short ALWAYS set reduceOnly=True
  - reduceOnly cannot be overridden by caller
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.core.exchange_executor import ExchangeExecutor
from bot.core.trading_core.execution_layer import BacktestExecutionLayer
from bot.core.virtual_position_manager import VirtualPosition, VirtualPositionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _long(
    strategy: str = "grid",
    symbol: str = "BTC/USDT",
    qty: float = 0.1,
    entry: float = 40_000.0,
    tp: float | None = 44_000.0,
    sl: float | None = 38_000.0,
) -> VirtualPosition:
    """Build a sample long VirtualPosition."""
    return VirtualPositionManager.new_position(
        strategy=strategy,
        symbol=symbol,
        side="long",
        qty=Decimal(str(qty)),
        entry_price=Decimal(str(entry)),
        tp_price=Decimal(str(tp)) if tp is not None else None,
        sl_price=Decimal(str(sl)) if sl is not None else None,
    )


def _short(
    strategy: str = "smc",
    symbol: str = "BTC/USDT",
    qty: float = 0.1,
    entry: float = 40_000.0,
    tp: float | None = 36_000.0,
    sl: float | None = 42_000.0,
) -> VirtualPosition:
    """Build a sample short VirtualPosition."""
    return VirtualPositionManager.new_position(
        strategy=strategy,
        symbol=symbol,
        side="short",
        qty=Decimal(str(qty)),
        entry_price=Decimal(str(entry)),
        tp_price=Decimal(str(tp)) if tp is not None else None,
        sl_price=Decimal(str(sl)) if sl is not None else None,
    )


# ===========================================================================
# VirtualPosition unit tests
# ===========================================================================


class TestVirtualPositionValidation:
    """VirtualPosition rejects invalid field values."""

    def test_negative_qty_raises(self) -> None:
        with pytest.raises(ValueError, match="qty must be positive"):
            VirtualPosition(
                pos_id="abc",
                strategy="grid",
                symbol="BTC/USDT",
                side="long",
                qty=Decimal("-1"),
                entry_price=Decimal("40000"),
            )

    def test_zero_qty_raises(self) -> None:
        with pytest.raises(ValueError, match="qty must be positive"):
            VirtualPosition(
                pos_id="abc",
                strategy="grid",
                symbol="BTC/USDT",
                side="long",
                qty=Decimal("0"),
                entry_price=Decimal("40000"),
            )

    def test_zero_entry_price_raises(self) -> None:
        with pytest.raises(ValueError, match="entry_price must be positive"):
            VirtualPosition(
                pos_id="abc",
                strategy="grid",
                symbol="BTC/USDT",
                side="long",
                qty=Decimal("0.1"),
                entry_price=Decimal("0"),
            )

    def test_invalid_side_raises(self) -> None:
        with pytest.raises(ValueError, match="side must be"):
            VirtualPosition(
                pos_id="abc",
                strategy="grid",
                symbol="BTC/USDT",
                side="up",
                qty=Decimal("0.1"),
                entry_price=Decimal("40000"),
            )


class TestVirtualPositionTpSlLogic:
    """TP / SL hit detection for long and short sides."""

    # ---- Long positions ----

    def test_long_tp_hit_at_exact_price(self) -> None:
        pos = _long(entry=40_000, tp=44_000)
        assert pos.is_tp_hit(Decimal("44000")) is True

    def test_long_tp_hit_above_tp(self) -> None:
        pos = _long(entry=40_000, tp=44_000)
        assert pos.is_tp_hit(Decimal("45000")) is True

    def test_long_tp_not_hit_below_tp(self) -> None:
        pos = _long(entry=40_000, tp=44_000)
        assert pos.is_tp_hit(Decimal("43999")) is False

    def test_long_sl_hit_at_exact_price(self) -> None:
        pos = _long(entry=40_000, sl=38_000)
        assert pos.is_sl_hit(Decimal("38000")) is True

    def test_long_sl_hit_below_sl(self) -> None:
        pos = _long(entry=40_000, sl=38_000)
        assert pos.is_sl_hit(Decimal("37999")) is True

    def test_long_sl_not_hit_above_sl(self) -> None:
        pos = _long(entry=40_000, sl=38_000)
        assert pos.is_sl_hit(Decimal("38001")) is False

    def test_long_no_tp_never_triggered(self) -> None:
        pos = _long(entry=40_000, tp=None)
        assert pos.is_tp_hit(Decimal("999999")) is False

    def test_long_no_sl_never_triggered(self) -> None:
        pos = _long(entry=40_000, sl=None)
        assert pos.is_sl_hit(Decimal("1")) is False

    # ---- Short positions ----

    def test_short_tp_hit_at_exact_price(self) -> None:
        pos = _short(entry=40_000, tp=36_000)
        assert pos.is_tp_hit(Decimal("36000")) is True

    def test_short_tp_hit_below_tp(self) -> None:
        pos = _short(entry=40_000, tp=36_000)
        assert pos.is_tp_hit(Decimal("35000")) is True

    def test_short_tp_not_hit_above_tp(self) -> None:
        pos = _short(entry=40_000, tp=36_000)
        assert pos.is_tp_hit(Decimal("36001")) is False

    def test_short_sl_hit_at_exact_price(self) -> None:
        pos = _short(entry=40_000, sl=42_000)
        assert pos.is_sl_hit(Decimal("42000")) is True

    def test_short_sl_hit_above_sl(self) -> None:
        pos = _short(entry=40_000, sl=42_000)
        assert pos.is_sl_hit(Decimal("43000")) is True

    def test_short_sl_not_hit_below_sl(self) -> None:
        pos = _short(entry=40_000, sl=42_000)
        assert pos.is_sl_hit(Decimal("41999")) is False


class TestVirtualPositionPnL:
    """Unrealised and realised PnL helpers."""

    def test_long_unrealized_profit(self) -> None:
        pos = _long(entry=40_000, qty=0.5, tp=None, sl=None)
        # price rises to 42_000 → gain 2_000 × 0.5 = 1_000
        assert pos.unrealized_pnl(Decimal("42000")) == Decimal("1000.0")

    def test_long_unrealized_loss(self) -> None:
        pos = _long(entry=40_000, qty=0.5, tp=None, sl=None)
        # price falls to 38_000 → loss 2_000 × 0.5 = -1_000
        assert pos.unrealized_pnl(Decimal("38000")) == Decimal("-1000.0")

    def test_short_unrealized_profit(self) -> None:
        pos = _short(entry=40_000, qty=0.5, tp=None, sl=None)
        # price falls to 38_000 → gain 2_000 × 0.5 = 1_000
        assert pos.unrealized_pnl(Decimal("38000")) == Decimal("1000.0")

    def test_short_unrealized_loss(self) -> None:
        pos = _short(entry=40_000, qty=0.5, tp=None, sl=None)
        # price rises to 42_000 → loss 2_000 × 0.5 = -1_000
        assert pos.unrealized_pnl(Decimal("42000")) == Decimal("-1000.0")

    def test_calc_pnl_equals_unrealized_at_exit_price(self) -> None:
        pos = _long(entry=40_000, qty=1.0, tp=None, sl=None)
        assert pos.calc_pnl(Decimal("41000")) == pos.unrealized_pnl(Decimal("41000"))


# ===========================================================================
# VirtualPositionManager unit tests
# ===========================================================================


class TestVPMOpen:
    """open() registers positions correctly."""

    @pytest.mark.asyncio
    async def test_open_returns_pos_id(self) -> None:
        vpm = VirtualPositionManager()
        pos = _long()
        result = await vpm.open(pos)
        assert result == pos.pos_id

    @pytest.mark.asyncio
    async def test_duplicate_open_raises(self) -> None:
        vpm = VirtualPositionManager()
        pos = _long()
        await vpm.open(pos)
        with pytest.raises(ValueError, match="already registered"):
            await vpm.open(pos)

    @pytest.mark.asyncio
    async def test_open_multiple_positions(self) -> None:
        vpm = VirtualPositionManager()
        pos1 = _long(strategy="grid")
        pos2 = _long(strategy="dca")
        await vpm.open(pos1)
        await vpm.open(pos2)
        all_pos = await vpm.get_all()
        assert len(all_pos) == 2


class TestVPMCheckExits:
    """check_exits() triggers TP / SL for long and short correctly."""

    @pytest.mark.asyncio
    async def test_long_tp_triggered(self) -> None:
        vpm = VirtualPositionManager()
        pos = _long(entry=40_000, tp=44_000, sl=38_000)
        await vpm.open(pos)

        exits = await vpm.check_exits(Decimal("44000"))
        assert len(exits) == 1
        assert exits[0] == (pos, "take_profit")

    @pytest.mark.asyncio
    async def test_long_sl_triggered(self) -> None:
        vpm = VirtualPositionManager()
        pos = _long(entry=40_000, tp=44_000, sl=38_000)
        await vpm.open(pos)

        exits = await vpm.check_exits(Decimal("37999"))
        assert len(exits) == 1
        assert exits[0] == (pos, "stop_loss")

    @pytest.mark.asyncio
    async def test_short_tp_triggered(self) -> None:
        vpm = VirtualPositionManager()
        pos = _short(entry=40_000, tp=36_000, sl=42_000)
        await vpm.open(pos)

        exits = await vpm.check_exits(Decimal("36000"))
        assert len(exits) == 1
        assert exits[0] == (pos, "take_profit")

    @pytest.mark.asyncio
    async def test_short_sl_triggered(self) -> None:
        vpm = VirtualPositionManager()
        pos = _short(entry=40_000, tp=36_000, sl=42_000)
        await vpm.open(pos)

        exits = await vpm.check_exits(Decimal("42001"))
        assert len(exits) == 1
        assert exits[0] == (pos, "stop_loss")

    @pytest.mark.asyncio
    async def test_no_exit_between_sl_and_tp(self) -> None:
        vpm = VirtualPositionManager()
        pos = _long(entry=40_000, tp=44_000, sl=38_000)
        await vpm.open(pos)

        exits = await vpm.check_exits(Decimal("41000"))
        assert exits == []

    @pytest.mark.asyncio
    async def test_multiple_positions_mixed_exits(self) -> None:
        """Only positions whose SL/TP is hit are returned."""
        vpm = VirtualPositionManager()
        pos1 = _long(entry=40_000, tp=44_000, sl=38_000)  # TP at 44k
        pos2 = _long(entry=40_000, tp=50_000, sl=38_000)  # TP at 50k — not hit

        await vpm.open(pos1)
        await vpm.open(pos2)

        exits = await vpm.check_exits(Decimal("44000"))
        assert len(exits) == 1
        assert exits[0][0].pos_id == pos1.pos_id
        assert exits[0][1] == "take_profit"

    @pytest.mark.asyncio
    async def test_check_exits_does_not_remove_position(self) -> None:
        """check_exits must NOT remove positions — close() must be called separately."""
        vpm = VirtualPositionManager()
        pos = _long(entry=40_000, tp=44_000, sl=38_000)
        await vpm.open(pos)

        await vpm.check_exits(Decimal("44000"))  # TP hit

        # Position still present
        remaining = await vpm.get_all()
        assert len(remaining) == 1

    @pytest.mark.asyncio
    async def test_tp_takes_priority_over_sl_logic(self) -> None:
        """
        If a position has both TP and SL and price hits TP first, only
        take_profit should be returned (not stop_loss).
        """
        vpm = VirtualPositionManager()
        # Unusual setup: entry very low, tp very low too (already triggered)
        pos = _long(entry=40_000, tp=44_000, sl=38_000)
        await vpm.open(pos)

        # Price is above TP → only take_profit
        exits = await vpm.check_exits(Decimal("45000"))
        reasons = [reason for _, reason in exits]
        assert reasons == ["take_profit"]


class TestVPMAggregateSlGrid:
    """Aggregate SL closes all Grid positions when total loss exceeds threshold."""

    @pytest.mark.asyncio
    async def test_aggregate_sl_triggered_at_threshold(self) -> None:
        """
        3 Grid positions each with -10% loss → aggregate_sl fires at 10% threshold.
        """
        vpm = VirtualPositionManager(max_grid_loss_pct=0.10)

        # Entry: 40_000, qty: 1 each → invested 40_000 each
        pos1 = VirtualPositionManager.new_position(
            "grid", "BTC/USDT", "long", Decimal("1"), Decimal("40000"),
            tp_price=Decimal("44000"), sl_price=None,
        )
        pos2 = VirtualPositionManager.new_position(
            "grid", "BTC/USDT", "long", Decimal("1"), Decimal("40000"),
            tp_price=Decimal("44000"), sl_price=None,
        )
        pos3 = VirtualPositionManager.new_position(
            "grid", "BTC/USDT", "long", Decimal("1"), Decimal("40000"),
            tp_price=Decimal("44000"), sl_price=None,
        )
        await vpm.open(pos1)
        await vpm.open(pos2)
        await vpm.open(pos3)

        # Price drops to 36_000 → loss per position = (40_000 - 36_000) × 1 = -4_000
        # total loss = -12_000; total invested = 120_000; loss_pct = 10%  ≥ 10%
        exits = await vpm.check_exits(Decimal("36000"))
        reasons = {reason for _, reason in exits}
        assert "aggregate_sl" in reasons
        assert len(exits) == 3  # all Grid positions

    @pytest.mark.asyncio
    async def test_aggregate_sl_not_triggered_below_threshold(self) -> None:
        vpm = VirtualPositionManager(max_grid_loss_pct=0.10)
        pos = VirtualPositionManager.new_position(
            "grid", "BTC/USDT", "long", Decimal("1"), Decimal("40000"),
            tp_price=Decimal("44000"), sl_price=None,
        )
        await vpm.open(pos)

        # 5% loss — below 10% threshold
        exits = await vpm.check_exits(Decimal("38000"))
        # No aggregate_sl; no individual SL either (sl_price is None)
        assert exits == []

    @pytest.mark.asyncio
    async def test_aggregate_sl_only_closes_grid_positions(self) -> None:
        """Non-Grid positions must NOT be closed by aggregate SL."""
        vpm = VirtualPositionManager(max_grid_loss_pct=0.10)

        grid_pos = VirtualPositionManager.new_position(
            "grid", "BTC/USDT", "long", Decimal("1"), Decimal("40000"),
            tp_price=Decimal("44000"), sl_price=None,
        )
        dca_pos = VirtualPositionManager.new_position(
            "dca", "BTC/USDT", "long", Decimal("1"), Decimal("40000"),
            tp_price=Decimal("44000"), sl_price=None,
        )
        await vpm.open(grid_pos)
        await vpm.open(dca_pos)

        # 10% loss
        exits = await vpm.check_exits(Decimal("36000"))
        exit_ids = {pos.pos_id for pos, _ in exits}
        assert grid_pos.pos_id in exit_ids
        assert dca_pos.pos_id not in exit_ids

    @pytest.mark.asyncio
    async def test_aggregate_sl_pos_not_duplicated_with_individual_sl(self) -> None:
        """
        If a Grid position also has individual SL set and price hits both, it
        should appear only once in the exits list.
        """
        vpm = VirtualPositionManager(max_grid_loss_pct=0.10)

        pos = VirtualPositionManager.new_position(
            "grid", "BTC/USDT", "long", Decimal("1"), Decimal("40000"),
            tp_price=Decimal("44000"),
            sl_price=Decimal("39000"),  # individual SL at 39k
        )
        await vpm.open(pos)

        # Price 36_000 hits individual SL (39_000 > 36_000) AND aggregate_sl (10%)
        exits = await vpm.check_exits(Decimal("36000"))
        # pos must appear exactly once
        pos_ids = [p.pos_id for p, _ in exits]
        assert pos_ids.count(pos.pos_id) == 1


class TestVPMClose:
    """close() archives position and returns PnL."""

    @pytest.mark.asyncio
    async def test_close_returns_correct_pnl(self) -> None:
        vpm = VirtualPositionManager()
        pos = _long(entry=40_000, qty=0.5, tp=None, sl=None)
        await vpm.open(pos)

        pnl = await vpm.close(pos.pos_id, Decimal("42000"), "take_profit")
        # (42_000 - 40_000) × 0.5 = 1_000
        assert pnl == Decimal("1000.0")

    @pytest.mark.asyncio
    async def test_close_removes_from_open_positions(self) -> None:
        vpm = VirtualPositionManager()
        pos = _long()
        await vpm.open(pos)
        await vpm.close(pos.pos_id, Decimal("44000"), "take_profit")

        remaining = await vpm.get_all()
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_close_archives_record(self) -> None:
        vpm = VirtualPositionManager()
        pos = _long()
        await vpm.open(pos)
        await vpm.close(pos.pos_id, Decimal("44000"), "take_profit")

        assert len(vpm.archive) == 1
        record = vpm.archive[0]
        assert record["pos_id"] == pos.pos_id
        assert record["reason"] == "take_profit"
        assert "exit_price" in record
        assert "pnl" in record

    @pytest.mark.asyncio
    async def test_close_unknown_pos_id_raises(self) -> None:
        vpm = VirtualPositionManager()
        with pytest.raises(KeyError):
            await vpm.close("does-not-exist", Decimal("40000"), "manual")


class TestVPMNetQty:
    """net_qty() sums correctly across multiple positions."""

    @pytest.mark.asyncio
    async def test_net_qty_single_position(self) -> None:
        vpm = VirtualPositionManager()
        pos = _long(qty=0.5)
        await vpm.open(pos)

        qty = await vpm.net_qty("BTC/USDT", "long")
        assert qty == Decimal("0.5")

    @pytest.mark.asyncio
    async def test_net_qty_sums_multiple(self) -> None:
        vpm = VirtualPositionManager()
        pos1 = _long(qty=0.3)
        pos2 = _long(qty=0.7)
        await vpm.open(pos1)
        await vpm.open(pos2)

        qty = await vpm.net_qty("BTC/USDT", "long")
        assert qty == Decimal("1.0")

    @pytest.mark.asyncio
    async def test_net_qty_excludes_wrong_side(self) -> None:
        vpm = VirtualPositionManager()
        pos_long = _long(qty=0.5)
        pos_short = _short(qty=0.3)
        await vpm.open(pos_long)
        await vpm.open(pos_short)

        long_qty = await vpm.net_qty("BTC/USDT", "long")
        short_qty = await vpm.net_qty("BTC/USDT", "short")
        assert long_qty == Decimal("0.5")
        assert short_qty == Decimal("0.3")

    @pytest.mark.asyncio
    async def test_net_qty_excludes_wrong_symbol(self) -> None:
        vpm = VirtualPositionManager()
        btc_pos = _long(symbol="BTC/USDT", qty=0.5)
        eth_pos = VirtualPositionManager.new_position(
            "dca", "ETH/USDT", "long", Decimal("2.0"), Decimal("2000")
        )
        await vpm.open(btc_pos)
        await vpm.open(eth_pos)

        btc_qty = await vpm.net_qty("BTC/USDT", "long")
        eth_qty = await vpm.net_qty("ETH/USDT", "long")
        assert btc_qty == Decimal("0.5")
        assert eth_qty == Decimal("2.0")

    @pytest.mark.asyncio
    async def test_net_qty_zero_when_no_positions(self) -> None:
        vpm = VirtualPositionManager()
        qty = await vpm.net_qty("BTC/USDT", "long")
        assert qty == Decimal("0")


class TestVPMGetByStrategy:
    """get_by_strategy() filters by strategy name."""

    @pytest.mark.asyncio
    async def test_get_by_strategy_returns_matching(self) -> None:
        vpm = VirtualPositionManager()
        grid_pos = _long(strategy="grid")
        dca_pos = _long(strategy="dca")
        smc_pos = _short(strategy="smc")
        await vpm.open(grid_pos)
        await vpm.open(dca_pos)
        await vpm.open(smc_pos)

        grid_positions = await vpm.get_by_strategy("grid")
        assert len(grid_positions) == 1
        assert grid_positions[0].pos_id == grid_pos.pos_id

    @pytest.mark.asyncio
    async def test_get_by_strategy_empty_when_none(self) -> None:
        vpm = VirtualPositionManager()
        pos = _long(strategy="grid")
        await vpm.open(pos)

        tf_positions = await vpm.get_by_strategy("tf")
        assert tf_positions == []


class TestVPMConcurrency:
    """asyncio.Lock() — concurrent ticks must not corrupt state."""

    @pytest.mark.asyncio
    async def test_no_race_condition_parallel_ticks(self) -> None:
        """
        100 concurrent check_exits() calls against 20 positions must all
        complete without raising and return a consistent list.
        """
        vpm = VirtualPositionManager()

        # Open 20 positions: 10 will have TP hit at 44_000, 10 won't
        for i in range(10):
            pos = VirtualPositionManager.new_position(
                "grid", "BTC/USDT", "long", Decimal("0.1"), Decimal("40000"),
                tp_price=Decimal("44000"), sl_price=Decimal("38000"),
            )
            await vpm.open(pos)

        for i in range(10):
            pos = VirtualPositionManager.new_position(
                "dca", "ETH/USDT", "long", Decimal("1.0"), Decimal("2000"),
                tp_price=Decimal("2500"), sl_price=Decimal("1900"),
            )
            await vpm.open(pos)

        # Fire 100 concurrent check_exits at TP price for BTC (only 10 BTC positions hit TP)
        results = await asyncio.gather(
            *[vpm.check_exits(Decimal("44000")) for _ in range(100)]
        )

        # Each call should return the same 10 BTC positions with take_profit
        for exits in results:
            btc_exits = [e for e in exits if e[0].symbol == "BTC/USDT"]
            assert len(btc_exits) == 10

    @pytest.mark.asyncio
    async def test_concurrent_open_and_close(self) -> None:
        """
        Simultaneously opening and closing positions must not raise.
        """
        vpm = VirtualPositionManager()

        positions = []
        for _ in range(50):
            pos = _long()
            positions.append(pos)
            await vpm.open(pos)

        async def _close(pos: VirtualPosition) -> None:
            try:
                await vpm.close(pos.pos_id, Decimal("41000"), "manual")
            except KeyError:
                pass  # already closed by another coroutine (expected)

        # Close all positions concurrently
        await asyncio.gather(*[_close(p) for p in positions])

        remaining = await vpm.get_all()
        assert remaining == []


# ===========================================================================
# ExchangeExecutor unit tests
# ===========================================================================


class TestExchangeExecutorOpenOrders:
    """open_long / open_short place correct side and order type."""

    def _make_executor(self) -> tuple[ExchangeExecutor, BacktestExecutionLayer]:
        layer = BacktestExecutionLayer()
        executor = ExchangeExecutor(layer)
        return executor, layer

    @pytest.mark.asyncio
    async def test_open_long_places_buy_order(self) -> None:
        executor, layer = self._make_executor()
        result = await executor.open_long("BTC/USDT", Decimal("0.01"))
        assert result["side"] == "buy"
        assert len(layer.orders) == 1

    @pytest.mark.asyncio
    async def test_open_long_with_limit_price(self) -> None:
        executor, layer = self._make_executor()
        result = await executor.open_long("BTC/USDT", Decimal("0.01"), price=Decimal("65000"))
        assert result["type"] == "limit"
        assert result["price"] == 65000.0

    @pytest.mark.asyncio
    async def test_open_long_without_price_is_market(self) -> None:
        executor, layer = self._make_executor()
        result = await executor.open_long("BTC/USDT", Decimal("0.01"))
        assert result["type"] == "market"
        assert result["price"] is None

    @pytest.mark.asyncio
    async def test_open_short_places_sell_order(self) -> None:
        executor, layer = self._make_executor()
        result = await executor.open_short("BTC/USDT", Decimal("0.01"))
        assert result["side"] == "sell"

    @pytest.mark.asyncio
    async def test_open_short_with_limit_price(self) -> None:
        executor, layer = self._make_executor()
        result = await executor.open_short("BTC/USDT", Decimal("0.01"), price=Decimal("65000"))
        assert result["type"] == "limit"


class TestExchangeExecutorCloseOrders:
    """close_long / close_short ALWAYS set reduceOnly=True."""

    def _make_executor(self) -> tuple[ExchangeExecutor, BacktestExecutionLayer]:
        layer = BacktestExecutionLayer()
        executor = ExchangeExecutor(layer)
        return executor, layer

    @pytest.mark.asyncio
    async def test_close_long_is_sell(self) -> None:
        executor, layer = self._make_executor()
        result = await executor.close_long("BTC/USDT", Decimal("0.01"))
        assert result["side"] == "sell"

    @pytest.mark.asyncio
    async def test_close_long_reduce_only_true(self) -> None:
        executor, layer = self._make_executor()
        await executor.close_long("BTC/USDT", Decimal("0.01"))
        order = layer.orders[-1]
        assert order["params"]["reduceOnly"] is True

    @pytest.mark.asyncio
    async def test_close_long_reduce_only_cannot_be_overridden(self) -> None:
        """Caller passing reduceOnly=False must NOT disable it."""
        executor, layer = self._make_executor()
        await executor.close_long(
            "BTC/USDT", Decimal("0.01"), params={"reduceOnly": False}
        )
        order = layer.orders[-1]
        assert order["params"]["reduceOnly"] is True

    @pytest.mark.asyncio
    async def test_close_long_with_limit_price(self) -> None:
        executor, layer = self._make_executor()
        await executor.close_long("BTC/USDT", Decimal("0.01"), price=Decimal("65000"))
        order = layer.orders[-1]
        assert order["type"] == "limit"
        assert order["price"] == 65000.0
        assert order["params"]["reduceOnly"] is True

    @pytest.mark.asyncio
    async def test_close_short_is_buy(self) -> None:
        executor, layer = self._make_executor()
        result = await executor.close_short("BTC/USDT", Decimal("0.01"))
        assert result["side"] == "buy"

    @pytest.mark.asyncio
    async def test_close_short_reduce_only_true(self) -> None:
        executor, layer = self._make_executor()
        await executor.close_short("BTC/USDT", Decimal("0.01"))
        order = layer.orders[-1]
        assert order["params"]["reduceOnly"] is True

    @pytest.mark.asyncio
    async def test_close_short_reduce_only_cannot_be_overridden(self) -> None:
        """Caller passing reduceOnly=False must NOT disable it."""
        executor, layer = self._make_executor()
        await executor.close_short(
            "BTC/USDT", Decimal("0.01"), params={"reduceOnly": False}
        )
        order = layer.orders[-1]
        assert order["params"]["reduceOnly"] is True

    @pytest.mark.asyncio
    async def test_close_short_with_limit_price(self) -> None:
        executor, layer = self._make_executor()
        await executor.close_short("BTC/USDT", Decimal("0.01"), price=Decimal("38000"))
        order = layer.orders[-1]
        assert order["type"] == "limit"
        assert order["params"]["reduceOnly"] is True


class TestExchangeExecutorWithMock:
    """ExchangeExecutor delegates correctly to a mocked ExecutionLayer."""

    def _make_mock_layer(self) -> MagicMock:
        layer = MagicMock()
        layer.create_order = AsyncMock(return_value={"id": "mock-1", "status": "open"})
        return layer

    @pytest.mark.asyncio
    async def test_open_long_delegates_to_layer(self) -> None:
        layer = self._make_mock_layer()
        executor = ExchangeExecutor(layer)
        await executor.open_long("BTC/USDT", Decimal("0.01"), price=Decimal("65000"))

        layer.create_order.assert_awaited_once_with(
            symbol="BTC/USDT",
            order_type="limit",
            side="buy",
            amount=0.01,
            price=65000.0,
            params=None,
        )

    @pytest.mark.asyncio
    async def test_close_long_always_passes_reduce_only(self) -> None:
        layer = self._make_mock_layer()
        executor = ExchangeExecutor(layer)
        await executor.close_long("BTC/USDT", Decimal("0.01"))

        _call_kwargs = layer.create_order.call_args.kwargs
        assert _call_kwargs["params"]["reduceOnly"] is True
        assert _call_kwargs["side"] == "sell"

    @pytest.mark.asyncio
    async def test_close_short_always_passes_reduce_only(self) -> None:
        layer = self._make_mock_layer()
        executor = ExchangeExecutor(layer)
        await executor.close_short("ETH/USDT", Decimal("1.0"))

        _call_kwargs = layer.create_order.call_args.kwargs
        assert _call_kwargs["params"]["reduceOnly"] is True
        assert _call_kwargs["side"] == "buy"


# ===========================================================================
# Integration test: reconcile net_qty
# ===========================================================================


class TestReconcileNetQty:
    """
    Integration test simulating the startup reconcile (§1.5).

    The bot compares exchange net position against VPM net_qty and detects
    divergence.  This test verifies the comparison logic is correct.
    """

    @pytest.mark.asyncio
    async def test_no_divergence_when_positions_match(self) -> None:
        vpm = VirtualPositionManager()

        # Register 3 long positions totalling 0.5 BTC
        for qty in [0.1, 0.2, 0.2]:
            pos = VirtualPositionManager.new_position(
                "grid", "BTC/USDT", "long", Decimal(str(qty)), Decimal("40000")
            )
            await vpm.open(pos)

        vpm_net = await vpm.net_qty("BTC/USDT", "long")
        exchange_net = Decimal("0.5")  # exchange reports same amount

        divergence_pct = abs(vpm_net - exchange_net) / exchange_net
        assert divergence_pct == Decimal("0"), f"Unexpected divergence: {divergence_pct}"

    @pytest.mark.asyncio
    async def test_small_divergence_below_5pct(self) -> None:
        """A divergence below 5 % should be detectable but not critical."""
        vpm = VirtualPositionManager()
        pos = VirtualPositionManager.new_position(
            "grid", "BTC/USDT", "long", Decimal("0.5"), Decimal("40000")
        )
        await vpm.open(pos)

        vpm_net = await vpm.net_qty("BTC/USDT", "long")
        exchange_net = Decimal("0.52")  # exchange has 4 % more

        divergence_pct = abs(vpm_net - exchange_net) / exchange_net
        assert divergence_pct < Decimal("0.05"), "Should be below 5 % warning threshold"

    @pytest.mark.asyncio
    async def test_critical_divergence_above_20pct(self) -> None:
        """A divergence ≥ 20 % should trigger emergency stop."""
        vpm = VirtualPositionManager()
        pos = VirtualPositionManager.new_position(
            "grid", "BTC/USDT", "long", Decimal("0.5"), Decimal("40000")
        )
        await vpm.open(pos)

        vpm_net = await vpm.net_qty("BTC/USDT", "long")
        exchange_net = Decimal("0.65")  # 30 % difference

        divergence_pct = abs(vpm_net - exchange_net) / exchange_net
        assert divergence_pct >= Decimal("0.20"), (
            f"Expected critical divergence ≥ 20 %, got {divergence_pct:.2%}"
        )

    @pytest.mark.asyncio
    async def test_net_qty_after_partial_close(self) -> None:
        """After closing some positions net_qty reflects the updated total."""
        vpm = VirtualPositionManager()
        pos1 = VirtualPositionManager.new_position(
            "grid", "BTC/USDT", "long", Decimal("0.3"), Decimal("40000")
        )
        pos2 = VirtualPositionManager.new_position(
            "grid", "BTC/USDT", "long", Decimal("0.2"), Decimal("40000")
        )
        await vpm.open(pos1)
        await vpm.open(pos2)

        # Close the first position
        await vpm.close(pos1.pos_id, Decimal("42000"), "take_profit")

        remaining_net = await vpm.net_qty("BTC/USDT", "long")
        assert remaining_net == Decimal("0.2")
