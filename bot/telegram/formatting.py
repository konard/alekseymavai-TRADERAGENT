"""Formatting helpers for Telegram bot messages."""

from typing import Any

from bot.orchestrator.bot_orchestrator import BotState
from bot.orchestrator.events import EventType, TradingEvent


def get_state_emoji(state: BotState) -> str:
    """Get emoji for bot state."""
    emoji_map = {
        BotState.STOPPED: "⚫",
        BotState.STARTING: "🟡",
        BotState.RUNNING: "🟢",
        BotState.PAUSED: "🟡",
        BotState.STOPPING: "🟠",
        BotState.EMERGENCY: "🔴",
    }
    return emoji_map.get(state, "⚪")


def format_status(status: dict[str, Any]) -> str:
    """Format bot status as a Markdown message."""
    state_emoji = get_state_emoji(BotState(status["state"]))

    message = (
        f"{state_emoji} *Bot Status: {status['bot_name']}*\n\n"
        f"Symbol: {status['symbol']}\n"
        f"Strategy: {status['strategy']}\n"
        f"State: {status['state']}\n"
        f"Dry Run: {'Yes' if status['dry_run'] else 'No'}\n"
    )

    lock = status.get("strategy_lock", {})
    if lock.get("locked"):
        locked_strats = ", ".join(lock.get("strategies") or [])
        message += f"Mode: LOCKED → {locked_strats}\n"
    else:
        active = status.get("active_strategies", [])
        regime = status.get("market_regime", {})
        regime_name = regime.get("regime", "unknown")
        message += f"Mode: AUTO ({regime_name}) → {', '.join(active)}\n"

    if status.get("current_price"):
        message += f"Current Price: {status['current_price']}\n"

    if "grid" in status:
        grid = status["grid"]
        message += (
            f"\n*Grid Status:*\n"
            f"Active Orders: {grid['active_orders']}\n"
            f"Total Profit: {grid['total_profit']}\n"
        )

    if "dca" in status:
        dca = status["dca"]
        if dca["has_position"]:
            message += (
                f"\n*DCA Position:*\n"
                f"Steps: {dca['current_step']}/{dca['max_steps']}\n"
                f"Avg Entry: {dca['avg_entry_price']}\n"
            )

    if "risk" in status:
        risk = status["risk"]
        message += f"\n*Risk Status:*\nHalted: {'Yes' if risk['halted'] else 'No'}\n"
        if risk["drawdown"]:
            message += f"Drawdown: {float(risk['drawdown']):.2%}\n"

    return message


_EVENT_EMOJI_MAP = {
    EventType.BOT_STARTED: "✅",
    EventType.BOT_STOPPED: "🛑",
    EventType.BOT_EMERGENCY_STOP: "🚨",
    EventType.ORDER_FILLED: "✅",
    EventType.DCA_TRIGGERED: "📉",
    EventType.TAKE_PROFIT_HIT: "💰",
    EventType.RISK_LIMIT_HIT: "⚠️",
    EventType.STOP_LOSS_TRIGGERED: "🛑",
    EventType.ERROR_OCCURRED: "❌",
    EventType.REGIME_CHANGED: "🔄",
    EventType.HYBRID_TRANSITION: "🔀",
    EventType.HEALTH_DEGRADED: "⚠️",
    EventType.HEALTH_CRITICAL: "🚨",
    EventType.STRATEGY_ERROR: "❌",
}

IMPORTANT_EVENTS = {
    EventType.BOT_STARTED,
    EventType.BOT_STOPPED,
    EventType.BOT_EMERGENCY_STOP,
    EventType.ORDER_FILLED,
    EventType.DCA_TRIGGERED,
    EventType.TAKE_PROFIT_HIT,
    EventType.RISK_LIMIT_HIT,
    EventType.STOP_LOSS_TRIGGERED,
    EventType.ERROR_OCCURRED,
    EventType.REGIME_CHANGED,
    EventType.HYBRID_TRANSITION,
    EventType.HEALTH_DEGRADED,
    EventType.HEALTH_CRITICAL,
    EventType.STRATEGY_ERROR,
    EventType.STRATEGY_LOCKED,
    EventType.STRATEGY_UNLOCKED,
}


def format_event_notification(event: TradingEvent) -> str:
    """Format a trading event as a notification message."""
    emoji = _EVENT_EMOJI_MAP.get(event.event_type, "ℹ️")
    title = event.event_type.value.replace("_", " ").title()

    message = f"{emoji} *{title}*\n\n"
    message += f"Bot: {event.bot_name}\n"
    message += f"Time: {event.timestamp}\n"

    if event.data:
        message += "\n*Details:*\n"
        for key, value in event.data.items():
            message += f"{key}: {value}\n"

    return message
