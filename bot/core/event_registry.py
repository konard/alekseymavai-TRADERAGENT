"""
Complete event type catalog for the TRADERAGENT event ontology.

Defines all recognized domain events organized by lifecycle domain,
with causality chains (causes/enables) for state machine validation.
"""

from typing import Any

# ---------------------------------------------------------------------------
# Trade lifecycle events
# ---------------------------------------------------------------------------
TRADE_LIFECYCLE_EVENTS: dict[str, dict[str, Any]] = {
    "SIGNAL_GENERATED": {
        "label": "Сигнал сгенерирован",
        "entity_type": "position",
        "enables": ["RISK_CHECK_PASSED", "RISK_CHECK_REJECTED"],
    },
    "RISK_CHECK_PASSED": {
        "label": "Риск-проверка пройдена",
        "entity_type": "position",
        "causes": ["SIGNAL_GENERATED"],
        "enables": ["ORDER_PLACED"],
    },
    "RISK_CHECK_REJECTED": {
        "label": "Риск-проверка отклонена",
        "entity_type": "position",
        "causes": ["SIGNAL_GENERATED"],
    },
    "ORDER_PLACED": {
        "label": "Ордер размещён",
        "entity_type": "position",
        "causes": ["RISK_CHECK_PASSED"],
        "enables": ["ORDER_FILLED", "ORDER_FAILED"],
    },
    "ORDER_FILLED": {
        "label": "Ордер исполнен",
        "entity_type": "position",
        "causes": ["ORDER_PLACED"],
        "enables": ["POSITION_OPENED"],
    },
    "ORDER_FAILED": {
        "label": "Ордер отклонён",
        "entity_type": "position",
        "causes": ["ORDER_PLACED"],
    },
    "POSITION_OPENED": {
        "label": "Позиция открыта",
        "entity_type": "position",
        "causes": ["ORDER_FILLED"],
        "enables": [
            "POSITION_UPDATED",
            "EXIT_SIGNAL",
            "STOP_LOSS_HIT",
            "TAKE_PROFIT_HIT",
        ],
    },
    "POSITION_UPDATED": {
        "label": "Позиция обновлена",
        "entity_type": "position",
        "causes": ["POSITION_OPENED"],
    },
    "EXIT_SIGNAL": {
        "label": "Сигнал на выход",
        "entity_type": "position",
        "causes": ["POSITION_OPENED"],
        "enables": ["POSITION_CLOSED"],
    },
    "STOP_LOSS_HIT": {
        "label": "Стоп-лосс сработал",
        "entity_type": "position",
        "causes": ["POSITION_OPENED"],
        "enables": ["POSITION_CLOSED"],
    },
    "TAKE_PROFIT_HIT": {
        "label": "Тейк-профит сработал",
        "entity_type": "position",
        "causes": ["POSITION_OPENED"],
        "enables": ["POSITION_CLOSED"],
    },
    "POSITION_CLOSED": {
        "label": "Позиция закрыта",
        "entity_type": "position",
        "causes": ["EXIT_SIGNAL", "STOP_LOSS_HIT", "TAKE_PROFIT_HIT"],
    },
}

# ---------------------------------------------------------------------------
# Regime events
# ---------------------------------------------------------------------------
REGIME_EVENTS: dict[str, dict[str, Any]] = {
    "REGIME_DETECTED": {
        "label": "Режим определён",
        "entity_type": "regime",
    },
    "REGIME_CHANGE_CANDIDATE": {
        "label": "Кандидат на смену режима",
        "entity_type": "regime",
        "causes": ["REGIME_DETECTED"],
        "enables": ["PRE_SWITCH_ENTERED"],
    },
    "PRE_SWITCH_ENTERED": {
        "label": "PRE_SWITCH фаза",
        "entity_type": "regime",
        "causes": ["REGIME_CHANGE_CANDIDATE"],
        "enables": ["SWITCH_CONFIRMED", "PRE_SWITCH_CANCELLED"],
    },
    "PRE_SWITCH_CANCELLED": {
        "label": "PRE_SWITCH отменён",
        "entity_type": "regime",
        "causes": ["PRE_SWITCH_ENTERED"],
    },
    "SWITCH_CONFIRMED": {
        "label": "Смена подтверждена",
        "entity_type": "regime",
        "causes": ["PRE_SWITCH_ENTERED"],
        "enables": ["DIRECTIVE_DISPATCHED"],
    },
    "DIRECTIVE_DISPATCHED": {
        "label": "Директива отправлена",
        "entity_type": "regime",
        "causes": ["SWITCH_CONFIRMED"],
        "enables": ["TRANSITION_COMPLETE"],
    },
    "TRANSITION_COMPLETE": {
        "label": "Переход завершён",
        "entity_type": "regime",
        "causes": ["DIRECTIVE_DISPATCHED"],
    },
}

# ---------------------------------------------------------------------------
# Strategy events
# ---------------------------------------------------------------------------
STRATEGY_EVENTS: dict[str, dict[str, Any]] = {
    "STRATEGY_ACTIVATED": {
        "label": "Стратегия активирована",
        "entity_type": "strategy",
    },
    "STRATEGY_DEACTIVATED": {
        "label": "Стратегия деактивирована",
        "entity_type": "strategy",
    },
    "STRATEGY_SIGNAL": {
        "label": "Сигнал стратегии",
        "entity_type": "strategy",
        "enables": ["SIGNAL_GENERATED"],
    },
    "STRATEGY_ERROR": {
        "label": "Ошибка стратегии",
        "entity_type": "strategy",
    },
    "STRATEGY_WARMUP_COMPLETE": {
        "label": "Прогрев завершён",
        "entity_type": "strategy",
    },
}

# ---------------------------------------------------------------------------
# Risk events
# ---------------------------------------------------------------------------
RISK_EVENTS: dict[str, dict[str, Any]] = {
    "DAILY_LOSS_LIMIT_HIT": {
        "label": "Дневной лимит убытков",
        "entity_type": "risk",
    },
    "PORTFOLIO_STOP_LOSS": {
        "label": "Стоп-лосс портфеля",
        "entity_type": "risk",
    },
    "RISK_HALTED": {
        "label": "Торговля остановлена",
        "entity_type": "risk",
    },
    "RISK_RESUMED": {
        "label": "Торговля возобновлена",
        "entity_type": "risk",
    },
    "TRADE_REJECTED": {
        "label": "Сделка отклонена",
        "entity_type": "risk",
        "causes": ["SIGNAL_GENERATED"],
    },
}

# ---------------------------------------------------------------------------
# Portfolio events
# ---------------------------------------------------------------------------
PORTFOLIO_EVENTS: dict[str, dict[str, Any]] = {
    "BALANCE_UPDATED": {
        "label": "Баланс обновлён",
        "entity_type": "portfolio",
    },
    "DRAWDOWN_UPDATED": {
        "label": "Просадка обновлена",
        "entity_type": "portfolio",
    },
    "PNL_REALIZED": {
        "label": "P&L реализован",
        "entity_type": "portfolio",
    },
    "FEE_CHARGED": {
        "label": "Комиссия списана",
        "entity_type": "portfolio",
    },
}

# ---------------------------------------------------------------------------
# Bot lifecycle events
# ---------------------------------------------------------------------------
BOT_LIFECYCLE_EVENTS: dict[str, dict[str, Any]] = {
    "BOT_STARTED": {
        "label": "Бот запущен",
        "entity_type": "bot",
    },
    "BOT_STOPPED": {
        "label": "Бот остановлен",
        "entity_type": "bot",
    },
    "BOT_PAUSED": {
        "label": "Бот на паузе",
        "entity_type": "bot",
    },
    "BOT_RESUMED": {
        "label": "Бот возобновлён",
        "entity_type": "bot",
    },
    "BOT_ERROR": {
        "label": "Ошибка бота",
        "entity_type": "bot",
    },
    "BOT_EMERGENCY_STOP": {
        "label": "Аварийная остановка",
        "entity_type": "bot",
    },
}

# ---------------------------------------------------------------------------
# Unified registry
# ---------------------------------------------------------------------------
EVENT_REGISTRY: dict[str, dict[str, Any]] = {}
EVENT_REGISTRY.update(TRADE_LIFECYCLE_EVENTS)
EVENT_REGISTRY.update(REGIME_EVENTS)
EVENT_REGISTRY.update(STRATEGY_EVENTS)
EVENT_REGISTRY.update(RISK_EVENTS)
EVENT_REGISTRY.update(PORTFOLIO_EVENTS)
EVENT_REGISTRY.update(BOT_LIFECYCLE_EVENTS)


def get_event_meta(event_type: str) -> dict[str, Any]:
    """
    Return metadata for a registered event type.

    Returns an empty dict if the event type is not registered.
    """
    return dict(EVENT_REGISTRY.get(event_type, {}))


def get_valid_transitions(event_type: str) -> list[str]:
    """Return event types that this event enables (valid next steps)."""
    meta = EVENT_REGISTRY.get(event_type, {})
    return list(meta.get("enables", []))


def get_causes(event_type: str) -> list[str]:
    """Return event types that can cause this event."""
    meta = EVENT_REGISTRY.get(event_type, {})
    return list(meta.get("causes", []))


def get_entity_events(entity_type: str) -> list[str]:
    """Return all registered event types for a given entity type."""
    return [
        event_type
        for event_type, meta in EVENT_REGISTRY.items()
        if meta.get("entity_type") == entity_type
    ]
