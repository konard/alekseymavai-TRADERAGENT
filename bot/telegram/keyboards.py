"""Inline keyboard builders for Telegram bot navigation."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    """Build the main menu keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🤖 Управление", callback_data="menu:control"),
                InlineKeyboardButton(text="📊 Мониторинг", callback_data="menu:monitoring"),
            ],
            [
                InlineKeyboardButton(text="⚙️ Стратегии", callback_data="menu:strategy"),
                InlineKeyboardButton(text="📈 Портфель", callback_data="menu:portfolio"),
            ],
        ]
    )


def bot_selector(bot_names: list[str], action_prefix: str) -> InlineKeyboardMarkup:
    """Build a keyboard with one button per bot + Back.

    callback_data format: ``{action_prefix}:{bot_name}``
    """
    rows: list[list[InlineKeyboardButton]] = []
    for name in sorted(bot_names):
        rows.append([InlineKeyboardButton(text=name, callback_data=f"{action_prefix}:{name}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bot_control(bot_name: str) -> InlineKeyboardMarkup:
    """Control actions for a specific bot."""
    prefix = f"ctl:{bot_name}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Старт", callback_data=f"{prefix}:start"),
                InlineKeyboardButton(text="⏸ Пауза", callback_data=f"{prefix}:pause"),
            ],
            [
                InlineKeyboardButton(text="▶️ Возобн.", callback_data=f"{prefix}:resume"),
                InlineKeyboardButton(text="⏹ Стоп", callback_data=f"{prefix}:stop"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:control")],
        ]
    )


def bot_monitoring(bot_name: str) -> InlineKeyboardMarkup:
    """Monitoring actions for a specific bot."""
    prefix = f"mon:{bot_name}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Статус", callback_data=f"{prefix}:status"),
                InlineKeyboardButton(text="💰 Баланс", callback_data=f"{prefix}:balance"),
            ],
            [
                InlineKeyboardButton(text="📊 PnL", callback_data=f"{prefix}:pnl"),
                InlineKeyboardButton(text="📦 Позиции", callback_data=f"{prefix}:positions"),
            ],
            [
                InlineKeyboardButton(text="📋 Ордера", callback_data=f"{prefix}:orders"),
                InlineKeyboardButton(text="📈 Отчёт", callback_data=f"{prefix}:report"),
            ],
            [
                InlineKeyboardButton(text="📜 Сделки", callback_data=f"{prefix}:trades"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:monitoring")],
        ]
    )


def bot_strategy(bot_name: str) -> InlineKeyboardMarkup:
    """Strategy actions for a specific bot."""
    prefix = f"strat:{bot_name}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Grid", callback_data=f"{prefix}:switch:grid"),
                InlineKeyboardButton(text="🔄 DCA", callback_data=f"{prefix}:switch:dca"),
            ],
            [
                InlineKeyboardButton(text="🔄 TF", callback_data=f"{prefix}:switch:trend_follower"),
                InlineKeyboardButton(text="🔄 SMC", callback_data=f"{prefix}:switch:smc"),
            ],
            [
                InlineKeyboardButton(text="🔓 Unlock", callback_data=f"{prefix}:unlock"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:strategy")],
        ]
    )


def portfolio_menu() -> InlineKeyboardMarkup:
    """Portfolio section keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Обзор портфеля", callback_data="port:overview")],
            [InlineKeyboardButton(text="📈 Отчёт по всем", callback_data="port:report_all")],
            [InlineKeyboardButton(text="🔍 Сканировать рынок", callback_data="port:scan")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )


def back_button(callback_data: str = "menu:main") -> InlineKeyboardMarkup:
    """Single back button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)],
        ]
    )
