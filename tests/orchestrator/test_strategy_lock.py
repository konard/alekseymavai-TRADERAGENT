"""Tests for manual strategy lock/unlock in BotOrchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot.orchestrator.bot_orchestrator import BotOrchestrator
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


def _make_orchestrator_stub(cooldown: float = 0.0) -> BotOrchestrator:
    """Create a BotOrchestrator with minimal mocked dependencies."""
    orch = object.__new__(BotOrchestrator)
    orch._current_regime = None
    orch._active_strategies = set()
    orch._last_strategy_switch_at = 0.0
    orch._strategy_switch_cooldown = cooldown
    orch._strategy_locked = False
    orch._locked_strategies = None
    orch._publish_event = AsyncMock()
    orch._graceful_transition = AsyncMock()
    # For _publish_event_sync
    orch.redis_client = None
    orch.config = type("C", (), {"name": "test_bot"})()
    # Phase-0 additions
    orch._last_regime_update_at = 1.0      # non-zero: skip eager fetch in tests
    orch._regime_stale_threshold = 120.0
    orch.detect_market_regime = AsyncMock(return_value=None)
    return orch


class TestStrategyLock:
    """Tests for lock_strategy / unlock_strategy and bypass logic."""

    def test_lock_sets_state(self) -> None:
        orch = _make_orchestrator_stub()
        orch.lock_strategy({"smc"})
        assert orch._strategy_locked is True
        assert orch._locked_strategies == {"smc"}
        assert orch._active_strategies == {"smc"}

    def test_lock_multiple_strategies(self) -> None:
        orch = _make_orchestrator_stub()
        orch.lock_strategy({"grid", "dca"})
        assert orch._locked_strategies == {"grid", "dca"}
        assert orch._active_strategies == {"grid", "dca"}

    def test_unlock_clears_state(self) -> None:
        orch = _make_orchestrator_stub()
        orch.lock_strategy({"smc"})
        orch.unlock_strategy()
        assert orch._strategy_locked is False
        assert orch._locked_strategies is None

    @pytest.mark.asyncio
    async def test_lock_bypasses_auto_switching(self) -> None:
        """When locked, _update_active_strategies should not change strategies."""
        orch = _make_orchestrator_stub()
        # Set a regime that would normally switch to grid
        orch._current_regime = _make_regime(
            MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID
        )
        # Lock to smc
        orch.lock_strategy({"smc"})
        # Run auto-switch — should be bypassed
        await orch._update_active_strategies()
        assert orch._active_strategies == {"smc"}

    @pytest.mark.asyncio
    async def test_lock_enforces_locked_set(self) -> None:
        """If active_strategies somehow diverges, lock re-syncs it."""
        orch = _make_orchestrator_stub()
        orch.lock_strategy({"smc"})
        # Simulate divergence
        orch._active_strategies = {"grid"}
        await orch._update_active_strategies()
        assert orch._active_strategies == {"smc"}

    @pytest.mark.asyncio
    async def test_unlock_restores_auto_switching(self) -> None:
        """After unlock, auto-switching resumes normally."""
        orch = _make_orchestrator_stub()
        orch.lock_strategy({"smc"})
        orch.unlock_strategy()
        # With no regime, all strategies should be active
        orch._current_regime = None
        await orch._update_active_strategies()
        assert orch._active_strategies == {"grid", "dca", "trend_follower", "smc"}

    @pytest.mark.asyncio
    async def test_unlock_allows_regime_switching(self) -> None:
        """After unlock, regime detection drives strategy selection."""
        orch = _make_orchestrator_stub()
        orch.lock_strategy({"smc"})
        orch.unlock_strategy()
        orch._current_regime = _make_regime(
            MarketRegime.TIGHT_RANGE, RecommendedStrategy.GRID
        )
        await orch._update_active_strategies()
        assert orch._active_strategies == {"grid"}


class TestGetStatusLock:
    """Tests for lock info in get_status output."""

    @pytest.mark.asyncio
    async def test_status_shows_locked(self) -> None:
        orch = _make_orchestrator_stub()
        # Provide minimal attrs needed by get_status
        orch.grid_engine = None
        orch.dca_engine = None
        orch.trend_follower_strategy = None
        orch.smc_strategy = None
        orch.risk_manager = None
        orch.current_price = None
        orch.state = type("S", (), {"value": "running"})()
        orch.config.symbol = "BTC/USDT"
        orch.config.strategy = "hybrid"
        orch.config.dry_run = True
        orch.strategy_registry = type("SR", (), {
            "get_registry_status": lambda self: {"total": 0, "active": 0}
        })()
        orch.health_monitor = type("HM", (), {
            "get_health_summary": lambda self: {"status": "healthy"}
        })()

        orch.lock_strategy({"smc", "grid"})
        status = await orch.get_status()

        assert status["strategy_lock"]["locked"] is True
        assert status["strategy_lock"]["strategies"] == ["grid", "smc"]
        assert status["active_strategies"] == ["grid", "smc"]

    @pytest.mark.asyncio
    async def test_status_shows_unlocked(self) -> None:
        orch = _make_orchestrator_stub()
        orch.grid_engine = None
        orch.dca_engine = None
        orch.trend_follower_strategy = None
        orch.smc_strategy = None
        orch.risk_manager = None
        orch.current_price = None
        orch.state = type("S", (), {"value": "running"})()
        orch.config.symbol = "BTC/USDT"
        orch.config.strategy = "hybrid"
        orch.config.dry_run = True
        orch.strategy_registry = type("SR", (), {
            "get_registry_status": lambda self: {"total": 0, "active": 0}
        })()
        orch.health_monitor = type("HM", (), {
            "get_health_summary": lambda self: {"status": "healthy"}
        })()

        status = await orch.get_status()

        assert status["strategy_lock"]["locked"] is False
        assert status["strategy_lock"]["strategies"] is None
