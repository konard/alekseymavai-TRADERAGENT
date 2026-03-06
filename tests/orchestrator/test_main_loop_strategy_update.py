"""Tests for throttled _update_active_strategies in _main_loop.

Verifies that:
1. Regime change actually updates _active_strategies when the interval has elapsed.
2. Throttle blocks updates when the interval has NOT yet elapsed.
3. The _last_active_strategies_update_at timestamp advances on each update.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot.orchestrator.bot_orchestrator import BotOrchestrator
from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_regime(
    regime: MarketRegime,
    recommended: RecommendedStrategy,
    adx: float = 20.0,
) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=regime,
        confidence=0.8,
        recommended_strategy=recommended,
        confluence_score=0.5,
        trend_strength=0.0,
        volatility_percentile=50.0,
        ema_divergence_pct=1.0,
        atr_pct=1.0,
        rsi=50.0,
        adx=adx,
        bb_width_pct=3.0,
        volume_ratio=1.0,
        regime_duration_seconds=120,
        previous_regime=None,
        timestamp=datetime.now(timezone.utc),
        analysis_details={},
    )


def _make_stub(
    *,
    cooldown: float = 0.0,
    regime_check_interval: float = 60.0,
) -> BotOrchestrator:
    """Minimal BotOrchestrator stub for _update_active_strategies tests."""
    orch = object.__new__(BotOrchestrator)
    orch._current_regime = None
    orch._active_strategies = set()
    orch._last_strategy_switch_at = 0.0
    orch._strategy_switch_cooldown = cooldown
    orch._strategy_locked = False
    orch._locked_strategies = None
    # Throttle attributes — match production default (-inf ensures first tick always runs)
    orch._last_active_strategies_update_at = -float("inf")
    orch._regime_check_interval = regime_check_interval
    # Phase-0 attributes
    orch._last_regime_update_at = 1.0       # non-zero → skip eager fetch
    orch._regime_stale_threshold = 2.0 * regime_check_interval
    orch.detect_market_regime = AsyncMock(return_value=None)
    # Mocked async helpers
    orch._publish_event = AsyncMock()
    orch._graceful_transition = AsyncMock()
    # redis_client needed by _publish_event_sync
    orch.redis_client = None
    orch.config = type("C", (), {"name": "test_bot"})()
    return orch


async def _run_main_loop_throttle(orch: BotOrchestrator) -> None:
    """Simulate what _main_loop does for the strategy-update throttle check."""
    now = time.monotonic()
    if now - orch._last_active_strategies_update_at >= orch._regime_check_interval:
        await orch._update_active_strategies()
        orch._last_active_strategies_update_at = now


# ---------------------------------------------------------------------------
# Throttle behaviour
# ---------------------------------------------------------------------------


class TestMainLoopThrottle:
    """Verify _update_active_strategies call rate is gated by _regime_check_interval."""

    @pytest.mark.asyncio
    async def test_update_called_on_first_tick(self):
        """_last_active_strategies_update_at == -inf → immediate call on first tick."""
        orch = _make_stub(regime_check_interval=3600.0)
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        orch._last_active_strategies_update_at = -float("inf")  # never updated

        await _run_main_loop_throttle(orch)

        # After first tick, strategies are set and timestamp advances
        assert orch._active_strategies  # non-empty
        assert orch._last_active_strategies_update_at > 0.0

    @pytest.mark.asyncio
    async def test_update_blocked_within_interval(self):
        """When interval has NOT elapsed, _update_active_strategies must not run."""
        orch = _make_stub(regime_check_interval=3600.0)
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        # Pretend update ran just now
        orch._active_strategies = {"grid"}
        recent = time.monotonic()
        orch._last_active_strategies_update_at = recent

        # Change regime — but throttle should prevent update
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)

        await _run_main_loop_throttle(orch)

        # Strategies must remain unchanged (throttle blocked the update)
        assert orch._active_strategies == {"grid"}
        # Timestamp must not advance
        assert orch._last_active_strategies_update_at == recent

    @pytest.mark.asyncio
    async def test_timestamp_advances_after_update(self):
        """After a successful update _last_active_strategies_update_at is refreshed."""
        orch = _make_stub(regime_check_interval=60.0)
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        before = time.monotonic()
        orch._last_active_strategies_update_at = 0.0

        await _run_main_loop_throttle(orch)

        assert orch._last_active_strategies_update_at >= before


# ---------------------------------------------------------------------------
# Regime change → strategy change (end-to-end)
# ---------------------------------------------------------------------------


class TestRegimeChangeCausesStrategyChange:
    """Verify that a regime change propagates to _active_strategies when not throttled."""

    @pytest.mark.asyncio
    async def test_tight_range_to_dca_only(self):
        """Switching from TIGHT_RANGE (GRID) to BEAR_TREND (DCA) clears grid, adds dca."""
        orch = _make_stub(cooldown=0.0, regime_check_interval=60.0)
        # First tick: tight range → grid
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()
        assert "grid" in orch._active_strategies
        assert "dca" not in orch._active_strategies

        # Regime changes to bear trend → dca (+ trend_follower via strategy augmentation)
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        orch._last_strategy_switch_at = 0.0  # no cooldown

        await orch._update_active_strategies()

        assert "dca" in orch._active_strategies
        assert "grid" not in orch._active_strategies

    @pytest.mark.asyncio
    async def test_tight_range_to_hybrid(self):
        """Switching from TIGHT_RANGE (GRID) to BULL_TREND (HYBRID) adds both strategies."""
        orch = _make_stub(cooldown=0.0, regime_check_interval=60.0)
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        await orch._update_active_strategies()

        orch._current_regime = _make_regime(
            MarketRegime.BULL_TREND, RecommendedStrategy.HYBRID
        )
        orch._last_strategy_switch_at = 0.0

        await orch._update_active_strategies()

        assert "grid" in orch._active_strategies
        assert "dca" in orch._active_strategies

    @pytest.mark.asyncio
    async def test_no_change_when_throttled(self):
        """Full interval throttle scenario: even if regime changed, strategies stay."""
        orch = _make_stub(cooldown=0.0, regime_check_interval=3600.0)
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        orch._active_strategies = {"grid"}
        # Mark update as just happened
        orch._last_active_strategies_update_at = time.monotonic()

        # Regime changes
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)

        # _run_main_loop_throttle simulates _main_loop logic
        await _run_main_loop_throttle(orch)

        # Must not change — throttle is still active
        assert orch._active_strategies == {"grid"}

    @pytest.mark.asyncio
    async def test_update_after_interval_expired(self):
        """After interval expires, strategies follow the new regime."""
        orch = _make_stub(cooldown=0.0, regime_check_interval=60.0)
        orch._current_regime = _make_regime(MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID)
        orch._active_strategies = {"grid"}
        # Pretend last update was a long time ago (beyond interval)
        orch._last_active_strategies_update_at = time.monotonic() - 120.0

        # New regime
        orch._current_regime = _make_regime(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        orch._last_strategy_switch_at = 0.0

        await _run_main_loop_throttle(orch)

        # Strategies must have updated
        assert "dca" in orch._active_strategies
        assert "grid" not in orch._active_strategies
