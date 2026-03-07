"""Strategy command handlers: /switch_strategy, /lock_strategy, /unlock_strategy."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.orchestrator.bot_orchestrator import BotOrchestrator
from bot.telegram import keyboards
from bot.utils.logger import get_logger

logger = get_logger(__name__)

router = Router()

_VALID_STRATEGIES = {"grid", "dca", "trend_follower", "smc"}


def _get_orchestrators(obj: Message | CallbackQuery) -> dict[str, BotOrchestrator]:
    return obj.bot._tg_bot_ref.orchestrators  # type: ignore[no-any-return]


def _check_auth(obj: Message | CallbackQuery) -> bool:
    user = obj.from_user
    if user is None:
        return False
    chat_id = obj.message.chat.id if isinstance(obj, CallbackQuery) else obj.chat.id
    return chat_id in obj.bot._tg_bot_ref.allowed_chat_ids


# ── Text commands ──────────────────────────────────────────────────────────


@router.message(Command("switch_strategy"))
async def cmd_switch_strategy(message: Message) -> None:
    """Handle /switch_strategy <bot_name> <strategy>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    args = message.text.split() if message.text else []
    if len(args) < 3:
        await message.answer(
            "Usage: /switch\\_strategy <bot\\_name> <strategy\\_id>\n\n"
            "Available strategies: grid, dca, trend\\_follower, smc"
        )
        return

    bot_name = args[1]
    strategy_id = args[2]
    orchestrators = _get_orchestrators(message)

    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    orch = orchestrators[bot_name]
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
        logger.error(
            "switch_strategy_failed", bot_name=bot_name, strategy=strategy_id, error=str(e)
        )
        await message.answer(f"❌ Failed to switch strategy: {str(e)}")


@router.message(Command("lock_strategy"))
async def cmd_lock_strategy(message: Message) -> None:
    """Handle /lock_strategy <bot_name> <strategy1> [strategy2...]."""
    if not _check_auth(message):
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
    orchestrators = _get_orchestrators(message)

    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    invalid = requested - _VALID_STRATEGIES
    if invalid:
        await message.answer(
            f"❌ Invalid strategies: {', '.join(sorted(invalid))}\n"
            f"Valid: {', '.join(sorted(_VALID_STRATEGIES))}"
        )
        return

    orch = orchestrators[bot_name]
    orch.lock_strategy(requested)
    await message.answer(
        f"🔒 Bot *{bot_name}* locked to: {', '.join(sorted(requested))}",
        parse_mode="Markdown",
    )


@router.message(Command("unlock_strategy"))
async def cmd_unlock_strategy(message: Message) -> None:
    """Handle /unlock_strategy <bot_name>."""
    if not _check_auth(message):
        await message.answer("⛔ Unauthorized access")
        return

    args = message.text.split() if message.text else []
    if len(args) < 2:
        await message.answer("Usage: /unlock\\_strategy <bot\\_name>")
        return

    bot_name = args[1]
    orchestrators = _get_orchestrators(message)

    if bot_name not in orchestrators:
        await message.answer(f"❌ Bot '{bot_name}' not found")
        return

    orch = orchestrators[bot_name]
    orch.unlock_strategy()
    await message.answer(
        f"🔓 Bot *{bot_name}* unlocked — auto mode resumed",
        parse_mode="Markdown",
    )


# ── Callback handlers (inline buttons) ────────────────────────────────────


@router.callback_query(lambda c: c.data == "menu:strategy")
async def cb_strategy_menu(callback: CallbackQuery) -> None:
    """Show bot selector for strategy management."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return
    orchestrators = _get_orchestrators(callback)
    await callback.message.edit_text(
        "⚙️ *Стратегии*\n\nВыберите бота:",
        parse_mode="Markdown",
        reply_markup=keyboards.bot_selector(list(orchestrators.keys()), "strat"),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("strat:") and c.data.count(":") == 1)
async def cb_strategy_bot_selected(callback: CallbackQuery) -> None:
    """Show strategy actions for a bot."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return
    bot_name = callback.data.split(":")[1]
    orchestrators = _get_orchestrators(callback)
    if bot_name not in orchestrators:
        await callback.answer("Bot not found", show_alert=True)
        return

    await callback.message.edit_text(
        f"⚙️ *Стратегии: {bot_name}*\n\nВыберите стратегию для переключения:",
        parse_mode="Markdown",
        reply_markup=keyboards.bot_strategy(bot_name),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("strat:") and ":switch:" in c.data)
async def cb_strategy_switch(callback: CallbackQuery) -> None:
    """Switch strategy via inline button."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return

    parts = callback.data.split(":")
    bot_name = parts[1]
    strategy_id = parts[3]
    orchestrators = _get_orchestrators(callback)

    if bot_name not in orchestrators:
        await callback.answer("Bot not found", show_alert=True)
        return

    orch = orchestrators[bot_name]
    try:
        registry_status = orch.strategy_registry.get_registry_status()
        if registry_status.get("active", 0) > 0:
            await orch.strategy_registry.stop_all()
        orch.register_strategy(strategy_id=strategy_id, strategy_type=strategy_id)
        await orch.start_strategy(strategy_id)
        await callback.answer(f"✅ Switched to {strategy_id}")
        await callback.message.edit_text(
            f"✅ Bot *{bot_name}* → strategy *{strategy_id}*",
            parse_mode="Markdown",
            reply_markup=keyboards.bot_strategy(bot_name),
        )
    except Exception as e:
        logger.error("cb_switch_failed", bot_name=bot_name, strategy=strategy_id, error=str(e))
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(
    lambda c: c.data and c.data.startswith("strat:") and c.data.endswith(":unlock")
)
async def cb_strategy_unlock(callback: CallbackQuery) -> None:
    """Unlock strategy via inline button."""
    if not _check_auth(callback):
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return

    bot_name = callback.data.split(":")[1]
    orchestrators = _get_orchestrators(callback)

    if bot_name not in orchestrators:
        await callback.answer("Bot not found", show_alert=True)
        return

    orchestrators[bot_name].unlock_strategy()
    await callback.answer("🔓 Unlocked")
    await callback.message.edit_text(
        f"🔓 Bot *{bot_name}* unlocked — auto mode",
        parse_mode="Markdown",
        reply_markup=keyboards.bot_strategy(bot_name),
    )
