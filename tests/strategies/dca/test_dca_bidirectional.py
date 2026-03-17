"""Tests for bidirectional DCAEngine (Phase 3, issue #400).

Covers:
- SHORT stack adds levels on price RISE
- SHORT stack does NOT add levels on price FALL
- close_long_stack() does not touch SHORT levels
- close_short_stack() does not touch LONG levels
- get_combined_pnl() correctly sums both stacks
- DCALevel and CloseOrder dataclasses
"""

from decimal import Decimal

import pytest

from bot.core.dca_engine import CloseOrder, DCAEngine, DCALevel


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def engine() -> DCAEngine:
    """DCAEngine with a 5% step and 10% TP, max 3 steps."""
    return DCAEngine(
        symbol="BTC/USDT",
        trigger_percentage=Decimal("0.05"),
        amount_per_step=Decimal("0.1"),
        max_steps=3,
        take_profit_percentage=Decimal("0.10"),
    )


# =============================================================================
# DCALevel and CloseOrder dataclasses
# =============================================================================


class TestDCALevelDataclass:
    def test_dca_level_fields(self):
        lvl = DCALevel(price=Decimal("40000"), amount=Decimal("0.1"))
        assert lvl.price == Decimal("40000")
        assert lvl.amount == Decimal("0.1")


class TestCloseOrderDataclass:
    def test_close_order_long(self):
        order = CloseOrder(price=Decimal("40000"), amount=Decimal("0.1"), side="sell")
        assert order.side == "sell"

    def test_close_order_short(self):
        order = CloseOrder(price=Decimal("42000"), amount=Decimal("0.1"), side="buy")
        assert order.side == "buy"


# =============================================================================
# SHORT stack: trigger logic
# =============================================================================


class TestShortStackTrigger:
    def test_first_short_entry_always_triggers(self, engine: DCAEngine):
        """No SHORT levels yet → trigger is True regardless of price."""
        assert engine.check_short_dca_trigger(Decimal("40000")) is True

    def test_short_adds_level_on_price_rise(self, engine: DCAEngine):
        """SHORT stack adds levels when price RISES by ≥ trigger_percentage."""
        # First SHORT entry at 40000
        engine.execute_short_dca_step(Decimal("40000"))
        assert len(engine._short_levels) == 1

        # Price rises 5% → should trigger second SHORT step
        assert engine.check_short_dca_trigger(Decimal("42000")) is True
        engine.execute_short_dca_step(Decimal("42000"))
        assert len(engine._short_levels) == 2

    def test_short_does_not_trigger_on_price_fall(self, engine: DCAEngine):
        """SHORT stack must NOT add a level when price falls."""
        engine.execute_short_dca_step(Decimal("40000"))
        # Price falls 5% → should NOT trigger
        assert engine.check_short_dca_trigger(Decimal("38000")) is False

    def test_short_does_not_trigger_below_threshold(self, engine: DCAEngine):
        """SHORT stack does not trigger when price rise is less than trigger_percentage."""
        engine.execute_short_dca_step(Decimal("40000"))
        # Only 2% rise, below 5% threshold
        assert engine.check_short_dca_trigger(Decimal("40800")) is False

    def test_short_max_steps_respected(self, engine: DCAEngine):
        """SHORT stack respects max_steps."""
        prices = [Decimal("40000"), Decimal("42000"), Decimal("44100")]
        for p in prices:
            engine.execute_short_dca_step(p)
        assert len(engine._short_levels) == 3

        # 4th step should not trigger
        assert engine.check_short_dca_trigger(Decimal("46305")) is False

    def test_execute_short_returns_false_when_not_triggered(self, engine: DCAEngine):
        """execute_short_dca_step returns False when trigger is not met."""
        engine.execute_short_dca_step(Decimal("40000"))
        result = engine.execute_short_dca_step(Decimal("38000"))  # price fell
        assert result is False
        assert len(engine._short_levels) == 1


# =============================================================================
# SHORT take-profit
# =============================================================================


class TestShortTakeProfit:
    def test_short_tp_triggered_when_price_falls_to_target(self, engine: DCAEngine):
        """SHORT TP triggers when price ≤ avg_entry × (1 - tp_pct)."""
        engine.execute_short_dca_step(Decimal("40000"))
        # TP = 40000 * (1 - 0.10) = 36000
        assert engine.check_short_take_profit(Decimal("36000")) is True

    def test_short_tp_not_triggered_above_target(self, engine: DCAEngine):
        """SHORT TP does not trigger above target price."""
        engine.execute_short_dca_step(Decimal("40000"))
        # Price still at 38000, TP is 36000
        assert engine.check_short_take_profit(Decimal("38000")) is False

    def test_short_tp_no_position(self, engine: DCAEngine):
        """SHORT TP returns False when no SHORT levels exist."""
        assert engine.check_short_take_profit(Decimal("35000")) is False


# =============================================================================
# close_long_stack: does NOT touch SHORT levels
# =============================================================================


class TestCloseLongStack:
    def test_close_long_clears_long_stack_only(self, engine: DCAEngine):
        """close_long_stack removes LONG levels but keeps SHORT levels intact."""
        # Build a LONG stack
        engine.execute_dca_step(Decimal("40000"))
        engine.execute_dca_step(Decimal("38000"))

        # Build a SHORT stack
        engine.execute_short_dca_step(Decimal("40000"))
        engine.execute_short_dca_step(Decimal("42000"))

        orders = engine.close_long_stack()

        assert len(orders) == 2
        assert all(o.side == "sell" for o in orders)
        # SHORT stack must be untouched
        assert len(engine._short_levels) == 2

    def test_close_long_returns_sell_orders(self, engine: DCAEngine):
        """close_long_stack returns sell CloseOrder objects."""
        engine.execute_dca_step(Decimal("40000"))
        orders = engine.close_long_stack()
        assert len(orders) == 1
        assert orders[0].side == "sell"
        assert orders[0].amount == Decimal("0.1")

    def test_close_long_empty_stack(self, engine: DCAEngine):
        """close_long_stack on empty stack returns empty list."""
        orders = engine.close_long_stack()
        assert orders == []


# =============================================================================
# close_short_stack: does NOT touch LONG levels
# =============================================================================


class TestCloseShortStack:
    def test_close_short_clears_short_stack_only(self, engine: DCAEngine):
        """close_short_stack removes SHORT levels but keeps LONG levels intact."""
        # Build a LONG stack
        engine.execute_dca_step(Decimal("40000"))
        engine.execute_dca_step(Decimal("38000"))

        # Build a SHORT stack
        engine.execute_short_dca_step(Decimal("40000"))
        engine.execute_short_dca_step(Decimal("42000"))

        orders = engine.close_short_stack()

        assert len(orders) == 2
        assert all(o.side == "buy" for o in orders)
        # LONG stack must be untouched
        assert len(engine._long_levels) == 2

    def test_close_short_returns_buy_orders(self, engine: DCAEngine):
        """close_short_stack returns buy (cover) CloseOrder objects."""
        engine.execute_short_dca_step(Decimal("40000"))
        orders = engine.close_short_stack()
        assert len(orders) == 1
        assert orders[0].side == "buy"
        assert orders[0].amount == Decimal("0.1")

    def test_close_short_empty_stack(self, engine: DCAEngine):
        """close_short_stack on empty stack returns empty list."""
        orders = engine.close_short_stack()
        assert orders == []


# =============================================================================
# get_combined_pnl: LONG PnL + SHORT PnL
# =============================================================================


class TestCombinedPnl:
    def test_combined_pnl_both_stacks(self, engine: DCAEngine):
        """get_combined_pnl correctly sums LONG + SHORT PnL."""
        # LONG: bought 0.1 BTC at 40000 → PnL at 42000: (42000-40000)*0.1 = +200
        engine.execute_dca_step(Decimal("40000"))

        # SHORT: sold 0.1 BTC at 42000 → PnL at 42000: (42000-42000)*0.1 = 0
        engine.execute_short_dca_step(Decimal("42000"))

        combined = engine.get_combined_pnl(Decimal("42000"))
        # LONG PnL = (42000-40000)*0.1 = 200
        # SHORT PnL = (42000-42000)*0.1 = 0
        assert combined == Decimal("200")

    def test_combined_pnl_long_only(self, engine: DCAEngine):
        """get_combined_pnl with only LONG stack active."""
        engine.execute_dca_step(Decimal("40000"))
        # price drops to 38000: LONG loses (38000-40000)*0.1 = -200
        combined = engine.get_combined_pnl(Decimal("38000"))
        assert combined == Decimal("-200")

    def test_combined_pnl_short_only(self, engine: DCAEngine):
        """get_combined_pnl with only SHORT stack active."""
        engine.execute_short_dca_step(Decimal("42000"))
        # price drops to 40000: SHORT gains (42000-40000)*0.1 = +200
        combined = engine.get_combined_pnl(Decimal("40000"))
        assert combined == Decimal("200")

    def test_combined_pnl_no_positions(self, engine: DCAEngine):
        """get_combined_pnl returns 0 when no positions."""
        assert engine.get_combined_pnl(Decimal("40000")) == Decimal("0")

    def test_combined_pnl_hedge_offsets(self, engine: DCAEngine):
        """Hedge scenario: LONG loss is partially offset by SHORT gain."""
        # LONG at 40000, SHORT at 40000 (both 0.1 BTC)
        engine.execute_dca_step(Decimal("40000"))
        engine.execute_short_dca_step(Decimal("40000"))

        # Price drops to 38000:
        # LONG PnL = (38000-40000)*0.1 = -200
        # SHORT PnL = (40000-38000)*0.1 = +200
        combined = engine.get_combined_pnl(Decimal("38000"))
        assert combined == Decimal("0")

    def test_combined_pnl_multiple_levels(self, engine: DCAEngine):
        """get_combined_pnl with multiple levels in each stack."""
        # LONG: bought at 40000 and 38000 (0.1 each)
        engine.execute_dca_step(Decimal("40000"))
        engine.execute_dca_step(Decimal("38000"))

        # SHORT: sold at 40000 and 42000 (0.1 each)
        engine.execute_short_dca_step(Decimal("40000"))
        engine.execute_short_dca_step(Decimal("42000"))

        current = Decimal("41000")
        # LONG PnL = (41000-40000)*0.1 + (41000-38000)*0.1 = 100 + 300 = 400
        # SHORT PnL = (40000-41000)*0.1 + (42000-41000)*0.1 = -100 + 100 = 0
        combined = engine.get_combined_pnl(current)
        assert combined == Decimal("400")


# =============================================================================
# reset clears both stacks
# =============================================================================


class TestReset:
    def test_reset_clears_short_stack(self, engine: DCAEngine):
        engine.execute_short_dca_step(Decimal("40000"))
        engine.execute_short_dca_step(Decimal("42000"))
        engine.reset()
        assert engine._short_levels == []
        assert engine._last_short_price is None

    def test_reset_clears_long_stack(self, engine: DCAEngine):
        engine.execute_dca_step(Decimal("40000"))
        engine.reset()
        assert engine._long_levels == []
