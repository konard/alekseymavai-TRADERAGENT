"""
Tests for GridAdapter recovery integration hooks.

Covers:
- recovery_triggered flag when price < grid_lower with recovery enabled
- Paused state: generate_signal returns None
- get_underwater_snapshot returns positions without closing
- resume() reinitializes grid with new range
- Default behaviour (recovery disabled): STOP_LOSS as before
"""

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from bot.strategies.grid_adapter import GridAdapter


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def adapter() -> GridAdapter:
    return GridAdapter(
        symbol="BTC/USDT",
        num_levels=6,
        amount_per_grid=Decimal("150"),
        profit_per_grid=Decimal("0.012"),
        grid_range_pct=Decimal("0.05"),
    )


def _make_df(prices: list[float]) -> pd.DataFrame:
    """Create minimal OHLCV DataFrame from a list of prices."""
    n = len(prices)
    return pd.DataFrame({
        "open": prices,
        "high": [p * 1.001 for p in prices],
        "low": [p * 0.999 for p in prices],
        "close": prices,
        "volume": [1000.0] * n,
    })


# =============================================================================
# Default behaviour (recovery disabled)
# =============================================================================


class TestDefaultStopLoss:
    def test_stop_loss_when_recovery_disabled(self, adapter: GridAdapter):
        """Без recovery → STOP_LOSS при price < grid_lower (поведение по умолчанию)."""
        df = _make_df([100.0] * 30)
        adapter.analyze_market(df)

        # Открываем позицию
        signal = adapter.generate_signal(df, Decimal("10000"))
        if signal:
            adapter.open_position(signal, Decimal("150"))

        # Имитируем позицию вручную если сигнал не сгенерировался
        if not adapter._positions:
            adapter._positions["test1"] = {
                "direction": "long",
                "entry_price": Decimal("100"),
                "stop_loss": adapter._grid_lower_price,
                "take_profit": Decimal("102"),
                "size": Decimal("150"),
                "entry_time": None,
                "current_price": Decimal("100"),
            }

        # Цена падает ниже grid_lower
        low_price = adapter._grid_lower_price - Decimal("1")
        exits = adapter.update_positions(low_price, df)
        assert len(exits) > 0
        assert all(reason.value == "stop_loss" for _, reason in exits)


# =============================================================================
# Recovery-enabled behaviour
# =============================================================================


class TestRecoveryEnabled:
    def test_recovery_triggered_flag(self, adapter: GridAdapter):
        """С recovery → flag, не STOP_LOSS."""
        adapter.set_recovery_enabled(True)
        df = _make_df([100.0] * 30)
        adapter.analyze_market(df)

        # Ставим позицию
        adapter._positions["test1"] = {
            "direction": "long",
            "entry_price": Decimal("100"),
            "stop_loss": adapter._grid_lower_price,
            "take_profit": Decimal("102"),
            "size": Decimal("150"),
            "entry_time": None,
            "current_price": Decimal("100"),
        }

        low_price = adapter._grid_lower_price - Decimal("1")
        exits = adapter.update_positions(low_price, df)

        # Нет exits, но recovery_triggered
        assert len(exits) == 0
        assert adapter.recovery_triggered is True
        assert adapter._paused is True

    def test_generate_signal_returns_none_when_paused(self, adapter: GridAdapter):
        adapter.set_recovery_enabled(True)
        adapter._paused = True
        df = _make_df([100.0] * 30)
        signal = adapter.generate_signal(df, Decimal("10000"))
        assert signal is None


# =============================================================================
# get_underwater_snapshot
# =============================================================================


class TestUnderwaterSnapshot:
    def test_returns_positions(self, adapter: GridAdapter):
        adapter._positions = {
            "p1": {"entry_price": Decimal("100"), "size": Decimal("150"), "other": "data"},
            "p2": {"entry_price": Decimal("99"), "size": Decimal("150"), "other": "data"},
        }
        snapshot = adapter.get_underwater_snapshot()
        assert len(snapshot) == 2
        assert snapshot[0]["pos_id"] == "p1"
        assert snapshot[0]["entry_price"] == Decimal("100")
        assert snapshot[0]["size"] == Decimal("150")

    def test_positions_not_closed(self, adapter: GridAdapter):
        adapter._positions["p1"] = {
            "entry_price": Decimal("100"),
            "size": Decimal("150"),
        }
        adapter.get_underwater_snapshot()
        assert "p1" in adapter._positions  # позиция всё ещё открыта


# =============================================================================
# pause / resume
# =============================================================================


class TestPauseResume:
    def test_pause(self, adapter: GridAdapter):
        adapter.pause()
        assert adapter._paused is True

    def test_resume_reinitializes_grid(self, adapter: GridAdapter):
        adapter.set_recovery_enabled(True)
        adapter._paused = True
        adapter._recovery_triggered = True

        adapter.resume(Decimal("90"), Decimal("100"))
        assert adapter._paused is False
        assert adapter._recovery_triggered is False
        assert adapter._grid_engine is not None
        assert len(adapter._grid_levels) > 0
        assert adapter._grid_lower_price == Decimal("90")


# =============================================================================
# clear_recovery_trigger
# =============================================================================


class TestClearTrigger:
    def test_clear(self, adapter: GridAdapter):
        adapter._recovery_triggered = True
        adapter.clear_recovery_trigger()
        assert adapter.recovery_triggered is False


# =============================================================================
# reset clears recovery state
# =============================================================================


class TestResetRecovery:
    def test_reset_clears_all(self, adapter: GridAdapter):
        adapter._recovery_triggered = True
        adapter._paused = True
        adapter.reset()
        assert adapter._recovery_triggered is False
        assert adapter._paused is False
