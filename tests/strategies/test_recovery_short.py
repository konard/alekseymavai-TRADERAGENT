"""
Tests for RecoveryCoordinator SHORT direction support.

Covers:
- DCA levels build upward for SHORT recovery
- DCA signals are SHORT direction
- TP triggers on price drop (SHORT)
- find_smc_resistance()
- Bidirectional enter_recovery
"""

from decimal import Decimal

import pytest

from bot.strategies.base import SignalDirection
from bot.strategies.hybrid.recovery_config import RecoveryConfig
from bot.strategies.hybrid.recovery_coordinator import (
    RecoveryCoordinator,
    RecoveryPhase,
    UnderwaterPosition,
)


def _make_config(**kwargs) -> RecoveryConfig:
    defaults = {
        "enabled": True,
        "max_dca_orders": 5,
        "dca_step_pct": Decimal("0.02"),
        "dca_volume_multiplier": Decimal("1.5"),
        "tp_target_pct": Decimal("0.01"),
        "timeout_bars": 100,
        "timeout_action": "close_all",
        "fallback_support_pct": Decimal("0.10"),
        "cooldown_after_recovery_bars": 5,
    }
    defaults.update(kwargs)
    return RecoveryConfig(**defaults)


def _make_underwater(entry_price: float, size: float = 100.0) -> UnderwaterPosition:
    return UnderwaterPosition(
        pos_id=f"pos_{entry_price}",
        entry_price=Decimal(str(entry_price)),
        size=Decimal(str(size)),
    )


class TestRecoveryShortDCALevels:
    def test_short_dca_levels_build_upward(self) -> None:
        """SHORT recovery: DCA levels go UP from current price toward resistance."""
        coord = RecoveryCoordinator(_make_config())
        state = coord.enter_recovery(
            grid_positions=[_make_underwater(980)],
            current_price=Decimal("1010"),
            current_bar=0,
            smc_support=Decimal("1100"),  # resistance above
            direction=SignalDirection.SHORT,
        )
        # Levels should be ascending (above current price)
        assert len(state.dca_levels) > 0
        for level in state.dca_levels:
            assert level > Decimal("1010")
        # All levels should be <= resistance
        for level in state.dca_levels:
            assert level <= Decimal("1100")

    def test_short_dca_levels_fallback_resistance(self) -> None:
        """SHORT recovery without explicit resistance → fallback = price * (1 + pct)."""
        coord = RecoveryCoordinator(_make_config(fallback_support_pct=Decimal("0.10")))
        state = coord.enter_recovery(
            grid_positions=[_make_underwater(980)],
            current_price=Decimal("1000"),
            current_bar=0,
            smc_support=None,  # no SMC data → fallback
            direction=SignalDirection.SHORT,
        )
        assert state.smc_support == Decimal("1000") * (Decimal("1") + Decimal("0.10"))
        assert len(state.dca_levels) > 0


class TestRecoveryShortSignals:
    def test_short_dca_signals_are_short(self) -> None:
        """DCA signals generated during SHORT recovery have SHORT direction."""
        coord = RecoveryCoordinator(_make_config(dca_step_pct=Decimal("0.02")))
        state = coord.enter_recovery(
            grid_positions=[_make_underwater(980)],
            current_price=Decimal("1000"),
            current_bar=0,
            smc_support=Decimal("1100"),
            direction=SignalDirection.SHORT,
        )
        # Price rises to first DCA level
        first_level = state.dca_levels[0]
        action = coord.on_price_update(first_level)
        assert len(action.dca_signals) >= 1
        for sig in action.dca_signals:
            assert sig.direction == SignalDirection.SHORT

    def test_short_dca_trigger_on_price_rise(self) -> None:
        """SHORT DCA: signals trigger when price rises to level (not when it drops)."""
        coord = RecoveryCoordinator(_make_config(dca_step_pct=Decimal("0.02")))
        state = coord.enter_recovery(
            grid_positions=[_make_underwater(980)],
            current_price=Decimal("1000"),
            current_bar=0,
            smc_support=Decimal("1100"),
            direction=SignalDirection.SHORT,
        )
        # Price stays below first level → no signal
        action = coord.on_price_update(Decimal("1005"))
        assert len(action.dca_signals) == 0

        # Price reaches first level → signal
        first_level = state.dca_levels[0]
        action = coord.on_price_update(first_level + Decimal("1"))
        assert len(action.dca_signals) >= 1


class TestRecoveryShortTP:
    def test_short_tp_triggers_on_price_drop(self) -> None:
        """SHORT recovery TP: blended avg * (1 - tp_pct) → triggers when price drops."""
        coord = RecoveryCoordinator(_make_config(tp_target_pct=Decimal("0.01")))
        state = coord.enter_recovery(
            grid_positions=[_make_underwater(1000, 100)],
            current_price=Decimal("1010"),
            current_bar=0,
            smc_support=Decimal("1100"),
            direction=SignalDirection.SHORT,
        )
        # TP target should be below blended avg (for SHORT)
        assert state.tp_target < state.blended_avg_entry

        # Price drops to TP → should_close_all
        action = coord.on_price_update(state.tp_target - Decimal("1"))
        assert action.should_close_all is True

    def test_short_tp_no_trigger_on_price_rise(self) -> None:
        """SHORT TP does NOT trigger when price rises."""
        coord = RecoveryCoordinator(_make_config(tp_target_pct=Decimal("0.01")))
        state = coord.enter_recovery(
            grid_positions=[_make_underwater(1000, 100)],
            current_price=Decimal("1010"),
            current_bar=0,
            smc_support=Decimal("1100"),
            direction=SignalDirection.SHORT,
        )
        # Price rises further → should NOT close
        action = coord.on_price_update(Decimal("1050"))
        assert action.should_close_all is False


class TestFindSMCResistance:
    def test_find_resistance_from_bearish_obs(self) -> None:
        """find_smc_resistance returns closest bearish OB above price."""
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ob1 = MagicMock()
        ob1.ob_type.value = "bear"
        ob1.invalidated = False
        ob1.low = 1050.0

        ob2 = MagicMock()
        ob2.ob_type.value = "bear"
        ob2.invalidated = False
        ob2.low = 1100.0

        ob3 = MagicMock()
        ob3.ob_type.value = "bull"
        ob3.invalidated = False
        ob3.low = 1080.0

        ctx.order_blocks = [ob1, ob2, ob3]
        ctx.swing_highs = []

        result = RecoveryCoordinator.find_smc_resistance(ctx, Decimal("1000"))
        assert result is not None
        assert result == Decimal("1050")  # closest bearish OB

    def test_find_resistance_returns_none_when_empty(self) -> None:
        """No resistance found → returns None."""
        result = RecoveryCoordinator.find_smc_resistance(None, Decimal("1000"))
        assert result is None

    def test_find_resistance_from_swing_highs(self) -> None:
        """find_smc_resistance returns swing highs above price."""
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.order_blocks = []

        sw1 = MagicMock()
        sw1.price = 1030.0
        sw2 = MagicMock()
        sw2.price = 950.0  # below price, should be ignored

        ctx.swing_highs = [sw1, sw2]

        result = RecoveryCoordinator.find_smc_resistance(ctx, Decimal("1000"))
        assert result == Decimal("1030")


class TestRecoveryLongStillWorks:
    """Regression: LONG recovery should not be broken by direction changes."""

    def test_long_recovery_unchanged(self) -> None:
        coord = RecoveryCoordinator(_make_config())
        state = coord.enter_recovery(
            grid_positions=[_make_underwater(1000, 100)],
            current_price=Decimal("990"),
            current_bar=0,
            smc_support=Decimal("950"),
            direction=SignalDirection.LONG,
        )
        assert state.direction == SignalDirection.LONG
        # DCA levels should be descending
        for level in state.dca_levels:
            assert level < Decimal("990")
        # TP above blended avg
        assert state.tp_target > state.blended_avg_entry
