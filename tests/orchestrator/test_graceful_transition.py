"""Tests for graceful strategy transition (#292).

Verifies that when regime changes deactivate strategies:
- Open orders are cancelled before switch
- Positions are optionally closed (configurable)
- Transition events are published
- No fund loss scenarios
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.orchestrator.bot_orchestrator import BotOrchestrator
from bot.orchestrator.events import EventType
from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)


def _make_regime(
    regime: MarketRegime,
    recommended: RecommendedStrategy,
    confidence: float = 0.8,
) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=regime,
        confidence=confidence,
        recommended_strategy=recommended,
        confluence_score=0.5,
        trend_strength=0.0,
        volatility_percentile=50.0,
        ema_divergence_pct=1.0,
        atr_pct=1.0,
        rsi=50.0,
        adx=30.0,
        bb_width_pct=3.0,
        volume_ratio=1.0,
        regime_duration_seconds=120,
        previous_regime=None,
        timestamp=datetime.now(timezone.utc),
        analysis_details={},
    )


def _make_orchestrator_with_exchange(
    *,
    dry_run: bool = False,
    close_positions_on_switch: bool = False,
    cooldown: float = 0.0,
) -> BotOrchestrator:
    """Create a BotOrchestrator stub with mocked exchange and config."""
    orch = object.__new__(BotOrchestrator)
    orch._current_regime = None
    orch._active_strategies = set()
    orch._last_strategy_switch_at = 0.0
    orch._strategy_switch_cooldown = cooldown
    orch._strategy_locked = False
    orch._locked_strategies = None
    # Phase-0 additions
    orch._last_regime_update_at = 1.0      # non-zero: skip eager fetch in tests
    orch._regime_stale_threshold = 120.0
    orch.detect_market_regime = AsyncMock(return_value=None)

    # Config mock
    orch.config = SimpleNamespace(
        dry_run=dry_run,
        symbol="BTC/USDT",
        close_positions_on_switch=close_positions_on_switch,
    )

    # Exchange mock
    orch.exchange = AsyncMock()
    orch.exchange.cancel_all_orders = AsyncMock(return_value=None)
    orch.exchange.create_order = AsyncMock(return_value={"id": "test-order-123"})

    # Strategy engine mocks
    orch.grid_engine = None
    orch.dca_engine = None
    orch.trend_follower_strategy = None
    orch.smc_strategy = None
    orch.current_price = Decimal("50000")

    # Redis event publishing mock
    orch.redis_client = None
    orch._publish_event = AsyncMock()

    return orch


class TestGracefulTransitionOrderCancellation:
    """Verify open orders are cancelled when strategies are deactivated."""

    @pytest.mark.asyncio
    async def test_grid_orders_cancelled_on_deactivation(self) -> None:
        """When grid is deactivated, cancel_all_orders is called."""
        orch = _make_orchestrator_with_exchange()
        orch.grid_engine = MagicMock()  # grid engine exists

        # Start with grid active
        orch._active_strategies = {"grid"}
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        orch._last_strategy_switch_at = 0.0  # allow switch

        # Switch to DCA (deactivates grid)
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        await orch._update_active_strategies()

        # Grid orders should be cancelled
        orch.exchange.cancel_all_orders.assert_awaited_once_with("BTC/USDT")

    @pytest.mark.asyncio
    async def test_no_cancel_in_dry_run(self) -> None:
        """In dry_run mode, no exchange calls are made."""
        orch = _make_orchestrator_with_exchange(dry_run=True)
        orch.grid_engine = MagicMock()

        orch._active_strategies = {"grid"}
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        orch._last_strategy_switch_at = 0.0

        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        await orch._update_active_strategies()

        orch.exchange.cancel_all_orders.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_cancel_when_grid_engine_absent(self) -> None:
        """If grid_engine is None, cancel is not called even when grid deactivated."""
        orch = _make_orchestrator_with_exchange()
        orch.grid_engine = None  # no grid engine

        orch._active_strategies = {"grid"}
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        orch._last_strategy_switch_at = 0.0

        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        await orch._update_active_strategies()

        orch.exchange.cancel_all_orders.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_error_does_not_block_transition(self) -> None:
        """If cancel_all_orders fails, transition still completes."""
        orch = _make_orchestrator_with_exchange()
        orch.grid_engine = MagicMock()
        orch.exchange.cancel_all_orders = AsyncMock(side_effect=Exception("Exchange error"))

        orch._active_strategies = {"grid"}
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        orch._last_strategy_switch_at = 0.0

        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        await orch._update_active_strategies()

        # Transition should complete despite error
        assert "dca" in orch._active_strategies
        assert "grid" not in orch._active_strategies


class TestGracefulTransitionPositionHandling:
    """Verify configurable position handling during transition."""

    @pytest.mark.asyncio
    async def test_positions_held_by_default(self) -> None:
        """With close_positions_on_switch=False, no market close orders placed."""
        orch = _make_orchestrator_with_exchange(close_positions_on_switch=False)
        orch.dca_engine = MagicMock()
        orch.dca_engine.position = MagicMock()  # has open position
        orch.dca_engine.position.amount = Decimal("500")

        orch._active_strategies = {"dca", "trend_follower", "smc"}
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        orch._last_strategy_switch_at = 0.0

        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()

        # No close orders should be placed (positions are held)
        orch.exchange.create_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dca_position_closed_when_configured(self) -> None:
        """With close_positions_on_switch=True, DCA position is closed."""
        orch = _make_orchestrator_with_exchange(close_positions_on_switch=True)
        orch.dca_engine = MagicMock()
        orch.dca_engine.position = MagicMock()
        orch.dca_engine.position.amount = Decimal("500")

        orch._active_strategies = {"dca", "trend_follower", "smc"}
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        orch._last_strategy_switch_at = 0.0

        # Patch _close_dca_position to verify it's called
        orch._close_dca_position = AsyncMock()

        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()

        orch._close_dca_position.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_position_close_in_dry_run(self) -> None:
        """In dry_run mode, positions are not closed even if configured."""
        orch = _make_orchestrator_with_exchange(dry_run=True, close_positions_on_switch=True)
        orch.dca_engine = MagicMock()
        orch.dca_engine.position = MagicMock()
        orch.dca_engine.position.amount = Decimal("500")

        orch._active_strategies = {"dca", "trend_follower", "smc"}
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        orch._last_strategy_switch_at = 0.0

        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()

        orch.exchange.create_order.assert_not_awaited()


class TestGracefulTransitionEvents:
    """Verify transition events are published."""

    @pytest.mark.asyncio
    async def test_transition_events_published(self) -> None:
        """STRATEGY_TRANSITION_STARTED and _COMPLETED events are published."""
        orch = _make_orchestrator_with_exchange()
        orch.grid_engine = MagicMock()

        orch._active_strategies = {"grid"}
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        orch._last_strategy_switch_at = 0.0

        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        await orch._update_active_strategies()

        # Check events
        event_types = [call.args[0] for call in orch._publish_event.call_args_list]
        assert EventType.STRATEGY_TRANSITION_STARTED in event_types
        assert EventType.STRATEGY_TRANSITION_COMPLETED in event_types

    @pytest.mark.asyncio
    async def test_transition_event_includes_deactivated_strategies(self) -> None:
        """Transition event payload includes deactivated strategy names."""
        orch = _make_orchestrator_with_exchange()
        orch.grid_engine = MagicMock()

        orch._active_strategies = {"grid"}
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        orch._last_strategy_switch_at = 0.0

        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        await orch._update_active_strategies()

        # Find STARTED event payload
        for call in orch._publish_event.call_args_list:
            if call.args[0] == EventType.STRATEGY_TRANSITION_STARTED:
                payload = call.args[1]
                assert "grid" in payload["deactivated"]
                break
        else:
            pytest.fail("STRATEGY_TRANSITION_STARTED event not found")

    @pytest.mark.asyncio
    async def test_no_transition_when_no_deactivation(self) -> None:
        """When strategies only grow (no deactivation), no transition events."""
        orch = _make_orchestrator_with_exchange()

        # Start with empty set (first regime detection)
        orch._active_strategies = set()
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()

        # No transition events (first-time set, nothing to deactivate)
        event_types = [call.args[0] for call in orch._publish_event.call_args_list]
        assert EventType.STRATEGY_TRANSITION_STARTED not in event_types


class TestGracefulTransitionIntegration:
    """Integration-style tests: regime change → orders cleaned up."""

    @pytest.mark.asyncio
    async def test_full_regime_switch_grid_to_dca(self) -> None:
        """Full flow: grid active → regime change to bear → grid orders cancelled → DCA active."""
        orch = _make_orchestrator_with_exchange()
        orch.grid_engine = MagicMock()
        orch.dca_engine = MagicMock()

        # Step 1: Initial regime → grid
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()
        assert orch._active_strategies == {"grid"}

        # Step 2: Regime changes to bear trend → DCA
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        await orch._update_active_strategies()

        # Grid orders cancelled
        orch.exchange.cancel_all_orders.assert_awaited_once_with("BTC/USDT")
        # DCA is now active, grid is not
        assert "dca" in orch._active_strategies
        assert "grid" not in orch._active_strategies

    @pytest.mark.asyncio
    async def test_full_regime_switch_dca_to_hold(self) -> None:
        """Regime → HOLD: all strategies deactivated, orders cancelled."""
        orch = _make_orchestrator_with_exchange()
        orch.grid_engine = MagicMock()
        orch.dca_engine = MagicMock()

        # Start with DCA + trend_follower + smc active
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        await orch._update_active_strategies()
        assert "dca" in orch._active_strategies

        # Regime changes to HOLD
        orch._current_regime = _make_regime(
            MarketRegime.QUIET_TRANSITION, RecommendedStrategy.HOLD
        )
        await orch._update_active_strategies()

        # All strategies deactivated
        assert orch._active_strategies == set()

    @pytest.mark.asyncio
    async def test_no_fund_loss_on_cancel_failure(self) -> None:
        """If order cancellation fails, strategies still switch and no panic."""
        orch = _make_orchestrator_with_exchange()
        orch.grid_engine = MagicMock()
        orch.exchange.cancel_all_orders = AsyncMock(
            side_effect=Exception("Network timeout")
        )

        # Grid active
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()
        assert "grid" in orch._active_strategies

        # Switch to DCA — cancel fails but transition completes
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        await orch._update_active_strategies()

        assert "dca" in orch._active_strategies
        # Transition events still published
        event_types = [call.args[0] for call in orch._publish_event.call_args_list]
        assert EventType.STRATEGY_TRANSITION_COMPLETED in event_types

    @pytest.mark.asyncio
    async def test_cooldown_prevents_transition_cleanup(self) -> None:
        """When cooldown blocks a switch, no transition cleanup occurs."""
        orch = _make_orchestrator_with_exchange(cooldown=600.0)
        orch.grid_engine = MagicMock()

        # Grid active
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()

        # Try to switch — blocked by cooldown
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        await orch._update_active_strategies()

        # No orders cancelled (switch was blocked)
        orch.exchange.cancel_all_orders.assert_not_awaited()
        assert "grid" in orch._active_strategies

    @pytest.mark.asyncio
    async def test_same_strategy_no_transition(self) -> None:
        """When regime changes but strategies don't, no transition occurs."""
        orch = _make_orchestrator_with_exchange()
        orch.grid_engine = MagicMock()

        # Tight range → grid
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()

        # Wide range → still grid (no change)
        orch._current_regime = _make_regime(MarketRegime.WIDE_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()

        # No transition occurred
        orch.exchange.cancel_all_orders.assert_not_awaited()
        event_types = [call.args[0] for call in orch._publish_event.call_args_list]
        assert EventType.STRATEGY_TRANSITION_STARTED not in event_types
