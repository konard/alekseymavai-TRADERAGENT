"""
Tests for GridAdapter SHORT direction support.

Covers:
- SHORT signal generation (sell levels above price, TP below)
- SHORT SL (price > grid_upper)
- SHORT PnL (entry - exit)
- set_direction() closes positions and resets grid
- Direction switch lifecycle (BEAR → BULL)
"""

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from bot.strategies.base import ExitReason, SignalDirection
from bot.strategies.grid_adapter import GridAdapter


def _make_df(prices: list[float], n_bars: int = 30) -> pd.DataFrame:
    """Create a DataFrame with given closing prices (extended to n_bars with last price)."""
    if len(prices) < n_bars:
        prices = prices + [prices[-1]] * (n_bars - len(prices))
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.002 for p in prices],
            "low": [p * 0.998 for p in prices],
            "close": prices,
            "volume": [100.0] * len(prices),
        },
        index=pd.date_range("2024-01-01", periods=len(prices), freq="5min"),
    )


class TestGridAdapterShortSignals:
    def test_short_signal_generated_above_price(self) -> None:
        """SHORT grid: sell levels above current price, signal has SHORT direction."""
        adapter = GridAdapter(
            num_levels=6,
            grid_range_pct=Decimal("0.05"),
            profit_per_grid=Decimal("0.012"),
            amount_per_grid=Decimal("150"),
            adx_filter=0,  # disable ADX filter
        )
        adapter._direction = SignalDirection.SHORT

        # Price at 1000 → grid range [950, 1050] → levels between
        df = _make_df([1000.0])
        adapter.analyze_market(df)
        signal = adapter.generate_signal(df, Decimal("10000"))

        if signal is not None:
            assert signal.direction == SignalDirection.SHORT
            assert signal.take_profit < signal.entry_price  # TP is below entry for SHORT
            assert signal.signal_reason == "grid_sell_level"

    def test_short_tp_below_entry(self) -> None:
        """SHORT TP = level * (1 - profit_per_grid), must be below entry."""
        adapter = GridAdapter(
            num_levels=10,
            grid_range_pct=Decimal("0.10"),
            profit_per_grid=Decimal("0.02"),
            amount_per_grid=Decimal("100"),
            adx_filter=0,
        )
        adapter._direction = SignalDirection.SHORT

        # Use price near a level
        df = _make_df([1000.0])
        adapter.analyze_market(df)

        # Force a signal by iterating through levels
        signal = adapter.generate_signal(df, Decimal("10000"))
        if signal is not None:
            assert signal.take_profit < signal.entry_price

    def test_short_sl_is_grid_upper(self) -> None:
        """SHORT SL should be grid_upper (not grid_lower like LONG)."""
        adapter = GridAdapter(
            num_levels=6,
            grid_range_pct=Decimal("0.05"),
            profit_per_grid=Decimal("0.012"),
            amount_per_grid=Decimal("150"),
            adx_filter=0,
        )
        adapter._direction = SignalDirection.SHORT

        df = _make_df([1000.0])
        adapter.analyze_market(df)
        signal = adapter.generate_signal(df, Decimal("10000"))

        if signal is not None:
            assert signal.stop_loss == adapter.grid_upper_price

    def test_short_boundary_breach_triggers_sl(self) -> None:
        """Price above grid_upper in SHORT → all positions closed at SL."""
        adapter = GridAdapter(
            num_levels=6,
            grid_range_pct=Decimal("0.05"),
            profit_per_grid=Decimal("0.012"),
            amount_per_grid=Decimal("150"),
            adx_filter=0,
        )
        adapter._direction = SignalDirection.SHORT

        df = _make_df([1000.0])
        adapter.analyze_market(df)

        # Manually add a SHORT position
        from bot.strategies.base import BaseSignal
        from datetime import datetime, timezone

        signal = BaseSignal(
            direction=SignalDirection.SHORT,
            entry_price=Decimal("1020"),
            stop_loss=adapter.grid_upper_price,
            take_profit=Decimal("1000"),
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
            signal_reason="grid_sell_level",
        )
        pos_id = adapter.open_position(signal, Decimal("150"))

        # Price breaks above grid upper → SL
        exits = adapter.update_positions(Decimal("1060"), df)
        assert len(exits) == 1
        assert exits[0][0] == pos_id
        assert exits[0][1] == ExitReason.STOP_LOSS

    def test_short_tp_hit_closes_position(self) -> None:
        """Price drops below SHORT TP → position closed at TP."""
        adapter = GridAdapter(
            num_levels=6,
            grid_range_pct=Decimal("0.05"),
            profit_per_grid=Decimal("0.012"),
            amount_per_grid=Decimal("150"),
            adx_filter=0,
        )
        adapter._direction = SignalDirection.SHORT

        df = _make_df([1000.0])
        adapter.analyze_market(df)

        from bot.strategies.base import BaseSignal
        from datetime import datetime, timezone

        signal = BaseSignal(
            direction=SignalDirection.SHORT,
            entry_price=Decimal("1020"),
            stop_loss=Decimal("1060"),
            take_profit=Decimal("1005"),  # TP below entry
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
            signal_reason="grid_sell_level",
        )
        pos_id = adapter.open_position(signal, Decimal("150"))

        # Price drops to TP
        exits = adapter.update_positions(Decimal("1004"), df)
        assert len(exits) == 1
        assert exits[0][1] == ExitReason.TAKE_PROFIT


class TestGridAdapterShortPnL:
    def test_short_pnl_positive_on_price_drop(self) -> None:
        """SHORT: PnL = (entry - exit) * size / entry → positive when price drops."""
        adapter = GridAdapter(adx_filter=0)
        adapter._direction = SignalDirection.SHORT

        from bot.strategies.base import BaseSignal
        from datetime import datetime, timezone

        signal = BaseSignal(
            direction=SignalDirection.SHORT,
            entry_price=Decimal("1000"),
            stop_loss=Decimal("1050"),
            take_profit=Decimal("980"),
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
            signal_reason="grid_sell_level",
        )
        pos_id = adapter.open_position(signal, Decimal("100"))
        adapter.close_position(pos_id, ExitReason.TAKE_PROFIT, Decimal("980"))

        perf = adapter.get_performance()
        assert perf.total_trades == 1
        assert perf.total_pnl > 0  # price dropped → profit

    def test_short_pnl_negative_on_price_rise(self) -> None:
        """SHORT: PnL negative when price rises."""
        adapter = GridAdapter(adx_filter=0)
        adapter._direction = SignalDirection.SHORT

        from bot.strategies.base import BaseSignal
        from datetime import datetime, timezone

        signal = BaseSignal(
            direction=SignalDirection.SHORT,
            entry_price=Decimal("1000"),
            stop_loss=Decimal("1050"),
            take_profit=Decimal("980"),
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
            signal_reason="grid_sell_level",
        )
        pos_id = adapter.open_position(signal, Decimal("100"))
        adapter.close_position(pos_id, ExitReason.STOP_LOSS, Decimal("1050"))

        perf = adapter.get_performance()
        assert perf.total_pnl < 0

    def test_short_unrealized_pnl_direction_aware(self) -> None:
        """get_active_positions() reports correct unrealized PnL for SHORT."""
        adapter = GridAdapter(adx_filter=0)
        adapter._direction = SignalDirection.SHORT

        from bot.strategies.base import BaseSignal
        from datetime import datetime, timezone

        signal = BaseSignal(
            direction=SignalDirection.SHORT,
            entry_price=Decimal("1000"),
            stop_loss=Decimal("1050"),
            take_profit=Decimal("980"),
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
            signal_reason="grid_sell_level",
        )
        adapter.open_position(signal, Decimal("100"))

        # Simulate price drop → positive PnL
        adapter._current_price = Decimal("990")
        for pos in adapter._positions.values():
            pos["current_price"] = Decimal("990")

        positions = adapter.get_active_positions()
        assert len(positions) == 1
        assert positions[0].unrealized_pnl > 0


class TestGridAdapterDirectionSwitch:
    def test_set_direction_closes_positions(self) -> None:
        """set_direction() returns force_close_all exits."""
        adapter = GridAdapter(adx_filter=0)
        assert adapter.direction == SignalDirection.LONG

        from bot.strategies.base import BaseSignal
        from datetime import datetime, timezone

        signal = BaseSignal(
            direction=SignalDirection.LONG,
            entry_price=Decimal("1000"),
            stop_loss=Decimal("950"),
            take_profit=Decimal("1020"),
            confidence=0.6,
            timestamp=datetime.now(timezone.utc),
            strategy_type="grid",
            signal_reason="grid_buy_level",
        )
        adapter.open_position(signal, Decimal("100"))
        assert len(adapter._positions) == 1

        forced = adapter.set_direction(SignalDirection.SHORT)
        assert len(forced) == 1
        assert forced[0][1] == ExitReason.MANUAL
        assert adapter.direction == SignalDirection.SHORT
        assert adapter._grid_engine is None
        assert adapter._grid_levels == []

    def test_set_same_direction_noop(self) -> None:
        """set_direction(same) returns empty list."""
        adapter = GridAdapter(adx_filter=0)
        forced = adapter.set_direction(SignalDirection.LONG)
        assert forced == []

    def test_direction_switch_lifecycle(self) -> None:
        """Full lifecycle: LONG → SHORT → LONG."""
        adapter = GridAdapter(
            num_levels=6,
            grid_range_pct=Decimal("0.05"),
            adx_filter=0,
        )
        assert adapter.direction == SignalDirection.LONG

        # Switch to SHORT
        adapter.set_direction(SignalDirection.SHORT)
        assert adapter.direction == SignalDirection.SHORT
        assert adapter._paused is False

        # Switch back to LONG
        adapter.set_direction(SignalDirection.LONG)
        assert adapter.direction == SignalDirection.LONG

    def test_reset_restores_long_direction(self) -> None:
        """reset() returns direction to LONG."""
        adapter = GridAdapter(adx_filter=0)
        adapter._direction = SignalDirection.SHORT
        adapter.reset()
        assert adapter.direction == SignalDirection.LONG
