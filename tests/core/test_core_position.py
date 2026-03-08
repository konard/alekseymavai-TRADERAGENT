"""
Unit and integration tests for CorePosition-based Grid/DCA coordination.

Issue #359: Координация DCA и Grid через единую базовую позицию (CorePosition)

Coverage:
- CorePosition state machine (update_from_fill, breakeven, safety flag)
- DCAEngine → CorePosition mirroring
- GridEngine.apply_position_constraints() — 5+ distinct scenarios
- HybridCoordinator lifecycle (create / attach / release)
- Integration: DCA fills → Grid constraints → no conflicting orders
- Backward compatibility: None CorePosition is a no-op for Grid
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bot.core.dca_engine import DCAEngine
from bot.core.grid_engine import GridEngine, GridOrder
from bot.core.trading_core.core_position import CorePosition
from bot.core.trading_core.hybrid_coordinator import HybridCoordinator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_core_position(
    symbol: str = "BTC/USDT",
    side: str = "long",
    avg: float = 40_000.0,
    amount: float = 0.5,
    steps: int = 1,
    safety_active: bool = False,
) -> CorePosition:
    """Build a CorePosition already populated with one fill."""
    pos = CorePosition(symbol=symbol, side=side)
    pos.avg_entry_price = Decimal(str(avg))
    pos.total_amount = Decimal(str(amount))
    pos.dca_steps_taken = steps
    pos.safety_order_active = safety_active
    return pos


def _make_grid(
    lower: float = 38_000.0,
    upper: float = 48_000.0,
    levels: int = 6,
    amount_per_grid: float = 100.0,
    profit_per_grid: float = 0.01,
) -> GridEngine:
    return GridEngine(
        symbol="BTC/USDT",
        upper_price=Decimal(str(upper)),
        lower_price=Decimal(str(lower)),
        grid_levels=levels,
        amount_per_grid=Decimal(str(amount_per_grid)),
        profit_per_grid=Decimal(str(profit_per_grid)),
    )


def _make_dca_engine() -> DCAEngine:
    return DCAEngine(
        symbol="BTC/USDT",
        trigger_percentage=Decimal("0.05"),
        amount_per_step=Decimal("0.1"),
        max_steps=5,
        take_profit_percentage=Decimal("0.10"),
    )


def _register_orders(grid: GridEngine, orders: list[GridOrder]) -> None:
    """Register synthetic order IDs for a list of GridOrder objects."""
    for i, order in enumerate(orders):
        grid.register_order(order, f"order-{i:03d}")


# ===========================================================================
# Tests: CorePosition state
# ===========================================================================


class TestCorePositionState:
    """CorePosition tracks avg_entry, total_amount, breakeven correctly."""

    def test_initial_empty(self):
        pos = CorePosition(symbol="BTC/USDT", side="long")
        assert pos.is_empty()
        assert pos.total_amount == Decimal("0")
        assert pos.dca_steps_taken == 0

    def test_first_fill_sets_avg_entry(self):
        pos = CorePosition(symbol="BTC/USDT", side="long")
        pos.update_from_fill(Decimal("40000"), Decimal("0.5"))
        assert pos.avg_entry_price == Decimal("40000")
        assert pos.total_amount == Decimal("0.5")
        assert pos.dca_steps_taken == 1
        assert not pos.is_empty()

    def test_second_fill_averages_correctly(self):
        pos = CorePosition(symbol="BTC/USDT", side="long")
        pos.update_from_fill(Decimal("40000"), Decimal("1"))  # cost 40_000
        pos.update_from_fill(Decimal("38000"), Decimal("1"))  # cost 38_000
        # avg = (40_000 + 38_000) / 2 = 39_000
        assert pos.avg_entry_price == Decimal("39000")
        assert pos.total_amount == Decimal("2")
        assert pos.dca_steps_taken == 2

    def test_breakeven_equals_avg_entry_for_long(self):
        pos = CorePosition(symbol="ETH/USDT", side="long")
        pos.update_from_fill(Decimal("2500"), Decimal("2"))
        assert pos.get_breakeven() == pos.avg_entry_price

    def test_safety_flag_set_on_fill_and_cleared(self):
        pos = CorePosition(symbol="BTC/USDT", side="long")
        pos.update_from_fill(Decimal("40000"), Decimal("0.5"))
        assert pos.safety_order_active is True
        pos.clear_safety_order_flag()
        assert pos.safety_order_active is False

    def test_fill_with_zero_price_raises(self):
        pos = CorePosition(symbol="BTC/USDT", side="long")
        with pytest.raises(ValueError):
            pos.update_from_fill(Decimal("0"), Decimal("1"))

    def test_fill_with_zero_amount_raises(self):
        pos = CorePosition(symbol="BTC/USDT", side="long")
        with pytest.raises(ValueError):
            pos.update_from_fill(Decimal("40000"), Decimal("0"))

    def test_repr_contains_symbol(self):
        pos = CorePosition(symbol="SOL/USDT", side="long")
        assert "SOL/USDT" in repr(pos)


# ===========================================================================
# Tests: DCAEngine → CorePosition mirroring
# ===========================================================================


class TestDCAEngineCorePositionMirroring:
    """DCA fills update the attached CorePosition."""

    def test_attach_and_detach(self):
        dca = _make_dca_engine()
        pos = CorePosition(symbol="BTC/USDT", side="long")
        dca.attach_core_position(pos)
        assert dca.core_position is pos
        dca.detach_core_position()
        assert dca.core_position is None

    def test_dca_step_updates_core_position(self):
        dca = _make_dca_engine()
        pos = CorePosition(symbol="BTC/USDT", side="long")
        dca.attach_core_position(pos)

        dca.execute_dca_step(Decimal("40000"))

        assert pos.total_amount == Decimal("0.1")
        assert pos.avg_entry_price == Decimal("40000")
        assert pos.dca_steps_taken == 1
        assert pos.safety_order_active is True

    def test_multiple_dca_steps_accumulate(self):
        dca = _make_dca_engine()
        pos = CorePosition(symbol="BTC/USDT", side="long")
        dca.attach_core_position(pos)

        dca.execute_dca_step(Decimal("40000"))
        dca.execute_dca_step(Decimal("38000"))

        assert pos.dca_steps_taken == 2
        # avg = (40_000 * 0.1 + 38_000 * 0.1) / 0.2 = 39_000
        assert pos.avg_entry_price == Decimal("39000")
        assert pos.total_amount == Decimal("0.2")

    def test_no_core_position_no_error(self):
        """DCAEngine without CorePosition must work as before."""
        dca = _make_dca_engine()
        assert dca.core_position is None
        result = dca.execute_dca_step(Decimal("40000"))
        assert result is True  # step executed normally


# ===========================================================================
# Tests: GridEngine.apply_position_constraints()
# ===========================================================================


class TestGridApplyPositionConstraints:
    """Grid adapts orders based on CorePosition — 5+ distinct scenarios."""

    # -----------------------------------------------------------------------
    # Scenario 1: No CorePosition → no-op
    # -----------------------------------------------------------------------

    def test_no_core_position_is_noop(self):
        """Backward compatibility: None CorePosition must leave grid untouched."""
        grid = _make_grid()
        orders = grid.initialize_grid(Decimal("43000"))
        _register_orders(grid, orders)

        initial_count = len(grid.active_orders)
        report = grid.apply_position_constraints(None)

        assert len(grid.active_orders) == initial_count
        assert report == {
            "sell_cancelled_accumulation_zone": [],
            "sell_cancelled_cap": [],
            "buy_cancelled_safety_conflict": [],
        }

    # -----------------------------------------------------------------------
    # Scenario 2: Empty CorePosition → no-op
    # -----------------------------------------------------------------------

    def test_empty_core_position_is_noop(self):
        grid = _make_grid()
        orders = grid.initialize_grid(Decimal("43000"))
        _register_orders(grid, orders)
        initial_count = len(grid.active_orders)

        pos = CorePosition(symbol="BTC/USDT", side="long")  # no fills
        report = grid.apply_position_constraints(pos)

        assert len(grid.active_orders) == initial_count
        assert all(len(v) == 0 for v in report.values())

    # -----------------------------------------------------------------------
    # Scenario 3: SELL orders in DCA accumulation zone are cancelled
    # -----------------------------------------------------------------------

    def test_sell_in_accumulation_zone_is_cancelled(self):
        """
        DCA avg_entry = 40_000.  Grid should not place SELL orders between
        (avg - 2×profit_per_grid×avg) and avg (the accumulation ceiling).
        """
        grid = _make_grid(lower=38_000, upper=44_000, levels=7, profit_per_grid=0.01)
        orders = grid.initialize_grid(Decimal("43000"))
        _register_orders(grid, orders)

        # Place a synthetic SELL order right at 39_600 (below avg 40_000)
        conflicting_sell = GridOrder(
            level=99, price=Decimal("39600"), amount=Decimal("0.003"), side="sell"
        )
        grid.register_order(conflicting_sell, "conflict-sell-001")

        pos = _make_core_position(avg=40_000, amount=0.5)
        report = grid.apply_position_constraints(pos, dca_protection_levels=2)

        assert "conflict-sell-001" in report["sell_cancelled_accumulation_zone"]

    # -----------------------------------------------------------------------
    # Scenario 4: SELL cap limits total SELL volume
    # -----------------------------------------------------------------------

    def test_sell_cap_limits_total_sell_volume(self):
        """
        CorePosition.total_amount = 0.5 BTC.
        max_sell_ratio = 0.5 → Grid may only SELL up to 0.25 BTC total.
        Extra SELL orders must be cancelled.
        """
        grid = _make_grid(lower=38_000, upper=44_000, levels=4, amount_per_grid=10_000)
        # Don't use initialize_grid here — manually add SELLs above avg
        pos = _make_core_position(avg=40_000, amount=0.5, safety_active=False)

        # Add 3 SELL orders above avg, each 0.1 BTC → cumulative 0.3 BTC > 0.25 cap
        sell_prices = [Decimal("41000"), Decimal("42000"), Decimal("43000")]
        for i, price in enumerate(sell_prices):
            order = GridOrder(level=i, price=price, amount=Decimal("0.1"), side="sell")
            grid.register_order(order, f"sell-{i:03d}")

        report = grid.apply_position_constraints(pos, max_sell_ratio=Decimal("0.5"))

        # After 0.1 + 0.1 = 0.2 ≤ 0.25 OK; 0.2 + 0.1 = 0.3 > 0.25 → 3rd cancelled
        assert len(report["sell_cancelled_cap"]) == 1

    # -----------------------------------------------------------------------
    # Scenario 5: Safety-order flag removes conflicting BUYs
    # -----------------------------------------------------------------------

    def test_safety_order_removes_conflicting_buy_orders(self):
        """
        When safety_order_active=True, BUY orders within ±1 profit_per_grid
        band around avg_entry_price are cancelled, then the flag is cleared.
        """
        grid = _make_grid(lower=38_000, upper=44_000, levels=4, profit_per_grid=0.01)
        pos = _make_core_position(avg=40_000, amount=0.5, safety_active=True)

        # BUY order right at avg (within the conflict band)
        close_buy = GridOrder(
            level=10, price=Decimal("40000"), amount=Decimal("0.002"), side="buy"
        )
        grid.register_order(close_buy, "close-buy-001")

        # BUY order far from avg (should survive)
        far_buy = GridOrder(
            level=11, price=Decimal("38000"), amount=Decimal("0.002"), side="buy"
        )
        grid.register_order(far_buy, "far-buy-001")

        report = grid.apply_position_constraints(pos)

        assert "close-buy-001" in report["buy_cancelled_safety_conflict"]
        assert "far-buy-001" not in report["buy_cancelled_safety_conflict"]
        # Flag must be cleared after Grid reacted
        assert pos.safety_order_active is False

    # -----------------------------------------------------------------------
    # Scenario 6: SELL orders ABOVE avg_entry_price are NOT cancelled
    # -----------------------------------------------------------------------

    def test_sell_above_avg_not_cancelled_by_accumulation_rule(self):
        """
        Accumulation-zone rule only cancels SELLs between
        (avg - band) and avg.  SELLs strictly above avg must survive.
        """
        grid = _make_grid(lower=38_000, upper=46_000, levels=5)
        pos = _make_core_position(avg=40_000, amount=0.5, safety_active=False)

        above_sell = GridOrder(
            level=5, price=Decimal("41000"), amount=Decimal("0.002"), side="sell"
        )
        grid.register_order(above_sell, "above-sell-001")

        report = grid.apply_position_constraints(pos)

        assert "above-sell-001" not in report["sell_cancelled_accumulation_zone"]
        assert "above-sell-001" in grid.active_orders

    # -----------------------------------------------------------------------
    # Scenario 7: No safety-order conflict when flag is False
    # -----------------------------------------------------------------------

    def test_no_buy_cancellation_when_safety_flag_false(self):
        """If safety_order_active=False, BUY orders near avg must survive."""
        grid = _make_grid(lower=38_000, upper=44_000, levels=4)
        pos = _make_core_position(avg=40_000, amount=0.5, safety_active=False)

        near_buy = GridOrder(
            level=10, price=Decimal("40000"), amount=Decimal("0.002"), side="buy"
        )
        grid.register_order(near_buy, "near-buy-001")

        report = grid.apply_position_constraints(pos)

        assert report["buy_cancelled_safety_conflict"] == []
        assert "near-buy-001" in grid.active_orders


# ===========================================================================
# Tests: HybridCoordinator lifecycle
# ===========================================================================


class TestHybridCoordinatorLifecycle:
    """HybridCoordinator creates / releases CorePosition and wires engines."""

    def test_initial_state_has_no_core_position(self):
        coord = HybridCoordinator()
        assert coord.core_position is None

    def test_create_core_position(self):
        coord = HybridCoordinator()
        pos = coord.create_core_position("BTC/USDT", side="long")
        assert coord.core_position is pos
        assert pos.symbol == "BTC/USDT"
        assert pos.side == "long"

    def test_release_core_position_clears_reference(self):
        coord = HybridCoordinator()
        coord.create_core_position("BTC/USDT")
        coord.release_core_position()
        assert coord.core_position is None

    def test_attach_engines_wires_dca_to_core_position(self):
        coord = HybridCoordinator()
        dca = _make_dca_engine()
        coord.create_core_position("BTC/USDT")
        coord.attach_engines(dca_engine=dca)

        # DCA engine must now be attached to the same CorePosition
        assert dca.core_position is coord.core_position

    def test_release_detaches_dca_engine(self):
        coord = HybridCoordinator()
        dca = _make_dca_engine()
        coord.create_core_position("BTC/USDT")
        coord.attach_engines(dca_engine=dca)
        coord.release_core_position()
        assert dca.core_position is None

    def test_apply_grid_constraints_without_core_position_returns_empty(self):
        """When no core position exists and no grid is wired, returns {}."""
        coord = HybridCoordinator()
        # No grid engine wired, no grid_engine argument → pure empty dict
        report = coord.apply_grid_constraints(grid_engine=None)
        assert report == {}

    def test_apply_grid_constraints_delegates_to_grid_engine(self):
        coord = HybridCoordinator()
        grid = _make_grid()
        pos = coord.create_core_position("BTC/USDT")

        # Populate position
        pos.update_from_fill(Decimal("40000"), Decimal("0.5"))

        # Add a SELL order in the accumulation zone
        conflict = GridOrder(
            level=99, price=Decimal("39600"), amount=Decimal("0.003"), side="sell"
        )
        grid.register_order(conflict, "test-sell-001")

        report = coord.apply_grid_constraints(grid)
        assert "test-sell-001" in report["sell_cancelled_accumulation_zone"]

    def test_evaluate_backward_compat_grid_only(self):
        """evaluate() must still work without any CorePosition."""
        coord = HybridCoordinator(adx_dca_threshold=25.0)
        decision = coord.evaluate(adx=15.0)
        assert decision.run_grid is True
        assert decision.run_dca is False

    def test_evaluate_backward_compat_dca_active(self):
        coord = HybridCoordinator(adx_dca_threshold=25.0)
        decision = coord.evaluate(adx=30.0)
        assert decision.run_dca is True
        assert decision.run_grid is False


# ===========================================================================
# Integration test: DCA fills → Grid constraints — no conflicting orders
# ===========================================================================


class TestHybridIntegration:
    """
    End-to-end scenario: DCA accumulates a position while Grid adapts.

    Verifies the full pipeline:
    1. HybridCoordinator creates CorePosition.
    2. DCA fills update CorePosition.
    3. Grid constraints applied — no SELL orders in accumulation zone.
    4. No conflicting BUY orders during active safety order.
    5. After constraint application, no order conflicts remain.
    """

    def test_dca_accumulates_grid_adapts_no_conflicts(self):
        """
        Full integration scenario:
        - DCA buys at 40_000 (base order) then at 38_000 (safety order).
        - Grid is initialised with current price at 43_000 (so it has SELL
          orders above and BUY orders below).
        - After each DCA fill, apply_grid_constraints() is called.
        - At the end, no active SELL order must be in the DCA accumulation
          zone (below avg_entry_price).
        """
        coord = HybridCoordinator()
        dca = _make_dca_engine()
        grid = _make_grid(lower=36_000, upper=46_000, levels=6, profit_per_grid=0.01)

        # --- Enter Hybrid mode ---
        pos = coord.create_core_position("BTC/USDT", side="long")
        coord.attach_engines(dca_engine=dca, grid_engine=grid)

        # Initialise grid at current price 43_000
        orders = grid.initialize_grid(Decimal("43000"))
        _register_orders(grid, orders)

        # --- DCA base order at 40_000 ---
        dca.execute_dca_step(Decimal("40000"))
        coord.apply_grid_constraints(grid)

        # Verify: no active SELL order is in the zone [avg-band, avg]
        avg_after_step1 = pos.avg_entry_price  # 40_000
        for order in grid.active_orders.values():
            if order.side == "sell":
                assert order.price > avg_after_step1, (
                    f"SELL order at {order.price} is in/below DCA accumulation "
                    f"zone (avg={avg_after_step1})"
                )

        # Safety flag must be cleared
        assert pos.safety_order_active is False

        # --- Simulate price drop and DCA safety order at 38_000 ---
        # Force trigger: set last_buy_price so 38_000 triggers (5% drop)
        dca.last_buy_price = Decimal("40000")
        dca.execute_dca_step(Decimal("38000"))
        coord.apply_grid_constraints(grid)

        avg_after_step2 = pos.avg_entry_price  # ~39_000
        for order in grid.active_orders.values():
            if order.side == "sell":
                assert order.price > avg_after_step2, (
                    f"SELL order at {order.price} conflicts with DCA accumulation "
                    f"zone (avg={avg_after_step2})"
                )

        # --- Leave Hybrid mode ---
        coord.release_core_position()
        assert dca.core_position is None
        assert coord.core_position is None

    def test_grid_sell_volume_capped_relative_to_dca_position(self):
        """
        After DCA accumulates 0.5 BTC, Grid SELL volume ≤ 0.25 BTC (50 % cap).
        """
        coord = HybridCoordinator()
        dca = _make_dca_engine()
        grid = _make_grid(lower=36_000, upper=46_000, levels=4, profit_per_grid=0.01)

        pos = coord.create_core_position("BTC/USDT", side="long")
        coord.attach_engines(dca_engine=dca, grid_engine=grid)

        # Manually set DCA position to 0.5 BTC at 40_000 (3 steps)
        dca.execute_dca_step(Decimal("40000"))
        dca.execute_dca_step(Decimal("38000"))
        dca.last_buy_price = Decimal("38000")
        dca.execute_dca_step(Decimal("36100"))

        # Add extra SELL orders above avg to push total over the 50 % cap
        for i, price in enumerate([Decimal("41000"), Decimal("42000"), Decimal("43000")]):
            order = GridOrder(level=20 + i, price=price, amount=Decimal("0.15"), side="sell")
            grid.register_order(order, f"extra-sell-{i:03d}")

        coord.apply_grid_constraints(grid, max_sell_ratio=Decimal("0.5"))

        total_sell = sum(
            o.amount for o in grid.active_orders.values() if o.side == "sell"
        )
        assert total_sell <= pos.total_amount * Decimal("0.5"), (
            f"Total SELL {total_sell} exceeds 50 % of CorePosition {pos.total_amount}"
        )
