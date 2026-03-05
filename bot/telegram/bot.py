"""
Telegram bot for managing and monitoring trading bots.

Thin facade that wires together handler routers, keyboards,
formatting, and the Redis event listener.
"""

import asyncio
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message

from bot.orchestrator.bot_orchestrator import BotOrchestrator, BotState
from bot.orchestrator.events import TradingEvent
from bot.telegram.events import EventListener
from bot.telegram.formatting import (
    IMPORTANT_EVENTS,
    format_event_notification,
    format_status,
    get_state_emoji,
)
from bot.telegram import keyboards
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class TelegramBot:
    """
    Telegram bot for trading bot management.

    Features:
    - Control commands: /start, /stop, /pause, /resume
    - Status monitoring: /status, /balance, /orders, /pnl
    - Inline-button navigation
    - Event notifications via Redis Pub/Sub
    - Multi-bot management support
    """

    _VALID_STRATEGIES = {"grid", "dca", "trend_follower", "smc"}

    def __init__(
        self,
        token: str,
        allowed_chat_ids: list[int],
        orchestrators: dict[str, BotOrchestrator],
        redis_url: str = "redis://localhost:6379",
    ):
        self.token = token
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.orchestrators = orchestrators
        self.redis_url = redis_url

        # Aiogram setup
        self.bot = Bot(token=token)
        self.dp = Dispatcher()
        self.router = Router()

        # Redis event listener
        self.redis_client = None  # kept for test compat
        self.event_listener_task: asyncio.Task | None = None
        self._event_listener = EventListener(
            bot=self.bot,
            allowed_chat_ids=self.allowed_chat_ids,
            bot_names=list(orchestrators.keys()),
            redis_url=redis_url,
        )

        # Register all handlers on this instance's router
        self._register_handlers()

        logger.info(
            "telegram_bot_initialized",
            bot_count=len(orchestrators),
            allowed_chats=len(allowed_chat_ids),
        )

    # ── Auth ──────────────────────────────────────────────────────────────

    def _check_auth(self, message: Message) -> bool:
        if message.from_user is None:
            return False
        return message.chat.id in self.allowed_chat_ids

    def _check_auth_cb(self, callback: CallbackQuery) -> bool:
        if callback.from_user is None:
            return False
        return callback.message.chat.id in self.allowed_chat_ids

    # ── Handler registration ──────────────────────────────────────────────

    def _register_handlers(self) -> None:
        """Register all command and callback handlers on this instance's router."""
        r = self.router

        # Control commands
        r.message(CommandStart())(self._cmd_start)
        r.message(Command("help"))(self._cmd_help)
        r.message(Command("list"))(self._cmd_list)
        r.message(Command("start_bot"))(self._cmd_start_bot)
        r.message(Command("stop_bot"))(self._cmd_stop_bot)
        r.message(Command("pause"))(self._cmd_pause)
        r.message(Command("resume"))(self._cmd_resume)

        # Monitoring commands
        r.message(Command("status"))(self._cmd_status)
        r.message(Command("balance"))(self._cmd_balance)
        r.message(Command("orders"))(self._cmd_orders)
        r.message(Command("positions"))(self._cmd_positions)
        r.message(Command("pnl"))(self._cmd_pnl)
        r.message(Command("report"))(self._cmd_report)
        r.message(Command("trades"))(self._cmd_trades)

        # Strategy commands
        r.message(Command("switch_strategy"))(self._cmd_switch_strategy)
        r.message(Command("lock_strategy"))(self._cmd_lock_strategy)
        r.message(Command("unlock_strategy"))(self._cmd_unlock_strategy)

        # Portfolio commands
        r.message(Command("scan"))(self._cmd_scan)
        r.message(Command("create_bot"))(self._cmd_create_bot)
        r.message(Command("delete_bot"))(self._cmd_delete_bot)
        r.message(Command("portfolio"))(self._cmd_portfolio)

        # ── Inline-button callbacks ───────────────────────────────────────
        r.callback_query(lambda c: c.data == "menu:main")(self._cb_main_menu)
        r.callback_query(lambda c: c.data == "menu:control")(self._cb_control_menu)
        r.callback_query(lambda c: c.data == "menu:monitoring")(self._cb_monitoring_menu)
        r.callback_query(lambda c: c.data == "menu:strategy")(self._cb_strategy_menu)
        r.callback_query(lambda c: c.data == "menu:portfolio")(self._cb_portfolio_menu)

        # Bot selectors  (ctl:<bot>, mon:<bot>, strat:<bot>)
        r.callback_query(lambda c: c.data and c.data.startswith("ctl:") and c.data.count(":") == 1)(self._cb_control_bot)
        r.callback_query(lambda c: c.data and c.data.startswith("ctl:") and c.data.count(":") == 2)(self._cb_control_action)
        r.callback_query(lambda c: c.data and c.data.startswith("mon:") and c.data.count(":") == 1)(self._cb_monitoring_bot)
        r.callback_query(lambda c: c.data and c.data.startswith("mon:") and c.data.count(":") == 2)(self._cb_monitoring_action)
        r.callback_query(lambda c: c.data and c.data.startswith("strat:") and c.data.count(":") == 1)(self._cb_strategy_bot)
        r.callback_query(lambda c: c.data and c.data.startswith("strat:") and ":switch:" in c.data)(self._cb_strategy_switch)
        r.callback_query(lambda c: c.data and c.data.startswith("strat:") and c.data.endswith(":unlock"))(self._cb_strategy_unlock)

        # Portfolio callbacks
        r.callback_query(lambda c: c.data == "port:overview")(self._cb_portfolio_overview)
        r.callback_query(lambda c: c.data == "port:report_all")(self._cb_portfolio_report_all)
        r.callback_query(lambda c: c.data == "port:scan")(self._cb_portfolio_scan)

        self.dp.include_router(self.router)

    # ══════════════════════════════════════════════════════════════════════
    # TEXT COMMAND HANDLERS
    # ══════════════════════════════════════════════════════════════════════

    async def _cmd_start(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        await message.answer(
            "🤖 *TRADERAGENT Bot Manager*\n\n"
            "Welcome to the trading bot management interface.\n\n"
            "*Available Commands:*\n\n"
            "*Control:*\n"
            "/start\\_bot <name> - Start a trading bot\n"
            "/stop\\_bot <name> - Stop a trading bot\n"
            "/pause <name> - Pause trading bot\n"
            "/resume <name> - Resume trading bot\n"
            "/switch\\_strategy <name> <strategy> - Switch strategy\n"
            "/lock\\_strategy <name> <strategy> - Lock to a strategy\n"
            "/unlock\\_strategy <name> - Unlock (auto mode)\n\n"
            "*Monitoring:*\n"
            "/status [name] - Bot status (all bots if no name)\n"
            "/balance <name> - Current balance\n"
            "/orders <name> - Open orders\n"
            "/positions <name> - Open positions\n"
            "/pnl <name> - Profit and Loss\n"
            "/report [name] - Performance report\n/trades [name] [N] - Trade history\n"
            "/list - List all bots\n\n"
            "*Other:*\n"
            "/help - Show this help message\n",
            parse_mode="Markdown",
            reply_markup=keyboards.main_menu(),
        )

    async def _cmd_help(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        await self._cmd_start(message)

    async def _cmd_list(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        if not self.orchestrators:
            await message.answer("📋 No bots configured")
            return
        response = "📋 *Available Bots:*\n\n"
        for name, orch in self.orchestrators.items():
            state_emoji = get_state_emoji(orch.state)
            response += (
                f"{state_emoji} *{name}*\n"
                f"  Symbol: {orch.config.symbol}\n"
                f"  Strategy: {orch.config.strategy}\n"
                f"  State: {orch.state.value}\n\n"
            )
        await message.answer(response, parse_mode="Markdown")

    async def _cmd_status(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        bot_name = args[1] if len(args) > 1 else None
        if bot_name:
            if bot_name not in self.orchestrators:
                await message.answer(f"❌ Bot '{bot_name}' not found")
                return
            status = await self.orchestrators[bot_name].get_status()
            await message.answer(format_status(status), parse_mode="Markdown")
        else:
            if not self.orchestrators:
                await message.answer("📋 No bots configured")
                return
            response = "📊 *Bot Status Summary:*\n\n"
            for name, orch in self.orchestrators.items():
                se = get_state_emoji(orch.state)
                lock_icon = "🔒" if orch._strategy_locked else "🔄"
                response += f"{se} *{name}*: {orch.state.value} {lock_icon}\n"
            response += "\nUse `/status <bot_name>` for detailed info"
            await message.answer(response, parse_mode="Markdown")

    async def _cmd_start_bot(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 2:
            await message.answer("Usage: /start_bot <bot_name>")
            return
        bot_name = args[1]
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        try:
            await self.orchestrators[bot_name].start()
            await message.answer(f"✅ Bot '{bot_name}' started successfully")
        except Exception as e:
            logger.error("start_bot_failed", bot_name=bot_name, error=str(e))
            await message.answer(f"❌ Failed to start bot: {str(e)}")

    async def _cmd_stop_bot(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 2:
            await message.answer("Usage: /stop_bot <bot_name>")
            return
        bot_name = args[1]
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        try:
            await self.orchestrators[bot_name].stop()
            await message.answer(f"🛑 Bot '{bot_name}' stopped successfully")
        except Exception as e:
            logger.error("stop_bot_failed", bot_name=bot_name, error=str(e))
            await message.answer(f"❌ Failed to stop bot: {str(e)}")

    async def _cmd_pause(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 2:
            await message.answer("Usage: /pause <bot_name>")
            return
        bot_name = args[1]
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        try:
            await self.orchestrators[bot_name].pause()
            await message.answer(f"⏸️ Bot '{bot_name}' paused")
        except Exception as e:
            logger.error("pause_bot_failed", bot_name=bot_name, error=str(e))
            await message.answer(f"❌ Failed to pause bot: {str(e)}")

    async def _cmd_resume(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 2:
            await message.answer("Usage: /resume <bot_name>")
            return
        bot_name = args[1]
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        try:
            await self.orchestrators[bot_name].resume()
            await message.answer(f"▶️ Bot '{bot_name}' resumed")
        except Exception as e:
            logger.error("resume_bot_failed", bot_name=bot_name, error=str(e))
            await message.answer(f"❌ Failed to resume bot: {str(e)}")

    async def _cmd_balance(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 2:
            await message.answer("Usage: /balance <bot_name>")
            return
        bot_name = args[1]
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        orch = self.orchestrators[bot_name]
        try:
            balance = await orch.exchange.get_balance()
            quote_currency = orch.config.symbol.split("/")[1]
            available = balance.get(quote_currency, 0)
            response = (
                f"💰 *Balance for {bot_name}*\n\n"
                f"Currency: {quote_currency}\n"
                f"Available: {available}\n"
            )
            if orch.risk_manager:
                risk_status = orch.risk_manager.get_risk_status()
                response += (
                    f"\n*Risk Status:*\n"
                    f"Initial Balance: {risk_status['initial_balance']}\n"
                    f"Current Balance: {risk_status['current_balance']}\n"
                )
                if risk_status["drawdown"] is not None:
                    response += f"Drawdown: {float(risk_status['drawdown']):.2%}\n"
            await message.answer(response, parse_mode="Markdown")
        except Exception as e:
            logger.error("balance_fetch_failed", bot_name=bot_name, error=str(e))
            await message.answer(f"❌ Failed to fetch balance: {str(e)}")

    async def _cmd_orders(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 2:
            await message.answer("Usage: /orders <bot_name>")
            return
        bot_name = args[1]
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        orch = self.orchestrators[bot_name]
        try:
            orders = await orch.exchange.fetch_open_orders(orch.config.symbol)
            if not orders:
                await message.answer(f"📋 No open orders for {bot_name}")
                return
            response = f"📋 *Open Orders for {bot_name}*\n\n"
            for order in orders[:10]:
                response += (
                    f"ID: `{order['id']}`\n"
                    f"Side: {order['side'].upper()}\n"
                    f"Price: {order['price']}\n"
                    f"Amount: {order['amount']}\n\n"
                )
            if len(orders) > 10:
                response += f"... and {len(orders) - 10} more orders"
            await message.answer(response, parse_mode="Markdown")
        except Exception as e:
            logger.error("orders_fetch_failed", bot_name=bot_name, error=str(e))
            await message.answer(f"❌ Failed to fetch orders: {str(e)}")

    async def _cmd_pnl(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 2:
            await message.answer("Usage: /pnl <bot_name>")
            return
        bot_name = args[1]
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        orch = self.orchestrators[bot_name]
        response = f"📊 *P&L for {bot_name}*\n\n"
        if orch.grid_engine:
            grid_status = orch.grid_engine.get_grid_status()
            response += (
                f"*Grid Trading:*\n"
                f"Total Profit: {grid_status['total_profit']}\n"
                f"Buy Count: {grid_status['buy_count']}\n"
                f"Sell Count: {grid_status['sell_count']}\n\n"
            )
        if orch.dca_engine and orch.dca_engine.position and orch.current_price:
            dca_status = orch.dca_engine.get_position_status()
            pnl = orch.dca_engine.position.get_pnl(orch.current_price)
            pnl_pct = orch.dca_engine.position.get_pnl_percentage(orch.current_price)
            response += (
                f"*DCA Position:*\n"
                f"P&L: {pnl}\n"
                f"P&L %: {float(pnl_pct):.2%}\n"
                f"Avg Entry: {dca_status['avg_entry_price']}\n"
                f"Current Price: {orch.current_price}\n\n"
            )
        if orch.risk_manager:
            risk_status = orch.risk_manager.get_risk_status()
            if risk_status["pnl_percentage"] is not None:
                response += f"*Overall:*\nP&L %: {float(risk_status['pnl_percentage']):.2%}\n"
        await message.answer(response, parse_mode="Markdown")

    async def _cmd_positions(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 2:
            await message.answer("Usage: /positions <bot_name>")
            return
        bot_name = args[1]
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        orch = self.orchestrators[bot_name]
        response = f"📊 *Positions for {bot_name}*\n\n"
        has_positions = False
        if orch.dca_engine and orch.dca_engine.position:
            has_positions = True
            pos = orch.dca_engine.position
            response += (
                f"*DCA Position:*\n"
                f"Symbol: {orch.config.symbol}\n"
                f"Entry: {pos.avg_entry_price}\n"
                f"Amount: {pos.total_amount}\n"
                f"Steps: {orch.dca_engine.current_step}/{orch.dca_engine.max_steps}\n"
            )
            if orch.current_price:
                pnl = pos.get_pnl(orch.current_price)
                pnl_pct = pos.get_pnl_percentage(orch.current_price)
                response += f"P&L: {pnl} ({float(pnl_pct):.2%})\n"
            response += "\n"
        if orch.trend_follower_strategy:
            active = orch.trend_follower_strategy.position_manager.active_positions
            if active:
                has_positions = True
                response += f"*Trend-Follower ({len(active)} active):*\n"
                for pid, pos in list(active.items())[:5]:
                    response += (
                        f"ID: `{pid[:8]}`\n"
                        f"  Type: {pos.signal_type.value}\n"
                        f"  Entry: {pos.entry_price}\n"
                        f"  Size: {pos.size}\n\n"
                    )
                if len(active) > 5:
                    response += f"... and {len(active) - 5} more positions\n"
        if orch.grid_engine and orch.grid_engine.active_orders:
            has_positions = True
            orders = orch.grid_engine.active_orders
            response += f"*Grid Orders ({len(orders)} active):*\n"
            for _oid, order in list(orders.items())[:5]:
                response += f"  {order.side.upper()} @ {order.price}\n"
            if len(orders) > 5:
                response += f"  ... and {len(orders) - 5} more\n"
        if not has_positions:
            response += "No open positions"
        await message.answer(response, parse_mode="Markdown")

    async def _cmd_report(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        bot_name = args[1] if len(args) > 1 else None
        if bot_name and bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        targets = {bot_name: self.orchestrators[bot_name]} if bot_name else self.orchestrators
        for name, orch in targets.items():
            response = f"📈 *Performance Report: {name}*\n\n"
            status = await orch.get_status()
            response += f"State: {status['state']}\nStrategy: {status['strategy']}\n"
            if status.get("current_price"):
                response += f"Current Price: {status['current_price']}\n"
            if "grid" in status:
                grid = status["grid"]
                response += (
                    f"\n*Grid:*\n"
                    f"Total Profit: {grid.get('total_profit', 0)}\n"
                    f"Buy Count: {grid.get('buy_count', 0)}\n"
                    f"Sell Count: {grid.get('sell_count', 0)}\n"
                )
            if "dca" in status:
                dca = status["dca"]
                response += (
                    f"\n*DCA:*\n"
                    f"Position: {'Yes' if dca.get('has_position') else 'No'}\n"
                    f"Steps: {dca.get('current_step', 0)}/{dca.get('max_steps', 0)}\n"
                )
            if "trend_follower" in status:
                tf = status["trend_follower"]
                response += f"\n*Trend-Follower:*\nActive Positions: {tf.get('active_positions', 0)}\n"
                stats = tf.get("statistics", {})
                if stats:
                    response += (
                        f"Total Trades: {stats.get('total_trades', 0)}\n"
                        f"Win Rate: {stats.get('win_rate', 0):.1%}\n"
                        f"Total P&L: {stats.get('total_pnl', 0)}\n"
                    )
            if "risk" in status:
                risk = status["risk"]
                response += "\n*Risk:*\n"
                if risk.get("drawdown") is not None:
                    response += f"Drawdown: {float(risk['drawdown']):.2%}\n"
                if risk.get("pnl_percentage") is not None:
                    response += f"P&L: {float(risk['pnl_percentage']):.2%}\n"
                response += f"Halted: {'Yes' if risk.get('is_halted') else 'No'}\n"
            if "market_regime" in status:
                regime = status["market_regime"]
                response += (
                    f"\n*Market Regime:*\n"
                    f"Regime: {regime.get('regime', 'unknown')}\n"
                    f"Confidence: {regime.get('confidence', 0):.1%}\n"
                )
            if "strategy_registry" in status:
                reg = status["strategy_registry"]
                response += (
                    f"\n*Strategies:*\n"
                    f"Total: {reg.get('total', 0)}\n"
                    f"Active: {reg.get('active', 0)}\n"
                )
            await message.answer(response, parse_mode="Markdown")


    async def _cmd_trades(self, message: Message) -> None:
        """Handle /trades [name] [N] — show recent trade history."""
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        bot_name = args[1] if len(args) > 1 else None
        limit = 10
        if len(args) > 2:
            try:
                limit = int(args[2])
            except ValueError:
                pass
        limit = min(limit, 50)

        orchestrators = self.orchestrators
        if bot_name:
            if bot_name not in orchestrators:
                await message.answer(f"❌ Bot '{bot_name}' not found")
                return
            targets = {bot_name: orchestrators[bot_name]}
        else:
            targets = orchestrators

        for name, orch in targets.items():
            trades: list[str] = []

            # SMC closed positions
            if orch.smc_strategy:
                for pos in reversed(orch.smc_strategy.position_manager.closed_positions[-limit:]):
                    pnl_sign = "+" if pos.realized_pnl >= 0 else ""
                    exit_r = pos.exit_reason or "?"
                    hold = f"{pos.hold_time_hours:.1f}h" if pos.hold_time_hours else "?"
                    trades.append(
                        f"SMC | {pos.entry_price} → {pos.current_price} | "
                        f"{pnl_sign}{pos.realized_pnl} | {exit_r} | {hold}"
                    )

            # Grid filled orders
            if orch.grid_engine:
                for order in reversed(orch.grid_engine.filled_orders[-limit:]):
                    trades.append(
                        f"Grid | {order.side.upper()} @ {order.price} | "
                        f"qty {order.amount} | lvl {order.level}"
                    )

            # TF — no closed position history available
            if orch.trend_follower_strategy:
                pm = orch.trend_follower_strategy.position_manager
                active_count = len(pm.active_positions)
                if active_count > 0:
                    trades.append(f"TF | {active_count} active (no closed history)")

            if not trades:
                await message.answer(f"📜 *{name}*: No trade history", parse_mode="Markdown")
                continue

            response = f"📜 *Trade History: {name}* (last {limit})\n\n"
            for t in trades[:limit]:
                response += f"`{t}`\n"
            await message.answer(response, parse_mode="Markdown")

    async def _cmd_switch_strategy(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 3:
            await message.answer(
                "Usage: /switch\\_strategy <bot\\_name> <strategy\\_id>\n\n"
                "Available strategies: grid, dca, trend\\_follower, smc"
            )
            return
        bot_name, strategy_id = args[1], args[2]
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        orch = self.orchestrators[bot_name]
        try:
            registry_status = orch.strategy_registry.get_registry_status()
            if registry_status.get("active", 0) > 0:
                await orch.strategy_registry.stop_all()
            orch.register_strategy(strategy_id=strategy_id, strategy_type=strategy_id)
            await orch.start_strategy(strategy_id)
            await message.answer(
                f"✅ Strategy switched to *{strategy_id}* for bot *{bot_name}*",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("switch_strategy_failed", bot_name=bot_name, strategy=strategy_id, error=str(e))
            await message.answer(f"❌ Failed to switch strategy: {str(e)}")

    async def _cmd_lock_strategy(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 3:
            await message.answer(
                "Usage: /lock\\_strategy <bot\\_name> <strategy1> [strategy2...]\n\n"
                "Valid strategies: grid, dca, trend\\_follower, smc"
            )
            return
        bot_name = args[1]
        requested = set(args[2:])
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        invalid = requested - self._VALID_STRATEGIES
        if invalid:
            await message.answer(
                f"❌ Invalid strategies: {', '.join(sorted(invalid))}\n"
                f"Valid: {', '.join(sorted(self._VALID_STRATEGIES))}"
            )
            return
        self.orchestrators[bot_name].lock_strategy(requested)
        await message.answer(
            f"🔒 Bot *{bot_name}* locked to: {', '.join(sorted(requested))}",
            parse_mode="Markdown",
        )

    async def _cmd_unlock_strategy(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("⛔ Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 2:
            await message.answer("Usage: /unlock\\_strategy <bot\\_name>")
            return
        bot_name = args[1]
        if bot_name not in self.orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        self.orchestrators[bot_name].unlock_strategy()
        await message.answer(
            f"🔓 Bot *{bot_name}* unlocked — auto mode resumed",
            parse_mode="Markdown",
        )

    async def _cmd_scan(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("Unauthorized access")
            return
        scanner = getattr(self, "_app", None)
        if scanner:
            scanner = getattr(scanner, "_scanner", None)
        if scanner is None:
            await message.answer(
                "Market scanner is not configured. Enable auto_trade in config to use /scan."
            )
            return
        await message.answer("Scanning market... please wait.")
        try:
            results = await scanner.scan()
            if not results:
                await message.answer("No pairs found matching scanner criteria.")
                return
            lines = ["Top pairs from market scan:\n"]
            for i, r in enumerate(results[:5], 1):
                confidence = getattr(r, "confidence", 0.0)
                regime = getattr(r, "regime", "unknown")
                recommendation = getattr(r, "recommended_strategy", "N/A")
                symbol = getattr(r, "symbol", "?")
                lines.append(
                    f"{i}. {symbol}\n"
                    f"   Regime: {regime} | Confidence: {confidence:.0%}\n"
                    f"   Recommendation: {recommendation}\n"
                )
            await message.answer("\n".join(lines))
        except Exception as e:
            logger.error("cmd_scan_failed", error=str(e))
            await message.answer(f"Scan failed: {e}")

    async def _cmd_create_bot(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 3:
            await message.answer(
                "Usage: /create_bot <symbol> <strategy>\n\n"
                "Example: /create_bot ETH/USDT hybrid\n"
                "Strategies: grid, dca, hybrid, trend_follower"
            )
            return
        symbol, strat = args[1], args[2].lower()
        app = getattr(self, "_app", None)
        if app is None:
            await message.answer("BotApplication not linked. Cannot create bot dynamically.")
            return
        template_mgr = getattr(app, "_pair_template_manager", None)
        exchange_client = getattr(app, "_main_exchange_client", None)
        main_config = getattr(app, "_main_config", None)
        if not template_mgr or not exchange_client or not main_config or not main_config.bots:
            await message.answer("Auto-trade is not configured. Enable auto_trade in config.")
            return
        await message.answer(f"Creating bot for {symbol} ({strat})...")
        try:
            base_cfg = main_config.bots[0]
            new_cfg = await template_mgr.create_config(
                symbol=symbol, strategy=strat, exchange_client=exchange_client, base_config=base_cfg,
            )
            orchestrator = await app.add_bot(new_cfg)
            if orchestrator:
                await message.answer(f"Bot '{new_cfg.name}' created and started for {symbol}.")
            else:
                await message.answer(f"Failed to start bot for {symbol}. Check logs.")
        except Exception as e:
            logger.error("cmd_create_bot_failed", symbol=symbol, error=str(e))
            await message.answer(f"Error creating bot: {e}")

    async def _cmd_delete_bot(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("Unauthorized access")
            return
        args = message.text.split() if message.text else []
        if len(args) < 2:
            await message.answer("Usage: /delete_bot <bot_name>\n\nUse /list to see current bots.")
            return
        bot_name = args[1]
        app = getattr(self, "_app", None)
        if app is None:
            await message.answer("BotApplication not linked.")
            return
        if bot_name not in self.orchestrators:
            await message.answer(f"Bot '{bot_name}' not found.")
            return
        await message.answer(f"Stopping and removing bot '{bot_name}'...")
        try:
            await app.remove_bot(bot_name)
            await message.answer(f"Bot '{bot_name}' removed successfully.")
        except Exception as e:
            logger.error("cmd_delete_bot_failed", bot_name=bot_name, error=str(e))
            await message.answer(f"Error removing bot: {e}")

    async def _cmd_portfolio(self, message: Message) -> None:
        if not self._check_auth(message):
            await message.answer("Unauthorized access")
            return
        if not self.orchestrators:
            await message.answer("No active bots.")
            return
        app = getattr(self, "_app", None)
        prm = getattr(app, "_portfolio_risk_manager", None) if app else None
        lines = ["Portfolio Summary\n"]
        for bot_name, orch in self.orchestrators.items():
            try:
                status = orch.get_status()
                symbol = status.get("symbol", "?")
                state = status.get("state", "?")
                lines.append(f"• {bot_name} ({symbol}) — {state}")
            except Exception:
                lines.append(f"• {bot_name} — status unavailable")
        if prm:
            try:
                summary = prm.get_summary()
                lines.append(
                    f"\nCapital Pool:\n"
                    f"  Total: ${summary['total_capital']:,.0f}\n"
                    f"  Allocated: ${summary['pool']['total_allocated']:,.0f} "
                    f"({summary['pool']['utilization_pct']}%)\n"
                    f"  Drawdown: {summary['drawdown_pct']}%\n"
                    f"  Halted: {'YES' if summary['halted'] else 'No'}"
                )
            except Exception as e:
                lines.append(f"\nPortfolio risk manager error: {e}")
        await message.answer("\n".join(lines))

    # ══════════════════════════════════════════════════════════════════════
    # INLINE-BUTTON CALLBACK HANDLERS
    # ══════════════════════════════════════════════════════════════════════

    async def _cb_main_menu(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        await callback.message.edit_text(
            "🤖 <b>TRADERAGENT Bot Manager</b>\n\nВыберите раздел:",
            parse_mode="HTML",
            reply_markup=keyboards.main_menu(),
        )
        await callback.answer()

    async def _cb_control_menu(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        await callback.message.edit_text(
            "🤖 <b>Управление ботами</b>\n\nВыберите бота:",
            parse_mode="HTML",
            reply_markup=keyboards.bot_selector(list(self.orchestrators.keys()), "ctl"),
        )
        await callback.answer()

    async def _cb_monitoring_menu(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        await callback.message.edit_text(
            "📊 <b>Мониторинг</b>\n\nВыберите бота:",
            parse_mode="HTML",
            reply_markup=keyboards.bot_selector(list(self.orchestrators.keys()), "mon"),
        )
        await callback.answer()

    async def _cb_strategy_menu(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        await callback.message.edit_text(
            "⚙️ <b>Стратегии</b>\n\nВыберите бота:",
            parse_mode="HTML",
            reply_markup=keyboards.bot_selector(list(self.orchestrators.keys()), "strat"),
        )
        await callback.answer()

    async def _cb_portfolio_menu(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        await callback.message.edit_text(
            "📈 <b>Портфель</b>",
            parse_mode="HTML",
            reply_markup=keyboards.portfolio_menu(),
        )
        await callback.answer()

    async def _cb_control_bot(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        bot_name = callback.data.split(":")[1]
        if bot_name not in self.orchestrators:
            await callback.answer("Bot not found", show_alert=True)
            return
        orch = self.orchestrators[bot_name]
        se = get_state_emoji(orch.state)
        await callback.message.edit_text(
            f"{se} <b>{bot_name}</b>\nState: {orch.state.value}\nSymbol: {orch.config.symbol}\n\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=keyboards.bot_control(bot_name),
        )
        await callback.answer()

    async def _cb_control_action(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        parts = callback.data.split(":")
        bot_name, action = parts[1], parts[2]
        if bot_name not in self.orchestrators:
            await callback.answer("Bot not found", show_alert=True)
            return
        orch = self.orchestrators[bot_name]
        action_map = {
            "start": (orch.start, "✅ Started"),
            "stop": (orch.stop, "🛑 Stopped"),
            "pause": (orch.pause, "⏸️ Paused"),
            "resume": (orch.resume, "▶️ Resumed"),
        }
        if action not in action_map:
            await callback.answer("Unknown action", show_alert=True)
            return
        func, label = action_map[action]
        try:
            await func()
            await callback.answer(f"{label} {bot_name}")
            se = get_state_emoji(orch.state)
            await callback.message.edit_text(
                f"{se} <b>{bot_name}</b>\nState: {orch.state.value}\nSymbol: {orch.config.symbol}\n\nВыберите действие:",
                parse_mode="HTML",
                reply_markup=keyboards.bot_control(bot_name),
            )
        except Exception as e:
            logger.error("control_action_failed", bot_name=bot_name, action=action, error=str(e))
            await callback.answer(f"❌ {str(e)}", show_alert=True)

    async def _cb_monitoring_bot(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        bot_name = callback.data.split(":")[1]
        if bot_name not in self.orchestrators:
            await callback.answer("Bot not found", show_alert=True)
            return
        orch = self.orchestrators[bot_name]
        se = get_state_emoji(orch.state)
        await callback.message.edit_text(
            f"{se} <b>{bot_name}</b>\nSymbol: {orch.config.symbol}\n\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=keyboards.bot_monitoring(bot_name),
        )
        await callback.answer()

    async def _cb_monitoring_action(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        parts = callback.data.split(":")
        bot_name, action = parts[1], parts[2]
        if bot_name not in self.orchestrators:
            await callback.answer("Bot not found", show_alert=True)
            return
        # Reuse text-command handlers by constructing a message
        msg = callback.message
        msg.text = f"/{action} {bot_name}"
        msg.from_user = callback.from_user
        handler_map = {
            "status": self._cmd_status,
            "balance": self._cmd_balance,
            "pnl": self._cmd_pnl,
            "positions": self._cmd_positions,
            "orders": self._cmd_orders,
            "report": self._cmd_report,
            "trades": self._cmd_trades,
        }
        handler = handler_map.get(action)
        if handler:
            await handler(msg)
        else:
            await callback.answer("Unknown action", show_alert=True)
        await callback.answer()

    async def _cb_strategy_bot(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        bot_name = callback.data.split(":")[1]
        if bot_name not in self.orchestrators:
            await callback.answer("Bot not found", show_alert=True)
            return
        await callback.message.edit_text(
            f"⚙️ <b>Стратегии: {bot_name}</b>\n\nВыберите стратегию:",
            parse_mode="HTML",
            reply_markup=keyboards.bot_strategy(bot_name),
        )
        await callback.answer()

    async def _cb_strategy_switch(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        parts = callback.data.split(":")
        bot_name, strategy_id = parts[1], parts[3]
        if bot_name not in self.orchestrators:
            await callback.answer("Bot not found", show_alert=True)
            return
        orch = self.orchestrators[bot_name]
        try:
            registry_status = orch.strategy_registry.get_registry_status()
            if registry_status.get("active", 0) > 0:
                await orch.strategy_registry.stop_all()
            orch.register_strategy(strategy_id=strategy_id, strategy_type=strategy_id)
            await orch.start_strategy(strategy_id)
            await callback.answer(f"✅ Switched to {strategy_id}")
            await callback.message.edit_text(
                f"✅ Bot <b>{bot_name}</b> → strategy <b>{strategy_id}</b>",
                parse_mode="HTML",
                reply_markup=keyboards.bot_strategy(bot_name),
            )
        except Exception as e:
            logger.error("cb_switch_failed", bot_name=bot_name, strategy=strategy_id, error=str(e))
            await callback.answer(f"❌ {str(e)}", show_alert=True)

    async def _cb_strategy_unlock(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        bot_name = callback.data.split(":")[1]
        if bot_name not in self.orchestrators:
            await callback.answer("Bot not found", show_alert=True)
            return
        self.orchestrators[bot_name].unlock_strategy()
        await callback.answer("🔓 Unlocked")
        await callback.message.edit_text(
            f"🔓 Bot <b>{bot_name}</b> unlocked — auto mode",
            parse_mode="HTML",
            reply_markup=keyboards.bot_strategy(bot_name),
        )

    async def _cb_portfolio_overview(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        msg = callback.message
        msg.text = "/portfolio"
        msg.from_user = callback.from_user
        await self._cmd_portfolio(msg)
        await callback.answer()

    async def _cb_portfolio_report_all(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        msg = callback.message
        msg.text = "/report"
        msg.from_user = callback.from_user
        await self._cmd_report(msg)
        await callback.answer()

    async def _cb_portfolio_scan(self, callback: CallbackQuery) -> None:
        if not self._check_auth_cb(callback):
            await callback.answer("⛔ Unauthorized", show_alert=True)
            return
        msg = callback.message
        msg.text = "/scan"
        msg.from_user = callback.from_user
        await self._cmd_scan(msg)
        await callback.answer()

    # ── Formatting (backward compat) ──────────────────────────────────────

    def _format_status(self, status: dict[str, Any]) -> str:
        return format_status(status)

    def _format_event_notification(self, event: TradingEvent) -> str:
        return format_event_notification(event)

    @staticmethod
    def _get_state_emoji(state: BotState) -> str:
        return get_state_emoji(state)

    # ── Event handling (backward compat for tests) ────────────────────────

    async def _handle_event(self, event: TradingEvent) -> None:
        if event.event_type not in IMPORTANT_EVENTS:
            return
        notification = format_event_notification(event)
        for chat_id in self.allowed_chat_ids:
            try:
                await self.bot.send_message(chat_id=chat_id, text=notification, parse_mode="HTML")
            except Exception:
                try:
                    await self.bot.send_message(chat_id=chat_id, text=notification)
                except Exception as e:
                    logger.error("notification_send_failed", chat_id=chat_id, error=str(e))

    # ── Broadcast ─────────────────────────────────────────────────────────

    async def _broadcast(self, text: str) -> None:
        for chat_id in self.allowed_chat_ids:
            try:
                await self.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                logger.error("broadcast_failed", chat_id=chat_id, error=str(e))

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("starting_telegram_bot")
        await self.bot.set_my_commands([
            BotCommand(command="status", description="Bot status"),
            BotCommand(command="list", description="List all bots"),
            BotCommand(command="start_bot", description="Start a bot"),
            BotCommand(command="stop_bot", description="Stop a bot"),
            BotCommand(command="pause", description="Pause a bot"),
            BotCommand(command="resume", description="Resume a bot"),
            BotCommand(command="switch_strategy", description="Switch strategy"),
            BotCommand(command="lock_strategy", description="Lock strategy"),
            BotCommand(command="unlock_strategy", description="Unlock strategy"),
            BotCommand(command="balance", description="Current balance"),
            BotCommand(command="orders", description="Open orders"),
            BotCommand(command="positions", description="Open positions"),
            BotCommand(command="pnl", description="Profit and Loss"),
            BotCommand(command="report", description="Performance report"),
            BotCommand(command="trades", description="Trade history"),
            BotCommand(command="scan", description="Scan market"),
            BotCommand(command="portfolio", description="Portfolio overview"),
            BotCommand(command="help", description="Help"),
        ])
        self._event_listener.start()
        self.event_listener_task = self._event_listener.task
        await self.dp.start_polling(self.bot)


    async def stop(self) -> None:
        logger.info("stopping_telegram_bot")
        if self.event_listener_task:
            self.event_listener_task.cancel()
            try:
                await self.event_listener_task
            except asyncio.CancelledError:
                pass
        self.event_listener_task = None
        await self.bot.session.close()
        logger.info("telegram_bot_stopped")
