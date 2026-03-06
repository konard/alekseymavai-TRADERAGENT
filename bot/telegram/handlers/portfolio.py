"""Portfolio command handlers: /scan, /create_bot, /delete_bot, /portfolio."""

from typing import Any

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.orchestrator.bot_orchestrator import BotOrchestrator
from bot.telegram import keyboards
from bot.utils.logger import get_logger

logger = get_logger(__name__)

router = Router()


def _get_orchestrators(obj: Message | CallbackQuery) -> dict[str, BotOrchestrator]:
    return dict(obj.bot._tg_bot_ref.orchestrators)


def _check_auth(obj: Message | CallbackQuery) -> bool:
    user = obj.from_user
    if user is None:
        return False
    chat_id = obj.message.chat.id if isinstance(obj, CallbackQuery) else obj.chat.id
    return chat_id in obj.bot._tg_bot_ref.allowed_chat_ids


def _get_app(obj: Message | CallbackQuery) -> Any:
    """Get BotApplication reference if available."""
    return getattr(obj.bot, "_tg_app_ref", None)


@router.message(Command("scan"))
async def cmd_scan(message: Message) -> None:
    """Handle /scan — market scanner."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    app = _get_app(message)
    scanner = getattr(app, "_scanner", None) if app else None

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


@router.message(Command("create_bot"))
async def cmd_create_bot(message: Message) -> None:
    """Handle /create_bot <symbol> <strategy>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    args = message.text.split() if message.text else []
    if len(args) < 3:
        await message.answer(
            "Usage: /create_bot <symbol> <strategy>\n\n"
            "Example: /create_bot ETH/USDT hybrid\n"
            "Strategies: grid, dca, hybrid, trend_follower"
        )
        return

    symbol = args[1]
    strategy = args[2].lower()

    app = _get_app(message)
    if app is None:
        await message.answer("BotApplication not linked. Cannot create bot dynamically.")
        return

    template_mgr = getattr(app, "_pair_template_manager", None)
    exchange_client = getattr(app, "_main_exchange_client", None)
    main_config = getattr(app, "_main_config", None)

    if not template_mgr or not exchange_client or not main_config or not main_config.bots:
        await message.answer("Auto-trade is not configured. Enable auto_trade in config.")
        return

    await message.answer(f"Creating bot for {symbol} ({strategy})...")
    try:
        base_cfg = main_config.bots[0]
        new_cfg = await template_mgr.create_config(
            symbol=symbol,
            strategy=strategy,
            exchange_client=exchange_client,
            base_config=base_cfg,
        )
        orchestrator = await app.add_bot(new_cfg)
        if orchestrator:
            await message.answer(f"Bot '{new_cfg.name}' created and started for {symbol}.")
        else:
            await message.answer(f"Failed to start bot for {symbol}. Check logs.")
    except Exception as e:
        logger.error("cmd_create_bot_failed", symbol=symbol, error=str(e))
        await message.answer(f"Error creating bot: {e}")


@router.message(Command("delete_bot"))
async def cmd_delete_bot(message: Message) -> None:
    """Handle /delete_bot <name>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    args = message.text.split() if message.text else []
    if len(args) < 2:
        await message.answer("Usage: /delete_bot <bot_name>\n\nUse /list to see current bots.")
        return

    bot_name = args[1]
    app = _get_app(message)
    orchestrators = _get_orchestrators(message)

    if app is None:
        await message.answer("BotApplication not linked.")
        return

    if bot_name not in orchestrators:
        await message.answer(f"Bot '{bot_name}' not found.")
        return

    await message.answer(f"Stopping and removing bot '{bot_name}'...")
    try:
        await app.remove_bot(bot_name)
        await message.answer(f"Bot '{bot_name}' removed successfully.")
    except Exception as e:
        logger.error("cmd_delete_bot_failed", bot_name=bot_name, error=str(e))
        await message.answer(f"Error removing bot: {e}")


@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message) -> None:
    """Handle /portfolio — portfolio overview."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    orchestrators = _get_orchestrators(message)
    if not orchestrators:
        await message.answer("No active bots.")
        return

    app = _get_app(message)
    prm = getattr(app, "_portfolio_risk_manager", None) if app else None

    lines = ["Portfolio Summary\n"]

    for bot_name, orch in orchestrators.items():
        try:
            status = await orch.get_status()
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


# ── Callback handlers ─────────────────────────────────────────────────────


@router.callback_query(lambda c: c.data == "menu:portfolio")
async def cb_portfolio_menu(callback: CallbackQuery) -> None:
    """Show portfolio section."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return
    await callback.message.edit_text(
        "📈 *Портфель*",
        parse_mode="Markdown",
        reply_markup=keyboards.portfolio_menu(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "port:overview")
async def cb_portfolio_overview(callback: CallbackQuery) -> None:
    """Portfolio overview via button."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return

    fake_msg = callback.message
    fake_msg.text = "/portfolio"
    fake_msg.from_user = callback.from_user
    fake_msg.bot = callback.bot
    await cmd_portfolio(fake_msg)
    await callback.answer()


@router.callback_query(lambda c: c.data == "port:report_all")
async def cb_portfolio_report_all(callback: CallbackQuery) -> None:
    """Report for all bots via button."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return

    from bot.telegram.handlers.monitoring import cmd_report

    fake_msg = callback.message
    fake_msg.text = "/report"
    fake_msg.from_user = callback.from_user
    fake_msg.bot = callback.bot
    await cmd_report(fake_msg)
    await callback.answer()


@router.callback_query(lambda c: c.data == "port:scan")
async def cb_portfolio_scan(callback: CallbackQuery) -> None:
    """Scan market via button."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return

    fake_msg = callback.message
    fake_msg.text = "/scan"
    fake_msg.from_user = callback.from_user
    fake_msg.bot = callback.bot
    await cmd_scan(fake_msg)
    await callback.answer()
