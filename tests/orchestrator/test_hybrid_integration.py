"""Tests for HybridStrategy integration in BotOrchestrator."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.orchestrator.bot_orchestrator import BotOrchestrator
from bot.strategies.hybrid.hybrid_config import HybridConfig, HybridMode
from bot.strategies.hybrid.hybrid_strategy import HybridStrategy


from bot.core.trading_core import HybridCoordinator, TradingCore, TradingCoreConfig


def _make_orchestrator_stub(
    *,
    has_grid: bool = True,
    has_dca: bool = True,
    has_hybrid: bool = True,
    strategy: str = "hybrid",
    adx_dca_threshold: float = 25.0,
) -> BotOrchestrator:
    """Create BotOrchestrator stub with minimal attributes for hybrid tests."""
    orch = object.__new__(BotOrchestrator)
    orch.config = MagicMock()
    orch.config.name = "test-bot"
    orch.config.strategy = strategy

    orch.grid_engine = MagicMock() if has_grid else None
    orch.dca_engine = MagicMock() if has_dca else None
    orch.current_price = Decimal("50000")
    orch._current_regime = None
    orch._active_strategies = {"grid", "dca"}
    orch.redis_client = None

    if has_hybrid and has_grid and has_dca:
        orch.hybrid_strategy = HybridStrategy(
            config=HybridConfig(),
            grid_risk_manager=MagicMock(),
            dca_engine=None,
        )
    else:
        orch.hybrid_strategy = None

    # TradingCore with configurable ADX threshold
    orch._trading_core = TradingCore.from_config(TradingCoreConfig())
    orch._trading_core = TradingCore(
        config=TradingCoreConfig(),
        hybrid_coordinator=HybridCoordinator(adx_dca_threshold=adx_dca_threshold),
    )

    # Mock async methods
    orch._process_grid_orders = AsyncMock()
    orch._process_dca_logic = AsyncMock()
    orch._publish_event = AsyncMock()
    return orch


class TestHybridStrategyInstantiation:
    """Verify hybrid_strategy is set for hybrid configs and None otherwise."""

    def test_hybrid_config_creates_strategy(self):
        orch = _make_orchestrator_stub(strategy="hybrid")
        assert orch.hybrid_strategy is not None
        assert isinstance(orch.hybrid_strategy, HybridStrategy)

    def test_non_hybrid_config_no_strategy(self):
        orch = _make_orchestrator_stub(strategy="grid", has_hybrid=False)
        assert orch.hybrid_strategy is None

    def test_missing_dca_engine_no_strategy(self):
        orch = _make_orchestrator_stub(has_dca=False, has_hybrid=False)
        assert orch.hybrid_strategy is None

    def test_missing_grid_engine_no_strategy(self):
        orch = _make_orchestrator_stub(has_grid=False, has_hybrid=False)
        assert orch.hybrid_strategy is None


class TestHybridModeRouting:
    """Verify _process_hybrid_logic routes based on ADX via HybridCoordinator."""

    @pytest.mark.asyncio
    async def test_no_adx_runs_grid_only(self):
        """No regime data → no ADX → HybridCoordinator defaults to GRID_ONLY."""
        orch = _make_orchestrator_stub()
        orch._current_regime = None  # no ADX available

        await orch._process_hybrid_logic()

        orch._process_grid_orders.assert_awaited_once()
        orch._process_dca_logic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_low_adx_runs_grid_only(self):
        """ADX ≤ threshold → GRID_ONLY."""
        orch = _make_orchestrator_stub(adx_dca_threshold=25.0)
        regime = MagicMock()
        regime.adx = 20.0
        orch._current_regime = regime

        await orch._process_hybrid_logic()

        orch._process_grid_orders.assert_awaited_once()
        orch._process_dca_logic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_high_adx_runs_dca_only(self):
        """ADX > threshold → DCA_ACTIVE."""
        orch = _make_orchestrator_stub(adx_dca_threshold=25.0)
        regime = MagicMock()
        regime.adx = 35.0
        orch._current_regime = regime

        await orch._process_hybrid_logic()

        orch._process_dca_logic.assert_awaited_once()
        orch._process_grid_orders.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_both_active_when_allow_both(self):
        """With allow_both=True and ADX in tolerance band, both strategies run."""
        orch = _make_orchestrator_stub(adx_dca_threshold=25.0)
        # Replace coordinator with allow_both=True
        orch._trading_core = TradingCore(
            config=TradingCoreConfig(),
            hybrid_coordinator=HybridCoordinator(
                adx_dca_threshold=25.0, allow_both=True, adx_tolerance=5.0
            ),
        )
        regime = MagicMock()
        regime.adx = 27.0  # in [20, 30] tolerance band
        orch._current_regime = regime

        await orch._process_hybrid_logic()

        orch._process_grid_orders.assert_awaited_once()
        orch._process_dca_logic.assert_awaited_once()


class TestHybridFallback:
    """Verify graceful behavior when components fail or are absent."""

    @pytest.mark.asyncio
    async def test_hybrid_strategy_error_does_not_block_routing(self):
        """HybridStrategy.evaluate() error is logged as warning — coordinator still routes."""
        orch = _make_orchestrator_stub()
        orch.hybrid_strategy.evaluate = MagicMock(side_effect=RuntimeError("boom"))
        # No ADX → coordinator returns grid_only → grid should run
        orch._current_regime = None

        await orch._process_hybrid_logic()

        # Grid should still run (coordinator is not affected by HybridStrategy error)
        orch._process_grid_orders.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_current_price_returns_early(self):
        orch = _make_orchestrator_stub()
        orch.current_price = None

        await orch._process_hybrid_logic()

        orch._process_grid_orders.assert_not_awaited()
        orch._process_dca_logic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_hybrid_strategy_coordinator_still_routes(self):
        """Without HybridStrategy, coordinator routes via ADX — no early return."""
        orch = _make_orchestrator_stub(has_hybrid=False)
        orch._current_regime = None  # no ADX → grid_only

        await orch._process_hybrid_logic()

        # Coordinator (not HybridStrategy) drives routing — grid runs
        orch._process_grid_orders.assert_awaited_once()
        orch._process_dca_logic.assert_not_awaited()


class TestHybridBackwardCompat:
    """Verify non-hybrid bots are unaffected."""

    @pytest.mark.asyncio
    async def test_grid_only_bot_no_hybrid(self):
        orch = _make_orchestrator_stub(strategy="grid", has_dca=False, has_hybrid=False)
        orch._active_strategies = {"grid"}
        assert orch.hybrid_strategy is None
        # Grid-only would go through the else branch in _main_loop

    @pytest.mark.asyncio
    async def test_dca_only_bot_no_hybrid(self):
        orch = _make_orchestrator_stub(strategy="dca", has_grid=False, has_hybrid=False)
        orch._active_strategies = {"dca"}
        assert orch.hybrid_strategy is None


class TestHybridMissingRegime:
    """Verify missing regime data doesn't crash."""

    @pytest.mark.asyncio
    async def test_no_regime_data_evaluates_safely(self):
        orch = _make_orchestrator_stub()
        orch._current_regime = None

        # Should not raise
        await orch._process_hybrid_logic()

        # No ADX → coordinator returns GRID_ONLY
        orch._process_grid_orders.assert_awaited_once()
        orch._process_dca_logic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_regime_with_high_adx_routes_to_dca(self):
        """High ADX from regime → coordinator routes to DCA."""
        orch = _make_orchestrator_stub(adx_dca_threshold=25.0)
        regime = MagicMock()
        regime.adx = 35.0
        orch._current_regime = regime

        await orch._process_hybrid_logic()

        orch._process_dca_logic.assert_awaited_once()
        orch._process_grid_orders.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_regime_with_adx_passes_to_coordinator(self):
        """ADX=35 > threshold=25 → coordinator routes to DCA."""
        orch = _make_orchestrator_stub(adx_dca_threshold=25.0)
        regime = MagicMock()
        regime.adx = 35.0
        orch._current_regime = regime

        await orch._process_hybrid_logic()

        orch._process_dca_logic.assert_awaited_once()
        orch._process_grid_orders.assert_not_awaited()
