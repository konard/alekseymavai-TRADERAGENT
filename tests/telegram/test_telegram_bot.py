"""Tests for TelegramBot — command handlers, auth, event notifications."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.orchestrator.bot_orchestrator import BotState
from bot.orchestrator.events import EventType, TradingEvent
from bot.telegram.bot import TelegramBot

# =============================================================================
# Fixtures
# =============================================================================


def _make_message(text: str, chat_id: int = 12345, user_id: int = 99) -> MagicMock:
    """Create a mock aiogram Message."""
    msg = MagicMock()
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.answer = AsyncMock()
    return msg


def _make_orchestrator(
    name: str = "test_bot",
    symbol: str = "BTC/USDT",
    strategy: str = "grid",
    state: BotState = BotState.RUNNING,
) -> MagicMock:
    """Create a mock BotOrchestrator."""
    orch = MagicMock()
    orch.config = MagicMock()
    orch.config.name = name
    orch.config.symbol = symbol
    orch.config.strategy = strategy
    orch.config.dry_run = True
    orch.state = state
    orch.current_price = Decimal("50000")

    # Exchange
    orch.exchange = MagicMock()
    orch.exchange.get_balance = AsyncMock(return_value={"USDT": 10000})
    orch.exchange.fetch_open_orders = AsyncMock(return_value=[])

    # Engines
    orch.grid_engine = None
    orch.dca_engine = None
    orch.trend_follower_strategy = None
    orch.risk_manager = None

    # Lifecycle
    orch.start = AsyncMock()
    orch.stop = AsyncMock()
    orch.pause = AsyncMock()
    orch.resume = AsyncMock()

    # Status
    orch.get_status = AsyncMock(
        return_value={
            "bot_name": name,
            "symbol": symbol,
            "strategy": strategy,
            "state": state.value,
            "current_price": "50000",
            "dry_run": True,
            "version": "2.0",
            "strategy_registry": {"total": 1, "active": 1},
        }
    )

    # Strategy registry
    orch.strategy_registry = MagicMock()
    orch.strategy_registry.get_registry_status.return_value = {"total": 1, "active": 1}
    orch.strategy_registry.stop_all = AsyncMock()
    orch.register_strategy = MagicMock()
    orch.start_strategy = AsyncMock()

    # Strategy lock
    orch._strategy_locked = False
    orch.lock_strategy = MagicMock()
    orch.unlock_strategy = MagicMock()

    return orch


@pytest.fixture
def bot():
    """Create TelegramBot with mock orchestrators."""
    orchestrators = {"test_bot": _make_orchestrator()}
    with patch("bot.telegram.bot.Bot"):
        tb = TelegramBot(
            token="fake-token",
            allowed_chat_ids=[12345],
            orchestrators=orchestrators,
        )
    return tb


@pytest.fixture
def bot_multi():
    """Create TelegramBot with multiple orchestrators."""
    orchestrators = {
        "grid_bot": _make_orchestrator("grid_bot", strategy="grid"),
        "dca_bot": _make_orchestrator("dca_bot", strategy="dca", state=BotState.PAUSED),
    }
    with patch("bot.telegram.bot.Bot"):
        tb = TelegramBot(
            token="fake-token",
            allowed_chat_ids=[12345, 67890],
            orchestrators=orchestrators,
        )
    return tb


# =============================================================================
# Auth Tests
# =============================================================================


class TestAuth:
    """Tests for authorization checks."""

    def test_authorized_user(self, bot):
        msg = _make_message("/status", chat_id=12345)
        assert bot._check_auth(msg) is True

    def test_unauthorized_user(self, bot):
        msg = _make_message("/status", chat_id=99999)
        assert bot._check_auth(msg) is False

    def test_no_user(self, bot):
        msg = _make_message("/status")
        msg.from_user = None
        assert bot._check_auth(msg) is False


# =============================================================================
# Command Handler Tests
# =============================================================================


class TestStartHelp:
    """Tests for /start and /help."""

    async def test_start_authorized(self, bot):
        msg = _make_message("/start")
        await bot._cmd_start(msg)
        msg.answer.assert_called_once()
        call_args = msg.answer.call_args
        text = call_args[0][0] if call_args[0] else call_args.kwargs.get("text", "")
        assert "TRADERAGENT" in text
        assert "/start" in text

    async def test_start_unauthorized(self, bot):
        msg = _make_message("/start", chat_id=99999)
        await bot._cmd_start(msg)
        msg.answer.assert_called_once()
        assert "Unauthorized" in msg.answer.call_args[0][0]

    async def test_help_calls_start(self, bot):
        msg = _make_message("/help")
        await bot._cmd_help(msg)
        msg.answer.assert_called()

    async def test_help_includes_new_commands(self, bot):
        msg = _make_message("/start")
        await bot._cmd_start(msg)
        text = msg.answer.call_args[0][0]
        assert "/positions" in text
        assert "/report" in text
        assert "/switch" in text


class TestListCommand:
    """Tests for /list."""

    async def test_list_shows_bots(self, bot_multi):
        msg = _make_message("/list")
        await bot_multi._cmd_list(msg)
        text = msg.answer.call_args[0][0]
        assert "grid_bot" in text
        assert "dca_bot" in text

    async def test_list_empty(self):
        with patch("bot.telegram.bot.Bot"):
            tb = TelegramBot(
                token="fake",
                allowed_chat_ids=[12345],
                orchestrators={},
            )
        msg = _make_message("/list")
        await tb._cmd_list(msg)
        assert "No bots" in msg.answer.call_args[0][0]


class TestStatusCommand:
    """Tests for /status."""

    async def test_status_specific_bot(self, bot):
        msg = _make_message("/status test_bot")
        await bot._cmd_status(msg)
        msg.answer.assert_called_once()
        orch = bot.orchestrators["test_bot"]
        orch.get_status.assert_called_once()

    async def test_status_all_bots(self, bot_multi):
        msg = _make_message("/status")
        await bot_multi._cmd_status(msg)
        text = msg.answer.call_args[0][0]
        assert "Bot Status Summary" in text

    async def test_status_not_found(self, bot):
        msg = _make_message("/status nonexistent")
        await bot._cmd_status(msg)
        assert "not found" in msg.answer.call_args[0][0]


class TestBotControlCommands:
    """Tests for /start_bot, /stop_bot, /pause, /resume."""

    async def test_start_bot(self, bot):
        msg = _make_message("/start_bot test_bot")
        await bot._cmd_start_bot(msg)
        bot.orchestrators["test_bot"].start.assert_called_once()
        assert "started" in msg.answer.call_args[0][0]

    async def test_start_bot_no_args(self, bot):
        msg = _make_message("/start_bot")
        await bot._cmd_start_bot(msg)
        assert "Usage" in msg.answer.call_args[0][0]

    async def test_start_bot_not_found(self, bot):
        msg = _make_message("/start_bot missing")
        await bot._cmd_start_bot(msg)
        assert "not found" in msg.answer.call_args[0][0]

    async def test_start_bot_failure(self, bot):
        bot.orchestrators["test_bot"].start.side_effect = RuntimeError("init failed")
        msg = _make_message("/start_bot test_bot")
        await bot._cmd_start_bot(msg)
        assert "Failed" in msg.answer.call_args[0][0]

    async def test_stop_bot(self, bot):
        msg = _make_message("/stop_bot test_bot")
        await bot._cmd_stop_bot(msg)
        bot.orchestrators["test_bot"].stop.assert_called_once()

    async def test_pause_bot(self, bot):
        msg = _make_message("/pause test_bot")
        await bot._cmd_pause(msg)
        bot.orchestrators["test_bot"].pause.assert_called_once()

    async def test_resume_bot(self, bot):
        msg = _make_message("/resume test_bot")
        await bot._cmd_resume(msg)
        bot.orchestrators["test_bot"].resume.assert_called_once()


class TestBalanceCommand:
    """Tests for /balance."""

    async def test_balance(self, bot):
        orch = bot.orchestrators["test_bot"]
        orch.exchange.get_balance = AsyncMock(return_value={"USDT": 10000})
        msg = _make_message("/balance test_bot")
        await bot._cmd_balance(msg)
        text = msg.answer.call_args[0][0]
        assert "Balance" in text

    async def test_balance_no_args(self, bot):
        msg = _make_message("/balance")
        await bot._cmd_balance(msg)
        assert "Usage" in msg.answer.call_args[0][0]


class TestOrdersCommand:
    """Tests for /orders."""

    async def test_orders_empty(self, bot):
        msg = _make_message("/orders test_bot")
        await bot._cmd_orders(msg)
        assert "No open orders" in msg.answer.call_args[0][0]

    async def test_orders_with_data(self, bot):
        orch = bot.orchestrators["test_bot"]
        orch.exchange.fetch_open_orders = AsyncMock(
            return_value=[
                {"id": "123", "side": "buy", "price": 49000, "amount": 0.01},
            ]
        )
        msg = _make_message("/orders test_bot")
        await bot._cmd_orders(msg)
        text = msg.answer.call_args[0][0]
        assert "123" in text
        assert "BUY" in text


class TestPnlCommand:
    """Tests for /pnl."""

    async def test_pnl_basic(self, bot):
        msg = _make_message("/pnl test_bot")
        await bot._cmd_pnl(msg)
        text = msg.answer.call_args[0][0]
        assert "P&amp;L" in text or "P&L" in text

    async def test_pnl_with_grid(self, bot):
        orch = bot.orchestrators["test_bot"]
        orch.grid_engine = MagicMock()
        orch.grid_engine.get_grid_status.return_value = {
            "total_profit": "150.0",
            "buy_count": 10,
            "sell_count": 8,
        }
        msg = _make_message("/pnl test_bot")
        await bot._cmd_pnl(msg)
        text = msg.answer.call_args[0][0]
        assert "Grid" in text
        assert "150.0" in text


# =============================================================================
# New Command Tests
# =============================================================================


class TestPositionsCommand:
    """Tests for /positions."""

    async def test_positions_no_args(self, bot):
        msg = _make_message("/positions")
        await bot._cmd_positions(msg)
        assert "Usage" in msg.answer.call_args[0][0]

    async def test_positions_not_found(self, bot):
        msg = _make_message("/positions missing")
        await bot._cmd_positions(msg)
        assert "not found" in msg.answer.call_args[0][0]

    async def test_positions_no_positions(self, bot):
        msg = _make_message("/positions test_bot")
        await bot._cmd_positions(msg)
        text = msg.answer.call_args[0][0]
        assert "No open positions" in text

    async def test_positions_with_dca(self, bot):
        orch = bot.orchestrators["test_bot"]
        orch.dca_engine = MagicMock()
        orch.dca_engine.position = MagicMock()
        orch.dca_engine.position.avg_entry_price = Decimal("48000")
        orch.dca_engine.position.total_amount = Decimal("0.5")
        orch.dca_engine.position.get_pnl.return_value = Decimal("1000")
        orch.dca_engine.position.get_pnl_percentage.return_value = Decimal("0.042")
        orch.dca_engine.current_step = 3
        orch.dca_engine.max_steps = 10
        msg = _make_message("/positions test_bot")
        await bot._cmd_positions(msg)
        text = msg.answer.call_args[0][0]
        assert "DCA Position" in text
        assert "48000" in text

    async def test_positions_with_grid(self, bot):
        orch = bot.orchestrators["test_bot"]
        orch.grid_engine = MagicMock()
        order = MagicMock()
        order.side = "buy"
        order.price = Decimal("49500")
        orch.grid_engine.active_orders = {"order1": order}
        msg = _make_message("/positions test_bot")
        await bot._cmd_positions(msg)
        text = msg.answer.call_args[0][0]
        assert "Grid Orders" in text

    async def test_positions_unauthorized(self, bot):
        msg = _make_message("/positions test_bot", chat_id=99999)
        await bot._cmd_positions(msg)
        assert "Unauthorized" in msg.answer.call_args[0][0]


class TestReportCommand:
    """Tests for /report."""

    async def test_report_specific_bot(self, bot):
        msg = _make_message("/report test_bot")
        await bot._cmd_report(msg)
        text = msg.answer.call_args[0][0]
        assert "Performance Report" in text
        assert "test_bot" in text

    async def test_report_all_bots(self, bot_multi):
        msg = _make_message("/report")
        await bot_multi._cmd_report(msg)
        # Should call answer twice — once per bot
        assert msg.answer.call_count == 2

    async def test_report_not_found(self, bot):
        msg = _make_message("/report missing")
        await bot._cmd_report(msg)
        assert "not found" in msg.answer.call_args[0][0]

    async def test_report_with_risk(self, bot):
        orch = bot.orchestrators["test_bot"]
        orch.get_status = AsyncMock(
            return_value={
                "bot_name": "test_bot",
                "symbol": "BTC/USDT",
                "strategy": "grid",
                "state": "running",
                "current_price": "50000",
                "dry_run": True,
                "version": "2.0",
                "strategy_registry": {"total": 1, "active": 1},
                "risk": {
                    "drawdown": Decimal("0.05"),
                    "pnl_percentage": Decimal("0.12"),
                    "is_halted": False,
                },
            }
        )
        msg = _make_message("/report test_bot")
        await bot._cmd_report(msg)
        text = msg.answer.call_args[0][0]
        assert "Risk" in text

    async def test_report_unauthorized(self, bot):
        msg = _make_message("/report", chat_id=99999)
        await bot._cmd_report(msg)
        assert "Unauthorized" in msg.answer.call_args[0][0]


class TestSwitchStrategyCommand:
    """Tests for /switch_strategy."""

    async def test_switch_no_args(self, bot):
        msg = _make_message("/switch_strategy")
        await bot._cmd_switch_strategy(msg)
        assert "Usage" in msg.answer.call_args[0][0]

    async def test_switch_not_found(self, bot):
        msg = _make_message("/switch_strategy missing dca")
        await bot._cmd_switch_strategy(msg)
        assert "not found" in msg.answer.call_args[0][0]

    async def test_switch_success(self, bot):
        msg = _make_message("/switch_strategy test_bot dca")
        await bot._cmd_switch_strategy(msg)
        orch = bot.orchestrators["test_bot"]
        orch.strategy_registry.stop_all.assert_called_once()
        orch.register_strategy.assert_called_once_with(
            strategy_id="dca",
            strategy_type="dca",
        )
        orch.start_strategy.assert_called_once_with("dca")
        assert "switched" in msg.answer.call_args[0][0]

    async def test_switch_failure(self, bot):
        orch = bot.orchestrators["test_bot"]
        orch.start_strategy.side_effect = RuntimeError("not supported")
        msg = _make_message("/switch_strategy test_bot smc")
        await bot._cmd_switch_strategy(msg)
        assert "Failed" in msg.answer.call_args[0][0]

    async def test_switch_unauthorized(self, bot):
        msg = _make_message("/switch_strategy test_bot dca", chat_id=99999)
        await bot._cmd_switch_strategy(msg)
        assert "Unauthorized" in msg.answer.call_args[0][0]


class TestLockStrategyCommand:
    """Tests for /lock_strategy."""

    async def test_lock_no_args(self, bot):
        msg = _make_message("/lock_strategy")
        await bot._cmd_lock_strategy(msg)
        assert "Usage" in msg.answer.call_args[0][0]

    async def test_lock_not_found(self, bot):
        msg = _make_message("/lock_strategy missing smc")
        await bot._cmd_lock_strategy(msg)
        assert "not found" in msg.answer.call_args[0][0]

    async def test_lock_invalid_strategy(self, bot):
        msg = _make_message("/lock_strategy test_bot invalid_strat")
        await bot._cmd_lock_strategy(msg)
        text = msg.answer.call_args[0][0]
        assert "Invalid" in text
        assert "invalid_strat" in text

    async def test_lock_success_single(self, bot):
        msg = _make_message("/lock_strategy test_bot smc")
        await bot._cmd_lock_strategy(msg)
        orch = bot.orchestrators["test_bot"]
        orch.lock_strategy.assert_called_once_with({"smc"})
        text = msg.answer.call_args[0][0]
        assert "locked" in text.lower()
        assert "smc" in text

    async def test_lock_success_multiple(self, bot):
        msg = _make_message("/lock_strategy test_bot grid dca")
        await bot._cmd_lock_strategy(msg)
        orch = bot.orchestrators["test_bot"]
        orch.lock_strategy.assert_called_once_with({"grid", "dca"})

    async def test_lock_unauthorized(self, bot):
        msg = _make_message("/lock_strategy test_bot smc", chat_id=99999)
        await bot._cmd_lock_strategy(msg)
        assert "Unauthorized" in msg.answer.call_args[0][0]


class TestUnlockStrategyCommand:
    """Tests for /unlock_strategy."""

    async def test_unlock_no_args(self, bot):
        msg = _make_message("/unlock_strategy")
        await bot._cmd_unlock_strategy(msg)
        assert "Usage" in msg.answer.call_args[0][0]

    async def test_unlock_not_found(self, bot):
        msg = _make_message("/unlock_strategy missing")
        await bot._cmd_unlock_strategy(msg)
        assert "not found" in msg.answer.call_args[0][0]

    async def test_unlock_success(self, bot):
        msg = _make_message("/unlock_strategy test_bot")
        await bot._cmd_unlock_strategy(msg)
        orch = bot.orchestrators["test_bot"]
        orch.unlock_strategy.assert_called_once()
        text = msg.answer.call_args[0][0]
        assert "unlocked" in text.lower()

    async def test_unlock_unauthorized(self, bot):
        msg = _make_message("/unlock_strategy test_bot", chat_id=99999)
        await bot._cmd_unlock_strategy(msg)
        assert "Unauthorized" in msg.answer.call_args[0][0]


# =============================================================================
# Event Notification Tests
# =============================================================================


class TestEventNotifications:
    """Tests for event formatting and handling."""

    def test_format_event_basic(self, bot):
        event = TradingEvent.create(
            EventType.BOT_STARTED,
            "test_bot",
            {"strategy": "grid"},
        )
        text = bot._format_event_notification(event)
        assert "Bot Started" in text
        assert "test_bot" in text
        assert "strategy" in text

    def test_format_event_v2_regime_changed(self, bot):
        event = TradingEvent.create(
            EventType.REGIME_CHANGED,
            "test_bot",
            {"old_regime": "sideways", "new_regime": "uptrend"},
        )
        text = bot._format_event_notification(event)
        assert "Regime Changed" in text
        assert "🔄" in text

    def test_format_event_v2_hybrid_transition(self, bot):
        event = TradingEvent.create(
            EventType.HYBRID_TRANSITION,
            "test_bot",
            {"from": "grid", "to": "dca"},
        )
        text = bot._format_event_notification(event)
        assert "Hybrid Transition" in text
        assert "🔀" in text

    def test_format_event_unknown_type_gets_default_emoji(self, bot):
        event = TradingEvent.create(
            EventType.PRICE_UPDATED,
            "test_bot",
            {"price": "50000"},
        )
        text = bot._format_event_notification(event)
        assert "ℹ️" in text

    async def test_handle_event_important(self, bot):
        event = TradingEvent.create(
            EventType.ORDER_FILLED,
            "test_bot",
            {"price": "50000"},
        )
        with patch.object(bot.bot, "send_message", new_callable=AsyncMock) as mock_send:
            await bot._handle_event(event)
            mock_send.assert_called_once()

    async def test_handle_event_not_important(self, bot):
        event = TradingEvent.create(
            EventType.PRICE_UPDATED,
            "test_bot",
            {"price": "50000"},
        )
        with patch.object(bot.bot, "send_message", new_callable=AsyncMock) as mock_send:
            await bot._handle_event(event)
            mock_send.assert_not_called()

    async def test_handle_event_strategy_lock_important(self, bot):
        """STRATEGY_LOCKED and STRATEGY_UNLOCKED should be treated as important."""
        for event_type in [EventType.STRATEGY_LOCKED, EventType.STRATEGY_UNLOCKED]:
            event = TradingEvent.create(event_type, "test_bot", {})
            with patch.object(bot.bot, "send_message", new_callable=AsyncMock) as mock_send:
                await bot._handle_event(event)
                mock_send.assert_called_once()

    async def test_handle_event_v2_important(self, bot):
        """v2.0 events like REGIME_CHANGED should be treated as important."""
        for event_type in [
            EventType.REGIME_CHANGED,
            EventType.HYBRID_TRANSITION,
            EventType.HEALTH_DEGRADED,
            EventType.HEALTH_CRITICAL,
            EventType.STRATEGY_ERROR,
        ]:
            event = TradingEvent.create(event_type, "test_bot", {})
            with patch.object(bot.bot, "send_message", new_callable=AsyncMock) as mock_send:
                await bot._handle_event(event)
                mock_send.assert_called_once()


# =============================================================================
# Status Formatting Tests
# =============================================================================


class TestStatusFormatting:
    """Tests for bot status formatting."""

    def test_format_status_basic(self, bot):
        status = {
            "bot_name": "test_bot",
            "state": "running",
            "symbol": "BTC/USDT",
            "strategy": "grid",
            "dry_run": True,
            "current_price": "50000",
        }
        text = bot._format_status(status)
        assert "test_bot" in text
        assert "running" in text
        assert "BTC/USDT" in text
        assert "50000" in text

    def test_format_status_with_grid(self, bot):
        status = {
            "bot_name": "test_bot",
            "state": "running",
            "symbol": "BTC/USDT",
            "strategy": "grid",
            "dry_run": False,
            "grid": {"active_orders": 10, "total_profit": "250.5"},
        }
        text = bot._format_status(status)
        assert "Grid Status" in text
        assert "250.5" in text

    def test_format_status_with_dca(self, bot):
        status = {
            "bot_name": "test_bot",
            "state": "running",
            "symbol": "BTC/USDT",
            "strategy": "dca",
            "dry_run": True,
            "dca": {
                "has_position": True,
                "current_step": 3,
                "max_steps": 10,
                "avg_entry_price": "48000",
            },
        }
        text = bot._format_status(status)
        assert "DCA Position" in text
        assert "3/10" in text

    def test_format_status_with_risk(self, bot):
        status = {
            "bot_name": "test_bot",
            "state": "running",
            "symbol": "BTC/USDT",
            "strategy": "grid",
            "dry_run": True,
            "risk": {"halted": False, "drawdown": Decimal("0.05")},
        }
        text = bot._format_status(status)
        assert "Risk Status" in text

    def test_format_status_locked(self, bot):
        status = {
            "bot_name": "test_bot",
            "state": "running",
            "symbol": "BTC/USDT",
            "strategy": "grid",
            "dry_run": True,
            "strategy_lock": {"locked": True, "strategies": ["smc"]},
            "active_strategies": ["smc"],
        }
        text = bot._format_status(status)
        assert "LOCKED" in text
        assert "smc" in text

    def test_format_status_unlocked(self, bot):
        status = {
            "bot_name": "test_bot",
            "state": "running",
            "symbol": "BTC/USDT",
            "strategy": "grid",
            "dry_run": True,
            "strategy_lock": {"locked": False, "strategies": None},
            "active_strategies": ["grid", "dca"],
        }
        text = bot._format_status(status)
        assert "AUTO" in text
        assert "grid" in text

    def test_get_state_emoji(self):
        assert TelegramBot._get_state_emoji(BotState.RUNNING) == "🟢"
        assert TelegramBot._get_state_emoji(BotState.STOPPED) == "⚫"
        assert TelegramBot._get_state_emoji(BotState.EMERGENCY) == "🔴"
        assert TelegramBot._get_state_emoji(BotState.PAUSED) == "🟡"


# =============================================================================
# Lifecycle Tests
# =============================================================================


class TestLifecycle:
    """Tests for bot start/stop lifecycle."""

    async def test_stop_cleans_up(self, bot):
        # Create a real asyncio task that we can cancel
        async def _forever():
            await asyncio.sleep(999)

        task = asyncio.create_task(_forever())
        bot.event_listener_task = task

        with patch.object(bot.bot.session, "close", new_callable=AsyncMock):
            await bot.stop()
            assert task.cancelled()

    def test_init_registers_handlers(self, bot):
        # Verify router was included in dispatcher
        assert bot.router in bot.dp.sub_routers


# =============================================================================
# Trades Command Tests
# =============================================================================


class TestTradesCommand:
    """Tests for /trades."""

    async def test_trades_no_history(self, bot):
        msg = _make_message("/trades test_bot")
        await bot._cmd_trades(msg)
        text = msg.answer.call_args[0][0]
        assert "No trade history" in text

    async def test_trades_not_found(self, bot):
        msg = _make_message("/trades nonexistent")
        await bot._cmd_trades(msg)
        assert "not found" in msg.answer.call_args[0][0]

    async def test_trades_unauthorized(self, bot):
        msg = _make_message("/trades test_bot", chat_id=99999)
        await bot._cmd_trades(msg)
        assert "Unauthorized" in msg.answer.call_args[0][0]

    async def test_trades_with_grid(self, bot):
        orch = bot.orchestrators["test_bot"]
        orch.grid_engine = MagicMock()
        order = MagicMock()
        order.side = "buy"
        order.price = Decimal("49500")
        order.amount = Decimal("0.01")
        order.level = 3
        orch.grid_engine.filled_orders = [order]
        msg = _make_message("/trades test_bot")
        await bot._cmd_trades(msg)
        text = msg.answer.call_args[0][0]
        assert "Grid" in text
        assert "BUY" in text
        assert "49500" in text

    async def test_trades_with_smc(self, bot):
        orch = bot.orchestrators["test_bot"]
        orch.smc_strategy = MagicMock()
        pos = MagicMock()
        pos.entry_price = Decimal("48000")
        pos.current_price = Decimal("49000")
        pos.realized_pnl = Decimal("100")
        pos.exit_reason = "take_profit"
        pos.hold_time_hours = 2.5
        orch.smc_strategy.position_manager.closed_positions = [pos]
        msg = _make_message("/trades test_bot")
        await bot._cmd_trades(msg)
        text = msg.answer.call_args[0][0]
        assert "SMC" in text
        assert "48000" in text
        assert "+100" in text
        assert "take_profit" in text

    async def test_trades_custom_limit(self, bot):
        orch = bot.orchestrators["test_bot"]
        orch.grid_engine = MagicMock()
        orders = []
        for i in range(20):
            o = MagicMock()
            o.side = "buy"
            o.price = Decimal(str(49000 + i))
            o.amount = Decimal("0.01")
            o.level = i
            orders.append(o)
        orch.grid_engine.filled_orders = orders
        msg = _make_message("/trades test_bot 5")
        await bot._cmd_trades(msg)
        text = msg.answer.call_args[0][0]
        assert "last 5" in text

    async def test_trades_all_bots(self, bot_multi):
        msg = _make_message("/trades")
        await bot_multi._cmd_trades(msg)
        # Should report for each bot (no history for any)
        assert msg.answer.call_count == 2
