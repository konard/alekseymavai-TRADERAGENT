"""
Tests for RecoveryCoordinator — DCA recovery when Grid hits lower boundary.

Covers:
- should_trigger: enabled/disabled, cooldown, price checks
- enter_recovery: DCA levels, blended avg, TP target
- on_price_update: TP hit, timeout, DCA signal generation
- find_smc_support: bullish OB, swing low, fallback
- blended avg calculation with Grid + DCA fills
"""

from decimal import Decimal

import pytest

from bot.strategies.hybrid.recovery_config import RecoveryConfig
from bot.strategies.hybrid.recovery_coordinator import (
    RecoveryAction,
    RecoveryCoordinator,
    RecoveryPhase,
    RecoveryState,
    UnderwaterPosition,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def config() -> RecoveryConfig:
    return RecoveryConfig(
        enabled=True,
        tp_target_pct=Decimal("0.01"),
        max_dca_orders=5,
        dca_step_pct=Decimal("0.015"),
        dca_volume_multiplier=Decimal("1.3"),
        timeout_bars=12,
        timeout_action="close_all",
        fallback_support_pct=Decimal("0.05"),
        max_recovery_capital_pct=Decimal("0.30"),
        cooldown_after_recovery_bars=6,
    )


@pytest.fixture
def coordinator(config: RecoveryConfig) -> RecoveryCoordinator:
    return RecoveryCoordinator(config)


@pytest.fixture
def grid_positions() -> list[UnderwaterPosition]:
    """3 Grid-позиции, купленные по 100, 99, 98."""
    return [
        UnderwaterPosition(pos_id="g1", entry_price=Decimal("100"), size=Decimal("150")),
        UnderwaterPosition(pos_id="g2", entry_price=Decimal("99"), size=Decimal("150")),
        UnderwaterPosition(pos_id="g3", entry_price=Decimal("98"), size=Decimal("150")),
    ]


# =============================================================================
# should_trigger
# =============================================================================


class TestShouldTrigger:
    def test_triggers_when_price_below_grid_lower(self, coordinator: RecoveryCoordinator):
        assert coordinator.should_trigger(Decimal("95"), Decimal("94")) is True

    def test_not_triggers_when_disabled(self):
        cfg = RecoveryConfig(enabled=False)
        coord = RecoveryCoordinator(cfg)
        assert coord.should_trigger(Decimal("95"), Decimal("94")) is False

    def test_not_triggers_when_price_above_lower(self, coordinator: RecoveryCoordinator):
        assert coordinator.should_trigger(Decimal("95"), Decimal("96")) is False

    def test_not_triggers_when_already_active(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        coordinator.enter_recovery(grid_positions, Decimal("94"), current_bar=0)
        assert coordinator.should_trigger(Decimal("95"), Decimal("93")) is False

    def test_not_triggers_during_cooldown(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        # Enter and finalize recovery to set cooldown
        coordinator.enter_recovery(grid_positions, Decimal("94"), current_bar=0)
        # Timeout to finalize
        for _ in range(12):
            coordinator.on_price_update(Decimal("93"))
        # Now cooling down
        assert coordinator.should_trigger(Decimal("95"), Decimal("94")) is False

    def test_not_triggers_grid_lower_zero(self, coordinator: RecoveryCoordinator):
        assert coordinator.should_trigger(Decimal("0"), Decimal("94")) is False


# =============================================================================
# enter_recovery
# =============================================================================


class TestEnterRecovery:
    def test_state_is_dca_active(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        state = coordinator.enter_recovery(grid_positions, Decimal("94"), current_bar=100)
        assert state.phase == RecoveryPhase.DCA_ACTIVE
        assert coordinator.is_active

    def test_dca_levels_built(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        state = coordinator.enter_recovery(
            grid_positions, Decimal("94"), current_bar=0, smc_support=Decimal("88")
        )
        assert len(state.dca_levels) > 0
        # Все уровни между current_price и support
        for level in state.dca_levels:
            assert level <= Decimal("94")
            assert level >= Decimal("88") or level == Decimal("88")

    def test_blended_avg_computed(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        state = coordinator.enter_recovery(grid_positions, Decimal("94"), current_bar=0)
        # Avg of 100, 99, 98 = 99 (equal sizes)
        assert state.blended_avg_entry == Decimal("99")
        assert state.blended_total_size == Decimal("450")

    def test_tp_target(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        state = coordinator.enter_recovery(grid_positions, Decimal("94"), current_bar=0)
        expected_tp = Decimal("99") * Decimal("1.01")
        assert state.tp_target == expected_tp

    def test_fallback_support_when_none(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        state = coordinator.enter_recovery(
            grid_positions, Decimal("94"), current_bar=0, smc_support=None
        )
        expected = Decimal("94") * Decimal("0.95")
        assert state.smc_support == expected

    def test_fallback_support_when_above_price(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        state = coordinator.enter_recovery(
            grid_positions, Decimal("94"), current_bar=0, smc_support=Decimal("100")
        )
        # Support above price → fallback
        expected = Decimal("94") * Decimal("0.95")
        assert state.smc_support == expected


# =============================================================================
# on_price_update — TP hit
# =============================================================================


class TestTPHit:
    def test_tp_hit_closes_all(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        coordinator.enter_recovery(grid_positions, Decimal("94"), current_bar=0)
        # TP = 99 * 1.01 = 99.99
        action = coordinator.on_price_update(Decimal("100"))
        assert action.should_close_all is True
        assert "recovery_tp_hit" in action.close_reason
        assert action.new_grid_range is not None
        assert not coordinator.is_active

    def test_tp_with_dca_fills(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        coordinator.enter_recovery(
            grid_positions, Decimal("94"), current_bar=0, smc_support=Decimal("85")
        )
        # Добавляем 2 DCA fill-а по более низкой цене
        dca_fills = [
            UnderwaterPosition(pos_id="d1", entry_price=Decimal("92"), size=Decimal("150")),
            UnderwaterPosition(pos_id="d2", entry_price=Decimal("90"), size=Decimal("195")),
        ]
        action = coordinator.on_price_update(Decimal("91"), new_fills=dca_fills)

        # blended avg = (100*150 + 99*150 + 98*150 + 92*150 + 90*195) / (150*4+195)
        state = coordinator.state  # None after TP, but let's check action
        # Если не TP — blended пересчитан
        # total_cost = 15000 + 14850 + 14700 + 13800 + 17550 = 75900
        # total_size = 795
        # avg = 75900/795 ≈ 95.47
        # TP = 95.47 * 1.01 ≈ 96.43
        # Цена 91 < 96.43 → TP не достигнут
        assert not action.should_close_all

    def test_blended_avg_recalculated_with_dca(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        coordinator.enter_recovery(
            grid_positions, Decimal("94"), current_bar=0, smc_support=Decimal("85")
        )
        dca_fills = [
            UnderwaterPosition(pos_id="d1", entry_price=Decimal("92"), size=Decimal("150")),
        ]
        coordinator.on_price_update(Decimal("91"), new_fills=dca_fills)
        state = coordinator.state
        assert state is not None
        # 4 позиции по 150: (100+99+98+92)*150 / 600 = 389*150/600 = 97.25
        expected_avg = (Decimal("100") + Decimal("99") + Decimal("98") + Decimal("92")) * Decimal("150") / Decimal("600")
        assert state.blended_avg_entry == expected_avg


# =============================================================================
# on_price_update — Timeout
# =============================================================================


class TestTimeout:
    def test_timeout_close_all(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        coordinator.enter_recovery(grid_positions, Decimal("94"), current_bar=0)
        # Прокручиваем 12 баров
        for i in range(11):
            action = coordinator.on_price_update(Decimal("93"))
            assert not action.should_close_all
        # 12-й бар — timeout
        action = coordinator.on_price_update(Decimal("93"))
        assert action.should_close_all
        assert "timeout" in action.close_reason
        assert not coordinator.is_active

    def test_timeout_hold(self, grid_positions: list[UnderwaterPosition]):
        cfg = RecoveryConfig(
            enabled=True,
            timeout_bars=3,
            timeout_action="hold",
        )
        coord = RecoveryCoordinator(cfg)
        coord.enter_recovery(grid_positions, Decimal("94"), current_bar=0)
        for _ in range(5):
            action = coord.on_price_update(Decimal("93"))
            assert not action.should_close_all  # hold — не закрываем


# =============================================================================
# DCA Signal Generation
# =============================================================================


class TestDCASignals:
    def test_dca_signals_generated_at_levels(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        coordinator.enter_recovery(
            grid_positions,
            Decimal("94"),
            current_bar=0,
            smc_support=Decimal("85"),
            base_order_size=Decimal("100"),
        )
        state = coordinator.state
        assert state is not None
        assert len(state.dca_levels) > 0

        # Цена падает до первого DCA-уровня
        first_level = state.dca_levels[0]
        action = coordinator.on_price_update(first_level - Decimal("0.01"))
        assert len(action.dca_signals) >= 1
        assert action.dca_signals[0].strategy_type == "recovery_dca"

    def test_dca_volume_multiplier(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        coordinator.enter_recovery(
            grid_positions,
            Decimal("94"),
            current_bar=0,
            smc_support=Decimal("80"),
            base_order_size=Decimal("100"),
        )
        state = coordinator.state
        assert state is not None

        # Цена падает ниже всех уровней → все сигналы за раз
        action = coordinator.on_price_update(Decimal("79"))
        if len(action.dca_signals) >= 2:
            # Второй ордер должен быть больше первого (мартингейл)
            meta0 = action.dca_signals[0].metadata or {}
            meta1 = action.dca_signals[1].metadata or {}
            assert meta1.get("multiplier", 0) > meta0.get("multiplier", 0)


# =============================================================================
# find_smc_support
# =============================================================================


class TestFindSMCSupport:
    def test_returns_none_without_context(self):
        result = RecoveryCoordinator.find_smc_support(None, Decimal("100"))
        assert result is None

    def test_finds_bullish_ob(self):
        """Мокаем MarketStructureContext с bullish OB."""
        from unittest.mock import MagicMock

        ctx = MagicMock()

        ob = MagicMock()
        ob.ob_type.value = "bull"
        ob.invalidated = False
        ob.high = 95.0
        ctx.order_blocks = [ob]
        ctx.swing_lows = []

        result = RecoveryCoordinator.find_smc_support(ctx, Decimal("100"))
        assert result == Decimal("95.0")

    def test_finds_swing_low(self):
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.order_blocks = []

        sw = MagicMock()
        sw.price = 92.0
        ctx.swing_lows = [sw]

        result = RecoveryCoordinator.find_smc_support(ctx, Decimal("100"))
        assert result == Decimal("92.0")

    def test_prefers_closer_support(self):
        from unittest.mock import MagicMock

        ctx = MagicMock()

        ob1 = MagicMock()
        ob1.ob_type.value = "bull"
        ob1.invalidated = False
        ob1.high = 90.0

        ob2 = MagicMock()
        ob2.ob_type.value = "bull"
        ob2.invalidated = False
        ob2.high = 97.0

        ctx.order_blocks = [ob1, ob2]
        ctx.swing_lows = []

        result = RecoveryCoordinator.find_smc_support(ctx, Decimal("100"))
        # Ближайший = 97
        assert result == Decimal("97.0")

    def test_ignores_invalidated_ob(self):
        from unittest.mock import MagicMock

        ctx = MagicMock()

        ob = MagicMock()
        ob.ob_type.value = "bull"
        ob.invalidated = True
        ob.high = 95.0
        ctx.order_blocks = [ob]
        ctx.swing_lows = []

        result = RecoveryCoordinator.find_smc_support(ctx, Decimal("100"))
        assert result is None

    def test_ignores_ob_above_price(self):
        from unittest.mock import MagicMock

        ctx = MagicMock()

        ob = MagicMock()
        ob.ob_type.value = "bull"
        ob.invalidated = False
        ob.high = 105.0
        ctx.order_blocks = [ob]
        ctx.swing_lows = []

        result = RecoveryCoordinator.find_smc_support(ctx, Decimal("100"))
        assert result is None


# =============================================================================
# Blended Avg Calculation
# =============================================================================


class TestBlendedCalculation:
    def test_grid_only(self):
        positions = [
            UnderwaterPosition("g1", Decimal("100"), Decimal("150")),
            UnderwaterPosition("g2", Decimal("98"), Decimal("150")),
        ]
        avg, total = RecoveryCoordinator._compute_blended(positions, [])
        assert avg == Decimal("99")
        assert total == Decimal("300")

    def test_grid_plus_dca(self):
        grid = [UnderwaterPosition("g1", Decimal("100"), Decimal("100"))]
        dca = [UnderwaterPosition("d1", Decimal("90"), Decimal("100"))]
        avg, total = RecoveryCoordinator._compute_blended(grid, dca)
        assert avg == Decimal("95")
        assert total == Decimal("200")

    def test_empty(self):
        avg, total = RecoveryCoordinator._compute_blended([], [])
        assert avg == Decimal("0")
        assert total == Decimal("0")

    def test_unequal_sizes(self):
        grid = [UnderwaterPosition("g1", Decimal("100"), Decimal("200"))]
        dca = [UnderwaterPosition("d1", Decimal("80"), Decimal("100"))]
        avg, total = RecoveryCoordinator._compute_blended(grid, dca)
        # (100*200 + 80*100) / 300 = 28000/300 ≈ 93.33
        expected = Decimal("28000") / Decimal("300")
        assert avg == expected
        assert total == Decimal("300")


# =============================================================================
# RecoveryConfig validation
# =============================================================================


class TestRecoveryConfig:
    def test_valid(self, config: RecoveryConfig):
        config.validate()  # не должно падать

    def test_invalid_tp(self):
        cfg = RecoveryConfig(tp_target_pct=Decimal("0"))
        with pytest.raises(ValueError, match="tp_target_pct"):
            cfg.validate()

    def test_invalid_timeout_action(self):
        cfg = RecoveryConfig(timeout_action="invalid")
        with pytest.raises(ValueError, match="timeout_action"):
            cfg.validate()

    def test_invalid_max_dca_orders(self):
        cfg = RecoveryConfig(max_dca_orders=0)
        with pytest.raises(ValueError, match="max_dca_orders"):
            cfg.validate()


# =============================================================================
# Statistics
# =============================================================================


class TestStatistics:
    def test_initial(self, coordinator: RecoveryCoordinator):
        stats = coordinator.get_statistics()
        assert stats["total_recoveries"] == 0
        assert stats["successful_recoveries"] == 0
        assert not stats["is_active"]

    def test_after_successful_recovery(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        coordinator.enter_recovery(grid_positions, Decimal("94"), current_bar=0)
        coordinator.on_price_update(Decimal("100"))  # TP hit
        stats = coordinator.get_statistics()
        assert stats["total_recoveries"] == 1
        assert stats["successful_recoveries"] == 1

    def test_after_timeout(
        self, coordinator: RecoveryCoordinator, grid_positions: list[UnderwaterPosition]
    ):
        coordinator.enter_recovery(grid_positions, Decimal("94"), current_bar=0)
        for _ in range(12):
            coordinator.on_price_update(Decimal("93"))
        stats = coordinator.get_statistics()
        assert stats["total_recoveries"] == 1
        assert stats["successful_recoveries"] == 0
