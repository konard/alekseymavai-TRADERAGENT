"""Monitoring command handlers: /status, /balance, /orders, /positions, /pnl, /report."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.orchestrator.bot_orchestrator import BotOrchestrator, BotState
from bot.telegram import keyboards
from bot.telegram.formatting import format_status, get_state_emoji
from bot.utils.logger import get_logger

logger = get_logger(__name__)

router = Router()


def _get_orchestrators(obj: Message | CallbackQuery) -> dict[str, BotOrchestrator]:
    return obj.bot._tg_bot_ref.orchestrators  # type: ignore[union-attr]


def _check_auth(obj: Message | CallbackQuery) -> bool:
    user = obj.from_user
    if user is None:
        return False
    chat_id = obj.message.chat.id if isinstance(obj, CallbackQuery) else obj.chat.id
    return chat_id in obj.bot._tg_bot_ref.allowed_chat_ids  # type: ignore[union-attr]


def _parse_bot_name(message: Message) -> str | None:
    """Extract bot_name from command args, or None."""
    args = message.text.split() if message.text else []
    return args[1] if len(args) > 1 else None


def _get_orch(message: Message, bot_name: str) -> BotOrchestrator | None:
    """Get orchestrator by name, sending error if not found. Returns None on miss."""
    orchestrators = _get_orchestrators(message)
    return orchestrators.get(bot_name)


# ── Text commands ──────────────────────────────────────────────────────────


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Handle /status [name]."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    bot_name = _parse_bot_name(message)
    orchestrators = _get_orchestrators(message)

    if bot_name:
        if bot_name not in orchestrators:
            await message.answer(f"❌ Bot '{bot_name}' not found")
            return
        orch = orchestrators[bot_name]
        status = await orch.get_status()
        await message.answer(format_status(status), parse_mode="Markdown")
    else:
        if not orchestrators:
            await message.answer("📋 No bots configured")
            return

        response = "📊 *Bot Status Summary:*\n\n"
        for name, orch in orchestrators.items():
            state_emoji = get_state_emoji(orch.state)
            lock_icon = "🔒" if orch._strategy_locked else "🔄"
            response += f"{state_emoji} *{name}*: {orch.state.value} {lock_icon}\n"
        response += "\nUse `/status <bot_name>` for detailed info"
        await message.answer(response, parse_mode="Markdown")


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    """Handle /balance <name>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    bot_name = _parse_bot_name(message)
    if not bot_name:
        await message.answer("Usage: /balance <bot_name>")
        return

    orchestrators = _get_orchestrators(message)
    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    orch = orchestrators[bot_name]
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


@router.message(Command("orders"))
async def cmd_orders(message: Message) -> None:
    """Handle /orders <name>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    bot_name = _parse_bot_name(message)
    if not bot_name:
        await message.answer("Usage: /orders <bot_name>")
        return

    orchestrators = _get_orchestrators(message)
    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    orch = orchestrators[bot_name]
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


@router.message(Command("pnl"))
async def cmd_pnl(message: Message) -> None:
    """Handle /pnl <name>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    bot_name = _parse_bot_name(message)
    if not bot_name:
        await message.answer("Usage: /pnl <bot_name>")
        return

    orchestrators = _get_orchestrators(message)
    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    orch = orchestrators[bot_name]
    response = f"📊 *P&L for {bot_name}*\n\n"

    # Grid P&L
    if orch.grid_engine:
        grid_status = orch.grid_engine.get_grid_status()
        response += (
            f"*Grid Trading:*\n"
            f"Total Profit: {grid_status['total_profit']}\n"
            f"Buy Count: {grid_status['buy_count']}\n"
            f"Sell Count: {grid_status['sell_count']}\n\n"
        )

    # DCA P&L
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

    # SMC P&L
    if orch.smc_strategy:
        smc_pm = orch.smc_strategy.position_manager
        active = smc_pm.active_positions
        closed = smc_pm.closed_positions
        total_pnl = sum(p.realized_pnl for p in closed) if closed else 0
        response += (
            f"*SMC:*\n"
            f"Active: {len(active)}\n"
            f"Closed: {len(closed)}\n"
            f"Total P&L: {total_pnl}\n\n"
        )

    # Risk Manager P&L
    if orch.risk_manager:
        risk_status = orch.risk_manager.get_risk_status()
        if risk_status["pnl_percentage"] is not None:
            response += f"*Overall:*\nP&L %: {float(risk_status['pnl_percentage']):.2%}\n"

    await message.answer(response, parse_mode="Markdown")


@router.message(Command("positions"))
async def cmd_positions(message: Message) -> None:
    """Handle /positions <name>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    bot_name = _parse_bot_name(message)
    if not bot_name:
        await message.answer("Usage: /positions <bot_name>")
        return

    orchestrators = _get_orchestrators(message)
    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    orch = orchestrators[bot_name]
    response = f"📊 *Positions for {bot_name}*\n\n"
    has_positions = False

    # DCA positions
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

    # Trend-Follower positions
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

    # SMC positions
    if orch.smc_strategy:
        smc_active = orch.smc_strategy.position_manager.active_positions
        if smc_active:
            has_positions = True
            response += f"*SMC ({len(smc_active)} active):*\n"
            for pid, pos in list(smc_active.items())[:5]:
                response += (
                    f"ID: `{pid[:8]}`\n"
                    f"  Type: {pos.signal_type.value}\n"
                    f"  Entry: {pos.entry_price}\n"
                    f"  Size: {pos.size}\n\n"
                )
            if len(smc_active) > 5:
                response += f"... and {len(smc_active) - 5} more positions\n"

    # Grid active orders as proxy for "positions"
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


@router.message(Command("trades"))
async def cmd_trades(message: Message) -> None:
    """Handle /trades [name] [N]."""
    # Delegates to bot.py instance method via callback
    pass


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    """Handle /report [name]."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    bot_name = _parse_bot_name(message)
    orchestrators = _get_orchestrators(message)

    if bot_name and bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    targets = {bot_name: orchestrators[bot_name]} if bot_name else orchestrators

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


# ── Callback handlers (inline buttons) ────────────────────────────────────


@router.callback_query(lambda c: c.data == "menu:monitoring")
async def cb_monitoring_menu(callback: CallbackQuery) -> None:
    """Show bot selector for monitoring."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return
    orchestrators = _get_orchestrators(callback)
    await callback.message.edit_text(
        "📊 *Мониторинг*\n\nВыберите бота:",
        parse_mode="Markdown",
        reply_markup=keyboards.bot_selector(list(orchestrators.keys()), "mon"),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("mon:") and c.data.count(":") == 1)
async def cb_monitoring_bot_selected(callback: CallbackQuery) -> None:
    """Show monitoring actions for a bot."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return
    bot_name = callback.data.split(":")[1]
    orchestrators = _get_orchestrators(callback)
    if bot_name not in orchestrators:
        await callback.answer("Bot not found", show_alert=True)
        return

    orch = orchestrators[bot_name]
    state_emoji = get_state_emoji(orch.state)
    await callback.message.edit_text(
        f"{state_emoji} *{bot_name}*\nSymbol: {orch.config.symbol}\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=keyboards.bot_monitoring(bot_name),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("mon:") and c.data.count(":") == 2)
async def cb_monitoring_action(callback: CallbackQuery) -> None:
    """Execute a monitoring action via inline button."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return

    parts = callback.data.split(":")
    bot_name, action = parts[1], parts[2]
    orchestrators = _get_orchestrators(callback)

    if bot_name not in orchestrators:
        await callback.answer("Bot not found", show_alert=True)
        return

    orch = orchestrators[bot_name]

    # Build a fake message to reuse text-command logic
    fake_msg = callback.message
    fake_msg.text = f"/{action} {bot_name}"
    fake_msg.from_user = callback.from_user
    fake_msg.chat = callback.message.chat
    # Redirect the bot ref
    fake_msg.bot = callback.bot

    handler_map = {
        "status": cmd_status,
        "balance": cmd_balance,
        "pnl": cmd_pnl,
        "positions": cmd_positions,
        "orders": cmd_orders,
        "report": cmd_report,
    }

    handler = handler_map.get(action)
    if handler:
        await handler(fake_msg)
    else:
        await callback.answer("Unknown action", show_alert=True)

    await callback.answer()
