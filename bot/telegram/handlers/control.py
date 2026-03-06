"""Control command handlers: /start, /help, /list, start_bot, stop_bot, pause, resume."""

from typing import cast

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from bot.orchestrator.bot_orchestrator import BotOrchestrator
from bot.telegram import keyboards
from bot.telegram.formatting import get_state_emoji
from bot.utils.logger import get_logger

logger = get_logger(__name__)

router = Router()


def _get_orchestrators(obj: Message | CallbackQuery) -> dict[str, BotOrchestrator]:
    """Retrieve orchestrators dict from the bot instance attached to dispatcher."""
    return cast(dict[str, BotOrchestrator], obj.bot._tg_bot_ref.orchestrators)


def _check_auth(obj: Message | CallbackQuery) -> bool:
    """Check if user is authorized."""
    user = obj.from_user
    if user is None:
        return False
    chat_id = obj.message.chat.id if isinstance(obj, CallbackQuery) else obj.chat.id
    return bool(chat_id in obj.bot._tg_bot_ref.allowed_chat_ids)


# ── Text commands ──────────────────────────────────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start — show main menu with inline buttons."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    await message.answer(
        "🤖 *TRADERAGENT Bot Manager*\n\n" "Выберите раздел:",
        parse_mode="Markdown",
        reply_markup=keyboards.main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help — same as /start."""
    await cmd_start(message)


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    """Handle /list — list all bots."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    orchestrators = _get_orchestrators(message)
    if not orchestrators:
        await message.answer("📋 No bots configured")
        return

    response = "📋 *Available Bots:*\n\n"
    for name, orch in orchestrators.items():
        state_emoji = get_state_emoji(orch.state)
        response += (
            f"{state_emoji} *{name}*\n"
            f"  Symbol: {orch.config.symbol}\n"
            f"  Strategy: {orch.config.strategy}\n"
            f"  State: {orch.state.value}\n\n"
        )

    await message.answer(response, parse_mode="Markdown")


@router.message(Command("start_bot"))
async def cmd_start_bot(message: Message) -> None:
    """Handle /start_bot <name>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    args = message.text.split() if message.text else []
    if len(args) < 2:
        await message.answer("Usage: /start_bot <bot_name>")
        return

    bot_name = args[1]
    orchestrators = _get_orchestrators(message)
    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    try:
        await orchestrators[bot_name].start()
        await message.answer(f"✅ Bot '{bot_name}' started successfully")
    except Exception as e:
        logger.error("start_bot_failed", bot_name=bot_name, error=str(e))
        await message.answer(f"❌ Failed to start bot: {str(e)}")


@router.message(Command("stop_bot"))
async def cmd_stop_bot(message: Message) -> None:
    """Handle /stop_bot <name>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    args = message.text.split() if message.text else []
    if len(args) < 2:
        await message.answer("Usage: /stop_bot <bot_name>")
        return

    bot_name = args[1]
    orchestrators = _get_orchestrators(message)
    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    try:
        await orchestrators[bot_name].stop()
        await message.answer(f"🛑 Bot '{bot_name}' stopped successfully")
    except Exception as e:
        logger.error("stop_bot_failed", bot_name=bot_name, error=str(e))
        await message.answer(f"❌ Failed to stop bot: {str(e)}")


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    """Handle /pause <name>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    args = message.text.split() if message.text else []
    if len(args) < 2:
        await message.answer("Usage: /pause <bot_name>")
        return

    bot_name = args[1]
    orchestrators = _get_orchestrators(message)
    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    try:
        await orchestrators[bot_name].pause()
        await message.answer(f"⏸️ Bot '{bot_name}' paused")
    except Exception as e:
        logger.error("pause_bot_failed", bot_name=bot_name, error=str(e))
        await message.answer(f"❌ Failed to pause bot: {str(e)}")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    """Handle /resume <name>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    args = message.text.split() if message.text else []
    if len(args) < 2:
        await message.answer("Usage: /resume <bot_name>")
        return

    bot_name = args[1]
    orchestrators = _get_orchestrators(message)
    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    try:
        await orchestrators[bot_name].resume()
        await message.answer(f"▶️ Bot '{bot_name}' resumed")
    except Exception as e:
        logger.error("resume_bot_failed", bot_name=bot_name, error=str(e))
        await message.answer(f"❌ Failed to resume bot: {str(e)}")


# ── Callback handlers (inline buttons) ────────────────────────────────────


@router.callback_query(lambda c: c.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery) -> None:
    """Return to main menu."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return
    await callback.message.edit_text(
        "🤖 *TRADERAGENT Bot Manager*\n\nВыберите раздел:",
        parse_mode="Markdown",
        reply_markup=keyboards.main_menu(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "menu:control")
async def cb_control_menu(callback: CallbackQuery) -> None:
    """Show bot selector for control actions."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return
    orchestrators = _get_orchestrators(callback)
    await callback.message.edit_text(
        "🤖 *Управление ботами*\n\nВыберите бота:",
        parse_mode="Markdown",
        reply_markup=keyboards.bot_selector(list(orchestrators.keys()), "ctl"),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("ctl:") and c.data.count(":") == 1)
async def cb_control_bot_selected(callback: CallbackQuery) -> None:
    """Show control actions for a specific bot."""
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
        f"{state_emoji} *{bot_name}*\n"
        f"State: {orch.state.value}\n"
        f"Symbol: {orch.config.symbol}\n\n"
        f"Выберите действие:",
        parse_mode="Markdown",
        reply_markup=keyboards.bot_control(bot_name),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("ctl:") and c.data.count(":") == 2)
async def cb_control_action(callback: CallbackQuery) -> None:
    """Execute a control action (start/stop/pause/resume) via inline button."""
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
        # Refresh the control panel
        state_emoji = get_state_emoji(orch.state)
        await callback.message.edit_text(
            f"{state_emoji} *{bot_name}*\n"
            f"State: {orch.state.value}\n"
            f"Symbol: {orch.config.symbol}\n\n"
            f"Выберите действие:",
            parse_mode="Markdown",
            reply_markup=keyboards.bot_control(bot_name),
        )
    except Exception as e:
        logger.error("control_action_failed", bot_name=bot_name, action=action, error=str(e))
        await callback.answer(f"❌ {str(e)}", show_alert=True)
