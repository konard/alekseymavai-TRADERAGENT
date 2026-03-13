"""
Integration tests for recovery in backtest engine.

Scenario: price 100 → Grid [95-105] → drops to 94 → DCA cascade → bounce → recovery TP.
"""

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from bot.strategies.grid_adapter import GridAdapter
from bot.strategies.hybrid.recovery_config import RecoveryConfig
from bot.strategies.hybrid.recovery_coordinator import (
    RecoveryCoordinator,
    RecoveryPhase,
    UnderwaterPosition,
)


class TestRecoveryIntegrationScenario:
    """Синтетический сценарий: цена 100 → падение до 94 → DCA cascade → отскок → TP."""

    def test_full_recovery_flow(self):
        """Тестируем полный цикл: Grid → recovery trigger → DCA → TP → resume."""
        # Setup
        config = RecoveryConfig(
            enabled=True,
            tp_target_pct=Decimal("0.01"),
            max_dca_orders=3,
            dca_step_pct=Decimal("0.02"),
            timeout_bars=20,
            cooldown_after_recovery_bars=2,
        )
        coordinator = RecoveryCoordinator(config)

        adapter = GridAdapter(
            symbol="ETH/USDT",
            num_levels=6,
            amount_per_grid=Decimal("100"),
            grid_range_pct=Decimal("0.05"),
        )
        adapter.set_recovery_enabled(True)

        # Инициализируем Grid
        prices = [100.0] * 30
        df = pd.DataFrame({
            "open": prices,
            "high": [p * 1.005 for p in prices],
            "low": [p * 0.995 for p in prices],
            "close": prices,
            "volume": [1000.0] * 30,
        })
        adapter.analyze_market(df)

        # Ставим несколько позиций вручную (имитируем что Grid их открыл)
        adapter._positions["g1"] = {
            "direction": "long",
            "entry_price": Decimal("100"),
            "stop_loss": adapter._grid_lower_price,
            "take_profit": Decimal("101.2"),
            "size": Decimal("100"),
            "entry_time": None,
            "current_price": Decimal("100"),
        }
        adapter._positions["g2"] = {
            "direction": "long",
            "entry_price": Decimal("99"),
            "stop_loss": adapter._grid_lower_price,
            "take_profit": Decimal("100.2"),
            "size": Decimal("100"),
            "entry_time": None,
            "current_price": Decimal("99"),
        }

        # Phase 1: Цена падает ниже grid_lower → recovery triggered
        grid_lower = adapter._grid_lower_price
        low_price = grid_lower - Decimal("1")
        exits = adapter.update_positions(low_price, df)
        assert len(exits) == 0  # не закрыли — recovery
        assert adapter.recovery_triggered is True

        # Phase 2: coordinator detects trigger
        assert coordinator.should_trigger(grid_lower, low_price) is True

        # Phase 3: enter recovery
        snapshot = adapter.get_underwater_snapshot()
        underwater = [
            UnderwaterPosition(p["pos_id"], p["entry_price"], p["size"])
            for p in snapshot
        ]
        state = coordinator.enter_recovery(
            grid_positions=underwater,
            current_price=low_price,
            current_bar=0,
            smc_support=Decimal("88"),
            base_order_size=Decimal("100"),
        )
        adapter.clear_recovery_trigger()

        assert state.phase == RecoveryPhase.DCA_ACTIVE
        assert len(state.dca_levels) > 0

        # Phase 4: цена падает дальше → DCA signals генерируются
        all_dca_fills = []
        for bar_idx in range(5):
            drop_price = low_price - Decimal(str(bar_idx * 0.5))
            action = coordinator.on_price_update(drop_price)
            for sig in action.dca_signals:
                fill = UnderwaterPosition(
                    pos_id=f"dca_{bar_idx}",
                    entry_price=drop_price,
                    size=Decimal("100"),
                )
                all_dca_fills.append(fill)
            if action.should_close_all:
                break

        # Register all fills at once
        if all_dca_fills:
            coordinator.on_price_update(low_price, new_fills=all_dca_fills)

        # Phase 5: цена отскакивает → TP hit
        if coordinator.is_active:
            tp = coordinator.state.tp_target
            action = coordinator.on_price_update(tp + Decimal("1"))
            assert action.should_close_all is True
            assert action.new_grid_range is not None

            # Phase 6: resume Grid
            new_lo, new_hi = action.new_grid_range
            adapter.resume(new_lo, new_hi)

            assert not adapter._paused
            assert adapter._grid_engine is not None

        # Verify statistics
        stats = coordinator.get_statistics()
        assert stats["total_recoveries"] == 1
        assert stats["successful_recoveries"] == 1

    def test_recovery_timeout_then_cooldown(self):
        """Recovery таймаутит → cooldown блокирует повторный вход."""
        config = RecoveryConfig(
            enabled=True,
            timeout_bars=5,
            timeout_action="close_all",
            cooldown_after_recovery_bars=3,
        )
        coordinator = RecoveryCoordinator(config)

        positions = [
            UnderwaterPosition("g1", Decimal("100"), Decimal("100")),
        ]

        # Enter recovery
        coordinator.enter_recovery(positions, Decimal("94"), 0)

        # Timeout
        for _ in range(5):
            coordinator.on_price_update(Decimal("93"))

        assert not coordinator.is_active

        # Cooldown: should_trigger returns False для 3 баров
        for _ in range(3):
            assert coordinator.should_trigger(Decimal("95"), Decimal("94")) is False

        # После cooldown: можно входить снова
        assert coordinator.should_trigger(Decimal("95"), Decimal("94")) is True

    def test_recovery_disabled_falls_through_to_stop_loss(self):
        """Без recovery → обычный STOP_LOSS (регрессионный тест)."""
        adapter = GridAdapter(
            symbol="BTC/USDT",
            num_levels=6,
            amount_per_grid=Decimal("150"),
            grid_range_pct=Decimal("0.05"),
        )
        # recovery НЕ включён (по умолчанию)

        prices = [100.0] * 30
        df = pd.DataFrame({
            "open": prices,
            "high": [p * 1.005 for p in prices],
            "low": [p * 0.995 for p in prices],
            "close": prices,
            "volume": [1000.0] * 30,
        })
        adapter.analyze_market(df)

        adapter._positions["g1"] = {
            "direction": "long",
            "entry_price": Decimal("100"),
            "stop_loss": adapter._grid_lower_price,
            "take_profit": Decimal("102"),
            "size": Decimal("150"),
            "entry_time": None,
            "current_price": Decimal("100"),
        }

        low = adapter._grid_lower_price - Decimal("1")
        exits = adapter.update_positions(low, df)
        assert len(exits) == 1
        assert exits[0][1].value == "stop_loss"
