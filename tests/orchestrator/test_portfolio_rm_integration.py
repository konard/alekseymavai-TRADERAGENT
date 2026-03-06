"""Tests for PortfolioRiskManager integration in BotOrchestrator.

Covers:
- _portfolio_rm_check helper
- DCA trigger blocked / allowed / confirmed / released
- Grid orders blocked when portfolio halted
- Single-bot backward compat (portfolio_rm=None → no change)
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.core.portfolio_risk_manager import (
    PortfolioRiskManager,
    RiskCheckResult,
    RiskCheckStatus,
)
from bot.orchestrator.bot_orchestrator import BotOrchestrator, BotState

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_portfolio_rm(
    *,
    approved: bool = True,
    halted: bool = False,
) -> PortfolioRiskManager:
    prm = MagicMock(spec=PortfolioRiskManager)
    prm.is_portfolio_halted.return_value = halted
    prm.check_allocation.return_value = RiskCheckResult(
        status=RiskCheckStatus.APPROVED if approved else RiskCheckStatus.REJECTED_EXPOSURE,
        approved=approved,
        reason="" if approved else "pool exhausted",
        max_allowed=Decimal("1000") if approved else Decimal("0"),
    )
    prm.confirm_allocation = MagicMock()
    prm.release_allocation = MagicMock()
    return prm


def _make_orchestrator(
    *,
    portfolio_rm: PortfolioRiskManager | None = None,
    state: BotState = BotState.RUNNING,
    dry_run: bool = False,
) -> BotOrchestrator:
    """Minimal BotOrchestrator stub for portfolio-RM tests."""
    orch = object.__new__(BotOrchestrator)

    # Config
    orch.config = MagicMock()
    orch.config.name = "test_bot"
    orch.config.symbol = "BTC/USDT"
    orch.config.dry_run = dry_run

    # State
    orch.state = state
    orch._cached_balance = Decimal("5000")

    # Portfolio RM
    orch._portfolio_rm = portfolio_rm

    # Per-bot risk manager (optional)
    orch.risk_manager = None

    # DCA engine stub
    orch.dca_engine = MagicMock()
    orch.dca_engine.amount_per_step = Decimal("200")
    orch.dca_engine.position = None
    orch.current_price = Decimal("50000")

    # Grid engine stub
    orch.grid_engine = MagicMock()

    # Async helpers
    orch._place_dca_order = AsyncMock()
    orch._close_dca_position = AsyncMock()
    orch._place_single_order = AsyncMock()
    orch._publish_event = AsyncMock()
    orch._get_available_balance = AsyncMock(return_value=Decimal("5000"))

    # DCA engine methods
    orch.dca_engine.update_price.return_value = {
        "dca_triggered": False,
        "tp_triggered": False,
    }
    orch.dca_engine.execute_dca_step.return_value = True

    return orch


# ---------------------------------------------------------------------------
# _portfolio_rm_check
# ---------------------------------------------------------------------------


class TestPortfolioRmCheck:
    def test_no_portfolio_rm_always_true(self):
        orch = _make_orchestrator(portfolio_rm=None)
        assert orch._portfolio_rm_check(Decimal("9999")) is True

    def test_approved_returns_true(self):
        prm = _make_portfolio_rm(approved=True)
        orch = _make_orchestrator(portfolio_rm=prm)
        assert orch._portfolio_rm_check(Decimal("500")) is True
        prm.check_allocation.assert_called_once_with(
            bot_name="test_bot",
            amount=Decimal("500"),
            balance=Decimal("5000"),
            symbol="BTC/USDT",
        )

    def test_rejected_returns_false(self):
        prm = _make_portfolio_rm(approved=False)
        orch = _make_orchestrator(portfolio_rm=prm)
        assert orch._portfolio_rm_check(Decimal("500")) is False

    def test_cached_balance_none_passed_through(self):
        """If _cached_balance is None the check still runs (balance=None)."""
        prm = _make_portfolio_rm(approved=True)
        orch = _make_orchestrator(portfolio_rm=prm)
        orch._cached_balance = None
        orch._portfolio_rm_check(Decimal("100"))
        _, kwargs = prm.check_allocation.call_args
        assert kwargs["balance"] is None


# ---------------------------------------------------------------------------
# DCA — blocked by portfolio RM
# ---------------------------------------------------------------------------


class TestDcaPortfolioRmBlocked:
    @pytest.mark.asyncio
    async def test_dca_blocked_when_rm_rejects(self):
        prm = _make_portfolio_rm(approved=False)
        orch = _make_orchestrator(portfolio_rm=prm)
        orch.dca_engine.update_price.return_value = {
            "dca_triggered": True,
            "tp_triggered": False,
        }

        await orch._process_dca_logic()

        # No order should be placed
        orch._place_dca_order.assert_not_awaited()
        prm.confirm_allocation.assert_not_called()

    @pytest.mark.asyncio
    async def test_dca_allowed_when_rm_approves(self):
        prm = _make_portfolio_rm(approved=True)
        orch = _make_orchestrator(portfolio_rm=prm)
        orch.dca_engine.update_price.return_value = {
            "dca_triggered": True,
            "tp_triggered": False,
        }

        await orch._process_dca_logic()

        orch._place_dca_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dca_no_rm_behaves_as_before(self):
        """Without portfolio RM, DCA proceeds exactly as before."""
        orch = _make_orchestrator(portfolio_rm=None)
        orch.dca_engine.update_price.return_value = {
            "dca_triggered": True,
            "tp_triggered": False,
        }

        await orch._process_dca_logic()

        orch._place_dca_order.assert_awaited_once()


# ---------------------------------------------------------------------------
# DCA — confirm allocation after order placed
# ---------------------------------------------------------------------------


class TestDcaConfirmAllocation:
    @pytest.mark.asyncio
    async def test_confirm_called_after_successful_order(self):
        prm = _make_portfolio_rm(approved=True)
        orch = _make_orchestrator(portfolio_rm=prm)
        orch.dca_engine.update_price.return_value = {
            "dca_triggered": True,
            "tp_triggered": False,
        }

        await orch._process_dca_logic()

        prm.confirm_allocation.assert_called_once_with(
            "test_bot",
            Decimal("200"),
            symbol="BTC/USDT",
        )

    @pytest.mark.asyncio
    async def test_confirm_not_called_when_order_fails(self):
        prm = _make_portfolio_rm(approved=True)
        orch = _make_orchestrator(portfolio_rm=prm)
        orch._place_dca_order.side_effect = RuntimeError("exchange error")
        orch.dca_engine.update_price.return_value = {
            "dca_triggered": True,
            "tp_triggered": False,
        }

        await orch._process_dca_logic()

        prm.confirm_allocation.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_not_called_in_dry_run(self):
        """In dry_run, no exchange call → no confirm."""
        prm = _make_portfolio_rm(approved=True)
        orch = _make_orchestrator(portfolio_rm=prm, dry_run=True)
        orch.dca_engine.update_price.return_value = {
            "dca_triggered": True,
            "tp_triggered": False,
        }

        await orch._process_dca_logic()

        prm.confirm_allocation.assert_not_called()


# ---------------------------------------------------------------------------
# DCA — release allocation on take profit
# ---------------------------------------------------------------------------


class TestDcaReleaseAllocation:
    @pytest.mark.asyncio
    async def test_release_called_on_take_profit(self):
        prm = _make_portfolio_rm(approved=True)
        orch = _make_orchestrator(portfolio_rm=prm)

        position = MagicMock()
        position.amount = Decimal("600")  # 3 steps × 200
        orch.dca_engine.position = position
        orch.dca_engine.close_position.return_value = Decimal("30")
        orch.dca_engine.update_price.return_value = {
            "dca_triggered": False,
            "tp_triggered": True,
        }

        await orch._process_dca_logic()

        prm.release_allocation.assert_called_once_with(
            "test_bot",
            Decimal("600"),
            symbol="BTC/USDT",
        )

    @pytest.mark.asyncio
    async def test_release_not_called_when_no_rm(self):
        orch = _make_orchestrator(portfolio_rm=None)
        orch.dca_engine.position = MagicMock()
        orch.dca_engine.position.amount = Decimal("600")
        orch.dca_engine.close_position.return_value = Decimal("30")
        orch.dca_engine.update_price.return_value = {
            "dca_triggered": False,
            "tp_triggered": True,
        }

        # Should not raise; no RM present
        await orch._process_dca_logic()

    @pytest.mark.asyncio
    async def test_release_not_called_when_no_position(self):
        """If position is None before tp close, release amount is zero → skipped."""
        prm = _make_portfolio_rm(approved=True)
        orch = _make_orchestrator(portfolio_rm=prm)
        orch.dca_engine.position = None
        orch.dca_engine.close_position.return_value = Decimal("0")
        orch.dca_engine.update_price.return_value = {
            "dca_triggered": False,
            "tp_triggered": True,
        }

        await orch._process_dca_logic()

        prm.release_allocation.assert_not_called()


# ---------------------------------------------------------------------------
# Grid — blocked when portfolio halted
# ---------------------------------------------------------------------------


class TestGridPortfolioRmBlocked:
    @pytest.mark.asyncio
    async def test_grid_orders_blocked_when_portfolio_halted(self):
        prm = _make_portfolio_rm(approved=False)
        orch = _make_orchestrator(portfolio_rm=prm)

        order = MagicMock()
        order.side = "buy"
        order.amount = Decimal("0.01")
        order.price = Decimal("50000")

        await orch._place_grid_orders([order])

        orch._place_single_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_grid_sell_only_orders_skip_rm_check(self):
        """Sell-only rebalance orders have zero buy notional → no RM check."""
        prm = _make_portfolio_rm(approved=True)
        orch = _make_orchestrator(portfolio_rm=prm)

        sell_order = MagicMock()
        sell_order.side = "sell"
        sell_order.amount = Decimal("0.01")
        sell_order.price = Decimal("52000")

        await orch._place_grid_orders([sell_order])

        # RM check skipped (no buy notional)
        prm.check_allocation.assert_not_called()
        orch._place_single_order.assert_awaited_once_with(sell_order)

    @pytest.mark.asyncio
    async def test_grid_orders_placed_when_rm_approves(self):
        prm = _make_portfolio_rm(approved=True)
        orch = _make_orchestrator(portfolio_rm=prm)

        order = MagicMock()
        order.side = "buy"
        order.amount = Decimal("0.01")
        order.price = Decimal("50000")

        await orch._place_grid_orders([order])

        orch._place_single_order.assert_awaited_once_with(order)

    @pytest.mark.asyncio
    async def test_grid_no_rm_places_all_orders(self):
        """Without portfolio RM, grid placement is unchanged."""
        orch = _make_orchestrator(portfolio_rm=None)

        orders = [MagicMock(side="buy", amount=Decimal("0.01"), price=Decimal("50000"))]
        await orch._place_grid_orders(orders)

        orch._place_single_order.assert_awaited_once()
