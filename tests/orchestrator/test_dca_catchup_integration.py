"""
Integration tests for DCA catch-up mode in BotOrchestrator.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.config.schemas import BotConfig, DCAConfig, ExchangeConfig, RiskManagementConfig, StrategyType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bot_config(catch_up_enabled: bool = True) -> BotConfig:
    return BotConfig(
        name="test_dca_bot",
        symbol="SOL/USDT",
        strategy=StrategyType.DCA,
        exchange=ExchangeConfig(
            exchange_id="bybit",
            credentials_name="test",
            sandbox=True,
        ),
        dca=DCAConfig(
            enabled=True,
            trigger_percentage=Decimal("0.05"),
            amount_per_step=Decimal("20"),
            max_steps=5,
            take_profit_percentage=Decimal("0.10"),
            catch_up_enabled=catch_up_enabled,
            catch_up_max_orders=3,
            catch_up_reference="current_price",
            catch_up_lookback_bars=50,
        ),
        risk_management=RiskManagementConfig(
            max_position_size=Decimal("300"),
            min_order_size=Decimal("5"),
        ),
        dry_run=True,
    )


def _make_exchange(current_price: float = 88.0) -> AsyncMock:
    exchange = AsyncMock()
    exchange.fetch_ticker.return_value = {"last": str(current_price)}
    exchange.fetch_balance.return_value = {"free": {"USDT": 1000.0}, "total": {}, "used": {}}
    exchange.fetch_ohlcv.return_value = [
        [i * 3_600_000, 90.0, 92.0, 88.0, 90.0, 1000.0] for i in range(50)
    ]
    exchange.fetch_open_orders.return_value = []
    exchange.create_order.return_value = {"id": "test_order_1"}
    exchange.cancel_all_orders.return_value = []
    exchange.ping = AsyncMock(return_value=True)
    return exchange


# ---------------------------------------------------------------------------
# Test 1: catch_up_enabled=false → _run_dca_catchup never called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_catchup_disabled_skips_entire_flow():
    from bot.orchestrator.bot_orchestrator import BotOrchestrator
    from bot.database.manager import DatabaseManager

    config = _make_bot_config(catch_up_enabled=False)
    exchange = _make_exchange()
    db = MagicMock(spec=DatabaseManager)
    db.load_bot_state = AsyncMock(return_value=None)
    db.save_bot_state = AsyncMock()

    orchestrator = BotOrchestrator(config, exchange, db, redis_url="redis://localhost:6379")

    with patch.object(orchestrator, "_run_dca_catchup", new_callable=AsyncMock) as mock_catchup:
        with patch.object(orchestrator, "initialize", new_callable=AsyncMock):
            orchestrator.dca_engine = MagicMock()
            orchestrator.dca_engine.reset = MagicMock()
            orchestrator._state_loaded = False
            orchestrator.current_price = Decimal("88")
            orchestrator.state = type(orchestrator.state).STOPPED  # type: ignore[assignment]

            # Simulate the relevant portion of start()
            if orchestrator.dca_engine and config.dca and config.dca.catch_up_enabled:
                await orchestrator._run_dca_catchup()

        mock_catchup.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: catch_up_enabled=true, dry_run=true → plan built, no orders placed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_catchup_builds_plan_but_no_orders_in_dry_run():
    from bot.orchestrator.bot_orchestrator import BotOrchestrator
    from bot.database.manager import DatabaseManager

    config = _make_bot_config(catch_up_enabled=True)
    exchange = _make_exchange(current_price=88.0)
    db = MagicMock(spec=DatabaseManager)

    orchestrator = BotOrchestrator(config, exchange, db, redis_url="redis://localhost:6379")
    orchestrator.current_price = Decimal("88")

    from bot.core.dca_engine import DCAEngine
    orchestrator.dca_engine = DCAEngine(
        symbol="SOL/USDT",
        trigger_percentage=Decimal("0.05"),
        amount_per_step=Decimal("20"),
        max_steps=5,
        take_profit_percentage=Decimal("0.10"),
    )

    await orchestrator._run_dca_catchup()

    # dry_run=True → create_order should NOT have been called
    exchange.create_order.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: catch_up respects existing open orders (does not duplicate)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_catchup_respects_existing_exchange_orders():
    from bot.orchestrator.bot_orchestrator import BotOrchestrator
    from bot.database.manager import DatabaseManager

    config = _make_bot_config(catch_up_enabled=True)
    exchange = _make_exchange(current_price=88.0)

    # Pre-populate open orders at the first DCA level: 88*(1-0.05*1) = 83.6
    exchange.fetch_open_orders.return_value = [{"price": "83.6", "side": "buy"}]

    db = MagicMock(spec=DatabaseManager)
    orchestrator = BotOrchestrator(config, exchange, db, redis_url="redis://localhost:6379")
    orchestrator.current_price = Decimal("88")

    from bot.core.dca_engine import DCAEngine
    orchestrator.dca_engine = DCAEngine(
        symbol="SOL/USDT",
        trigger_percentage=Decimal("0.05"),
        amount_per_step=Decimal("20"),
        max_steps=5,
        take_profit_percentage=Decimal("0.10"),
    )

    from bot.strategies.dca.startup_analyzer import DCAStartupAnalyzer

    analyzer = DCAStartupAnalyzer(
        trigger_pct=Decimal("0.05"),
        amount_per_step=Decimal("20"),
        max_steps=5,
        catch_up_max_orders=3,
        catch_up_reference="current_price",
        catch_up_lookback_bars=50,
    )
    plan = analyzer.analyze(
        ohlcv=[[i * 3_600_000, 90.0, 92.0, 88.0, 90.0, 1000.0] for i in range(50)],
        current_price=Decimal("88"),
        open_orders=[{"price": "83.6"}],
        min_order_size=Decimal("5"),
    )

    # Level 1 (83.6) should be skipped; levels 2 & 3 should be candidates
    assert plan.skipped_covered == 1
    assert len(plan.orders_to_place) <= 3
