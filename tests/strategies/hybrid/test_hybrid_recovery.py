"""
Tests for HybridStrategy recovery mode integration.

Covers:
- Transition to RECOVERY_ACTIVE mode
- _evaluate_recovery_mode delegates to RecoveryCoordinator
- Recovery → GRID_ONLY on TP hit
- Recovery → GRID_ONLY on timeout
- Statistics include recovery count
"""

from decimal import Decimal

import pytest

from bot.strategies.dca.dca_signal_generator import MarketState
from bot.strategies.hybrid.hybrid_config import HybridConfig, HybridMode
from bot.strategies.hybrid.hybrid_strategy import HybridStrategy
from bot.strategies.hybrid.recovery_config import RecoveryConfig
from bot.strategies.hybrid.recovery_coordinator import (
    RecoveryCoordinator,
    UnderwaterPosition,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def recovery_config() -> RecoveryConfig:
    return RecoveryConfig(
        enabled=True,
        tp_target_pct=Decimal("0.01"),
        timeout_bars=6,
        timeout_action="close_all",
    )


@pytest.fixture
def hybrid_config(recovery_config: RecoveryConfig) -> HybridConfig:
    return HybridConfig(recovery=recovery_config)


@pytest.fixture
def coordinator(recovery_config: RecoveryConfig) -> RecoveryCoordinator:
    return RecoveryCoordinator(recovery_config)


@pytest.fixture
def strategy(
    hybrid_config: HybridConfig, coordinator: RecoveryCoordinator
) -> HybridStrategy:
    return HybridStrategy(
        config=hybrid_config,
        recovery_coordinator=coordinator,
    )


@pytest.fixture
def grid_snapshot() -> list[dict]:
    return [
        {"pos_id": "g1", "entry_price": Decimal("100"), "size": Decimal("150")},
        {"pos_id": "g2", "entry_price": Decimal("99"), "size": Decimal("150")},
    ]


def _market_state(price: Decimal) -> MarketState:
    return MarketState(
        current_price=price,
        ema_fast=price,
        ema_slow=price,
    )


# =============================================================================
# enter_recovery
# =============================================================================


class TestEnterRecovery:
    def test_transitions_to_recovery_active(
        self, strategy: HybridStrategy, grid_snapshot: list[dict]
    ):
        assert strategy.mode == HybridMode.GRID_ONLY

        strategy.enter_recovery(
            grid_positions=grid_snapshot,
            current_price=Decimal("94"),
            current_bar=0,
            smc_support=Decimal("88"),
        )

        assert strategy.mode == HybridMode.RECOVERY_ACTIVE
        assert strategy.recovery_state is not None

    def test_recovery_count_incremented(
        self, strategy: HybridStrategy, grid_snapshot: list[dict]
    ):
        strategy.enter_recovery(grid_snapshot, Decimal("94"), 0)
        stats = strategy.get_statistics()
        assert stats["recovery_count"] == 1


# =============================================================================
# evaluate in RECOVERY_ACTIVE
# =============================================================================


class TestEvaluateRecovery:
    def test_tp_hit_returns_to_grid(
        self, strategy: HybridStrategy, grid_snapshot: list[dict]
    ):
        strategy.enter_recovery(grid_snapshot, Decimal("94"), 0)
        assert strategy.mode == HybridMode.RECOVERY_ACTIVE

        # TP = blended_avg * 1.01 = 99.5 * 1.01 ≈ 100.495
        ms = _market_state(Decimal("101"))
        action = strategy.evaluate(ms)

        assert action.transition_triggered is True
        assert action.mode == HybridMode.GRID_ONLY
        assert strategy.mode == HybridMode.GRID_ONLY
        assert strategy.recovery_state is None

    def test_timeout_returns_to_grid(
        self, strategy: HybridStrategy, grid_snapshot: list[dict]
    ):
        strategy.enter_recovery(grid_snapshot, Decimal("94"), 0)

        # 6 баров timeout
        for _ in range(5):
            ms = _market_state(Decimal("93"))
            action = strategy.evaluate(ms)
            assert strategy.mode == HybridMode.RECOVERY_ACTIVE

        # 6-й бар → timeout
        ms = _market_state(Decimal("93"))
        action = strategy.evaluate(ms)
        assert action.transition_triggered is True
        assert strategy.mode == HybridMode.GRID_ONLY

    def test_dca_signals_in_warnings(
        self, strategy: HybridStrategy, grid_snapshot: list[dict]
    ):
        strategy.enter_recovery(
            grid_snapshot, Decimal("94"), 0, smc_support=Decimal("85")
        )
        # Цена падает — DCA уровни пробиты
        ms = _market_state(Decimal("85"))
        action = strategy.evaluate(ms)
        # DCA signals count should appear in warnings
        has_dca_warning = any("recovery_dca_signals" in w for w in action.warnings)
        assert has_dca_warning


# =============================================================================
# Statistics with recovery
# =============================================================================


class TestRecoveryStatistics:
    def test_includes_coordinator_stats(
        self, strategy: HybridStrategy, grid_snapshot: list[dict]
    ):
        strategy.enter_recovery(grid_snapshot, Decimal("94"), 0)
        # TP hit
        strategy.evaluate(_market_state(Decimal("101")))
        stats = strategy.get_statistics()
        assert "recovery" in stats
        assert stats["recovery"]["total_recoveries"] == 1
        assert stats["recovery"]["successful_recoveries"] == 1
