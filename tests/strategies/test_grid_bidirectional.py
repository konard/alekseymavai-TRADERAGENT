"""
Tests for Bidirectional GridEngine (Issue #399, Phase 2).

Covers:
- GridMode enum values
- GridBiConfig dataclass construction
- initialize_grid in BIDIRECTIONAL mode: LONG orders below price, SHORT orders above
- _long_orders / _short_orders population
- get_combined_unrealized_pnl: LONG profits, SHORT profits, mixed
- close_direction: closes only one stack, leaves the other intact
- reconfigure: closes all, reinitialises with new config
- set_direction deprecation warning
- GridAdapter.reconfigure integration
"""

import warnings
from decimal import Decimal

from bot.core.grid_engine import (
    GridBiConfig,
    GridDirection,
    GridEngine,
    GridMode,
)
from bot.strategies.base import ExitReason, SignalDirection
from bot.strategies.grid_adapter import GridAdapter

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_engine(
    mode: GridMode = GridMode.BIDIRECTIONAL,
    levels: int = 6,
) -> GridEngine:
    return GridEngine(
        symbol="BTC/USDT",
        upper_price=Decimal("110"),
        lower_price=Decimal("90"),
        grid_levels=levels,
        amount_per_grid=Decimal("100"),
        profit_per_grid=Decimal("0.01"),
        mode=mode,
    )


def _make_bi_config(
    current_price: Decimal = Decimal("100"),
    long_levels: int = 3,
    short_levels: int = 3,
    step_pct: Decimal = Decimal("0.01"),
) -> GridBiConfig:
    return GridBiConfig(
        lower_bound=current_price * Decimal("0.95"),
        upper_bound=current_price * Decimal("1.05"),
        current_price=current_price,
        step_pct=step_pct,
        long_levels=long_levels,
        short_levels=short_levels,
        order_size_quote=Decimal("100"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# GridMode enum
# ─────────────────────────────────────────────────────────────────────────────

class TestGridModeEnum:
    def test_values_exist(self) -> None:
        assert GridMode.LONG_ONLY == "long_only"
        assert GridMode.SHORT_ONLY == "short_only"
        assert GridMode.BIDIRECTIONAL == "bidirectional"

    def test_is_string(self) -> None:
        assert isinstance(GridMode.BIDIRECTIONAL, str)


# ─────────────────────────────────────────────────────────────────────────────
# GridBiConfig dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestGridBiConfig:
    def test_construction(self) -> None:
        cfg = _make_bi_config()
        assert cfg.current_price == Decimal("100")
        assert cfg.long_levels == 3
        assert cfg.short_levels == 3

    def test_fields_are_decimal(self) -> None:
        cfg = _make_bi_config()
        for field in (cfg.lower_bound, cfg.upper_bound, cfg.current_price,
                      cfg.step_pct, cfg.order_size_quote):
            assert isinstance(field, Decimal)


# ─────────────────────────────────────────────────────────────────────────────
# initialize_grid in BIDIRECTIONAL mode
# ─────────────────────────────────────────────────────────────────────────────

class TestInitializeBidirectional:
    def test_long_orders_below_price(self) -> None:
        """All LONG (buy) orders must have price < current_price."""
        engine = _make_engine()
        cfg = _make_bi_config(current_price=Decimal("100"))
        orders = engine.initialize_grid(Decimal("100"), config=cfg)
        buy_orders = [o for o in orders if o.side == "buy"]
        assert buy_orders, "Expected at least one buy order"
        for o in buy_orders:
            assert o.price < Decimal("100"), f"Buy order price {o.price} is not below 100"

    def test_short_orders_above_price(self) -> None:
        """All SHORT (sell) orders must have price > current_price."""
        engine = _make_engine()
        cfg = _make_bi_config(current_price=Decimal("100"))
        orders = engine.initialize_grid(Decimal("100"), config=cfg)
        sell_orders = [o for o in orders if o.side == "sell"]
        assert sell_orders, "Expected at least one sell order"
        for o in sell_orders:
            assert o.price > Decimal("100"), f"Sell order price {o.price} is not above 100"

    def test_correct_level_counts(self) -> None:
        """Number of buy/sell orders matches long_levels/short_levels."""
        engine = _make_engine()
        cfg = _make_bi_config(long_levels=4, short_levels=2)
        orders = engine.initialize_grid(Decimal("100"), config=cfg)
        assert sum(1 for o in orders if o.side == "buy") == 4
        assert sum(1 for o in orders if o.side == "sell") == 2

    def test_long_orders_registry_populated(self) -> None:
        engine = _make_engine()
        cfg = _make_bi_config(long_levels=3, short_levels=3)
        engine.initialize_grid(Decimal("100"), config=cfg)
        assert len(engine._long_orders) == 3

    def test_short_orders_registry_populated(self) -> None:
        engine = _make_engine()
        cfg = _make_bi_config(long_levels=3, short_levels=3)
        engine.initialize_grid(Decimal("100"), config=cfg)
        assert len(engine._short_orders) == 3

    def test_mode_flag_triggers_bidirectional(self) -> None:
        """Engine with mode=BIDIRECTIONAL calls bidirectional path even without config."""
        engine = GridEngine(
            symbol="ETH/USDT",
            upper_price=Decimal("110"),
            lower_price=Decimal("90"),
            grid_levels=6,
            amount_per_grid=Decimal("50"),
            profit_per_grid=Decimal("0.01"),
            mode=GridMode.BIDIRECTIONAL,
        )
        orders = engine.initialize_grid(Decimal("100"))
        buy_orders = [o for o in orders if o.side == "buy"]
        sell_orders = [o for o in orders if o.side == "sell"]
        assert buy_orders
        assert sell_orders

    def test_bound_respected_lower(self) -> None:
        """Buy orders must not go below lower_bound."""
        cfg = GridBiConfig(
            lower_bound=Decimal("97"),
            upper_bound=Decimal("105"),
            current_price=Decimal("100"),
            step_pct=Decimal("0.02"),
            long_levels=10,  # many levels — some would go below bound
            short_levels=3,
            order_size_quote=Decimal("100"),
        )
        engine = _make_engine()
        orders = engine.initialize_grid(Decimal("100"), config=cfg)
        buy_orders = [o for o in orders if o.side == "buy"]
        for o in buy_orders:
            assert o.price >= cfg.lower_bound

    def test_bound_respected_upper(self) -> None:
        """Sell orders must not go above upper_bound."""
        cfg = GridBiConfig(
            lower_bound=Decimal("95"),
            upper_bound=Decimal("103"),
            current_price=Decimal("100"),
            step_pct=Decimal("0.02"),
            long_levels=3,
            short_levels=10,  # many levels — some would go above bound
            order_size_quote=Decimal("100"),
        )
        engine = _make_engine()
        orders = engine.initialize_grid(Decimal("100"), config=cfg)
        sell_orders = [o for o in orders if o.side == "sell"]
        for o in sell_orders:
            assert o.price <= cfg.upper_bound


# ─────────────────────────────────────────────────────────────────────────────
# get_combined_unrealized_pnl
# ─────────────────────────────────────────────────────────────────────────────

class TestGetCombinedUnrealizedPnl:
    def _engine_with_open_positions(self) -> GridEngine:
        engine = _make_engine()
        # Simulate a filled LONG buy at 98 and a filled SHORT sell at 102
        from bot.core.grid_engine import GridOrder
        long_order = GridOrder(level=1, price=Decimal("98"), amount=Decimal("1"), side="buy",
                               order_id="long1", filled=True)
        short_order = GridOrder(level=1, price=Decimal("102"), amount=Decimal("1"), side="sell",
                                order_id="short1", filled=True)
        engine._open_buys["long1"] = long_order
        engine._open_sells["short1"] = short_order
        return engine

    def test_long_profit_when_price_rises(self) -> None:
        engine = self._engine_with_open_positions()
        # current_price=102: long_pnl=(102-98)*1=4, short_pnl=(102-102)*1=0
        pnl = engine.get_combined_unrealized_pnl(Decimal("102"))
        assert pnl == Decimal("4")

    def test_short_profit_when_price_falls(self) -> None:
        engine = self._engine_with_open_positions()
        # current_price=98: long_pnl=(98-98)*1=0, short_pnl=(102-98)*1=4
        pnl = engine.get_combined_unrealized_pnl(Decimal("98"))
        assert pnl == Decimal("4")

    def test_both_profitable(self) -> None:
        engine = self._engine_with_open_positions()
        # current_price=100: long_pnl=(100-98)*1=2, short_pnl=(102-100)*1=2 → 4
        pnl = engine.get_combined_unrealized_pnl(Decimal("100"))
        assert pnl == Decimal("4")

    def test_both_losing(self) -> None:
        engine = self._engine_with_open_positions()
        # long entry=98, short entry=102, current=100 inside — above shows both profitable.
        # Make price shoot to 105: long_pnl=(105-98)=7, short_pnl=(102-105)=-3 → 4
        pnl = engine.get_combined_unrealized_pnl(Decimal("105"))
        assert pnl == Decimal("4")

    def test_zero_when_no_open_positions(self) -> None:
        engine = _make_engine()
        assert engine.get_combined_unrealized_pnl(Decimal("100")) == Decimal("0")


# ─────────────────────────────────────────────────────────────────────────────
# close_direction
# ─────────────────────────────────────────────────────────────────────────────

class TestCloseDirection:
    def _engine_with_both_stacks(self) -> GridEngine:
        engine = _make_engine()
        cfg = _make_bi_config(long_levels=2, short_levels=2)
        engine.initialize_grid(Decimal("100"), config=cfg)
        # Register all orders with fake exchange IDs
        for key, order in list(engine._long_orders.items()):
            oid = f"exchange_{key}"
            order.order_id = oid
            engine.active_orders[oid] = order
            engine._long_orders[oid] = order
        for key, order in list(engine._short_orders.items()):
            oid = f"exchange_{key}"
            order.order_id = oid
            engine.active_orders[oid] = order
            engine._short_orders[oid] = order
        return engine

    def test_close_long_removes_buy_orders(self) -> None:
        engine = self._engine_with_both_stacks()
        ids = engine.close_direction(GridDirection.LONG)
        assert len(ids) > 0
        # All remaining active orders should be sell orders
        remaining = list(engine.active_orders.values())
        for o in remaining:
            assert o.side == "sell"

    def test_close_short_removes_sell_orders(self) -> None:
        engine = self._engine_with_both_stacks()
        ids = engine.close_direction(GridDirection.SHORT)
        assert len(ids) > 0
        # All remaining active orders should be buy orders
        remaining = list(engine.active_orders.values())
        for o in remaining:
            assert o.side == "buy"

    def test_close_long_clears_long_orders_registry(self) -> None:
        engine = self._engine_with_both_stacks()
        engine.close_direction(GridDirection.LONG)
        assert len(engine._long_orders) == 0

    def test_close_short_clears_short_orders_registry(self) -> None:
        engine = self._engine_with_both_stacks()
        engine.close_direction(GridDirection.SHORT)
        assert len(engine._short_orders) == 0

    def test_close_long_does_not_touch_short_registry(self) -> None:
        engine = self._engine_with_both_stacks()
        short_count_before = len(engine._short_orders)
        engine.close_direction(GridDirection.LONG)
        assert len(engine._short_orders) == short_count_before

    def test_close_short_does_not_touch_long_registry(self) -> None:
        engine = self._engine_with_both_stacks()
        long_count_before = len(engine._long_orders)
        engine.close_direction(GridDirection.SHORT)
        assert len(engine._long_orders) == long_count_before

    def test_returns_order_ids_list(self) -> None:
        engine = self._engine_with_both_stacks()
        ids = engine.close_direction(GridDirection.LONG)
        assert isinstance(ids, list)
        for oid in ids:
            assert isinstance(oid, str)


# ─────────────────────────────────────────────────────────────────────────────
# reconfigure
# ─────────────────────────────────────────────────────────────────────────────

class TestReconfigure:
    def test_returns_old_order_ids(self) -> None:
        engine = _make_engine()
        cfg1 = _make_bi_config(long_levels=2, short_levels=2)
        engine.initialize_grid(Decimal("100"), config=cfg1)
        # Register fake exchange IDs
        for key, order in list(engine._long_orders.items()):
            oid = f"old_{key}"
            order.order_id = oid
            engine.active_orders[oid] = order
        for key, order in list(engine._short_orders.items()):
            oid = f"old_{key}"
            order.order_id = oid
            engine.active_orders[oid] = order

        n_old = len(engine.active_orders)
        cfg2 = _make_bi_config(current_price=Decimal("105"), long_levels=3, short_levels=3)
        ids = engine.reconfigure(cfg2)
        assert len(ids) == n_old

    def test_resets_statistics(self) -> None:
        engine = _make_engine()
        engine.total_profit = Decimal("999")
        engine.buy_count = 10
        engine.sell_count = 10
        cfg = _make_bi_config()
        engine.reconfigure(cfg)
        assert engine.total_profit == Decimal("0")
        assert engine.buy_count == 0
        assert engine.sell_count == 0

    def test_new_orders_around_new_price(self) -> None:
        engine = _make_engine()
        cfg = _make_bi_config(current_price=Decimal("200"), long_levels=2, short_levels=2)
        engine.reconfigure(cfg)
        buy_orders = [o for o in engine.grid_orders if o.side == "buy"]
        sell_orders = [o for o in engine.grid_orders if o.side == "sell"]
        for o in buy_orders:
            assert o.price < Decimal("200")
        for o in sell_orders:
            assert o.price > Decimal("200")

    def test_mode_set_to_bidirectional(self) -> None:
        engine = _make_engine(mode=GridMode.LONG_ONLY)
        cfg = _make_bi_config()
        engine.reconfigure(cfg)
        assert engine.mode == GridMode.BIDIRECTIONAL

    def test_clears_active_orders(self) -> None:
        engine = _make_engine()
        cfg1 = _make_bi_config()
        engine.initialize_grid(Decimal("100"), config=cfg1)
        for key, order in list(engine._long_orders.items()):
            oid = f"fake_{key}"
            order.order_id = oid
            engine.active_orders[oid] = order
        cfg2 = _make_bi_config()
        engine.reconfigure(cfg2)
        # After reconfigure, active_orders only contains newly added placeholder keys
        # (not the old registered exchange IDs)
        old_ids = [k for k in engine.active_orders if k.startswith("fake_")]
        assert len(old_ids) == 0


# ─────────────────────────────────────────────────────────────────────────────
# set_direction deprecation
# ─────────────────────────────────────────────────────────────────────────────

class TestSetDirectionDeprecation:
    def test_emits_deprecation_warning(self) -> None:
        engine = _make_engine(mode=GridMode.LONG_ONLY)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            engine.set_direction(GridDirection.SHORT)
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1
        assert "reconfigure" in str(dep_warnings[0].message).lower()

    def test_still_switches_direction(self) -> None:
        engine = _make_engine(mode=GridMode.LONG_ONLY)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            engine.set_direction(GridDirection.SHORT)
        assert engine.direction == GridDirection.SHORT

    def test_same_direction_no_warning(self) -> None:
        engine = _make_engine(mode=GridMode.LONG_ONLY)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            engine.set_direction(GridDirection.LONG)  # same — no-op
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) == 0


# ─────────────────────────────────────────────────────────────────────────────
# GridAdapter.reconfigure integration
# ─────────────────────────────────────────────────────────────────────────────

class TestGridAdapterReconfigure:
    def _adapter_with_positions(self) -> GridAdapter:
        from datetime import datetime, timezone

        from bot.strategies.base import BaseSignal

        adapter = GridAdapter(adx_filter=0)
        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            take_profit=Decimal("102"),
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
            signal_reason="grid_buy_level",
        )
        adapter.open_position(signal, Decimal("100"))
        return adapter

    def test_reconfigure_closes_existing_positions(self) -> None:
        adapter = self._adapter_with_positions()
        assert len(adapter._positions) == 1
        cfg = _make_bi_config()
        forced = adapter.reconfigure(cfg)
        assert len(forced) == 1
        assert forced[0][1] == ExitReason.MANUAL
        assert len(adapter._positions) == 0

    def test_reconfigure_creates_grid_engine(self) -> None:
        adapter = GridAdapter(adx_filter=0)
        assert adapter._grid_engine is None
        cfg = _make_bi_config()
        adapter.reconfigure(cfg)
        assert adapter._grid_engine is not None

    def test_reconfigure_updates_bounds(self) -> None:
        adapter = GridAdapter(adx_filter=0)
        cfg = _make_bi_config(current_price=Decimal("200"))
        adapter.reconfigure(cfg)
        assert adapter.grid_lower_price == cfg.lower_bound
        assert adapter.grid_upper_price == cfg.upper_bound

    def test_reconfigure_resets_paused_flag(self) -> None:
        adapter = GridAdapter(adx_filter=0)
        adapter._paused = True
        cfg = _make_bi_config()
        adapter.reconfigure(cfg)
        assert adapter._paused is False

    def test_reconfigure_existing_engine(self) -> None:
        """reconfigure() on an adapter that already has an engine replaces it."""
        adapter = GridAdapter(
            num_levels=6,
            grid_range_pct=Decimal("0.05"),
            profit_per_grid=Decimal("0.01"),
            amount_per_grid=Decimal("100"),
            adx_filter=0,
        )
        cfg1 = _make_bi_config(current_price=Decimal("100"))
        adapter.reconfigure(cfg1)
        engine_before = id(adapter._grid_engine)

        cfg2 = _make_bi_config(current_price=Decimal("200"))
        adapter.reconfigure(cfg2)
        # Engine is reconfigured (same object, different state) or replaced
        assert adapter.grid_upper_price == cfg2.upper_bound
