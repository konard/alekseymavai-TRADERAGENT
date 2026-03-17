"""
State projection from domain events (pure reducer functions).

Each reducer takes a list of DomainEvents and returns a projected state dict.
Reducers are pure functions with no side effects — they derive current state
by replaying the event timeline in chronological order.
"""

from collections.abc import Callable
from typing import Any

from bot.core.event_bus import DomainEvent


def reduce_position(events: list[DomainEvent]) -> dict[str, Any]:
    """Project position state from event timeline."""
    state: dict[str, Any] = {
        "status": "unknown",
        "entry_price": None,
        "exit_price": None,
        "pnl": None,
        "direction": None,
        "amount": None,
        "strategy": None,
        "stop_loss": None,
        "take_profit": None,
        "created_at": None,
        "closed_at": None,
        "updates": 0,
    }
    for evt in sorted(events, key=lambda e: e.ts):
        if evt.event_type == "SIGNAL_GENERATED":
            state["direction"] = evt.data.get("direction")
            state["strategy"] = evt.data.get("strategy")
            state["status"] = "signal"
        elif evt.event_type == "ORDER_PLACED":
            state["status"] = "pending"
            state["amount"] = evt.data.get("amount")
        elif evt.event_type == "POSITION_OPENED":
            state["status"] = "open"
            state["entry_price"] = evt.data.get("entry_price")
            state["stop_loss"] = evt.data.get("stop_loss")
            state["take_profit"] = evt.data.get("take_profit")
            state["created_at"] = evt.ts
        elif evt.event_type == "POSITION_UPDATED":
            state["updates"] += 1
            # Update only fields that exist in state
            state.update({k: v for k, v in evt.data.items() if k in state})
        elif evt.event_type == "POSITION_CLOSED":
            state["status"] = "closed"
            state["exit_price"] = evt.data.get("exit_price")
            state["pnl"] = evt.data.get("pnl")
            state["closed_at"] = evt.ts
        elif evt.event_type in ("RISK_CHECK_REJECTED", "ORDER_FAILED"):
            state["status"] = "rejected"
    return state


def reduce_regime(events: list[DomainEvent]) -> dict[str, Any]:
    """Project regime state from event timeline."""
    state: dict[str, Any] = {
        "current_regime": None,
        "previous_regime": None,
        "phase": "stable",
        "confidence": 0.0,
        "transitions": 0,
        "last_transition_at": None,
        "pre_switch_target": None,
        "cancelled_switches": 0,
    }
    for evt in sorted(events, key=lambda e: e.ts):
        if evt.event_type == "REGIME_DETECTED":
            state["current_regime"] = evt.data.get("regime")
            state["confidence"] = evt.data.get("confidence", 0.0)
        elif evt.event_type == "PRE_SWITCH_ENTERED":
            state["phase"] = "pre_switch"
            state["pre_switch_target"] = evt.data.get("target_regime")
        elif evt.event_type == "PRE_SWITCH_CANCELLED":
            state["phase"] = "stable"
            state["pre_switch_target"] = None
            state["cancelled_switches"] += 1
        elif evt.event_type == "SWITCH_CONFIRMED":
            state["phase"] = "confirmed"
        elif evt.event_type == "TRANSITION_COMPLETE":
            state["phase"] = "stable"
            state["previous_regime"] = state["current_regime"]
            state["current_regime"] = evt.data.get("new_regime")
            state["transitions"] += 1
            state["last_transition_at"] = evt.ts
            state["pre_switch_target"] = None
    return state


def reduce_portfolio(events: list[DomainEvent]) -> dict[str, Any]:
    """Project portfolio state from event timeline."""
    state: dict[str, Any] = {
        "balance": 0.0,
        "initial_balance": 0.0,
        "peak_balance": 0.0,
        "total_pnl": 0.0,
        "total_fees": 0.0,
        "open_positions": 0,
        "closed_positions": 0,
        "wins": 0,
        "losses": 0,
        "max_drawdown": 0.0,
    }
    for evt in sorted(events, key=lambda e: e.ts):
        if evt.event_type == "BALANCE_UPDATED":
            state["balance"] = evt.data.get("balance", state["balance"])
            if state["initial_balance"] == 0:
                state["initial_balance"] = state["balance"]
            if state["balance"] > state["peak_balance"]:
                state["peak_balance"] = state["balance"]
            if state["peak_balance"] > 0:
                dd = (state["peak_balance"] - state["balance"]) / state["peak_balance"]
                if dd > state["max_drawdown"]:
                    state["max_drawdown"] = dd
        elif evt.event_type == "PNL_REALIZED":
            pnl = evt.data.get("pnl", 0.0)
            state["total_pnl"] += pnl
            if pnl > 0:
                state["wins"] += 1
            elif pnl < 0:
                state["losses"] += 1
        elif evt.event_type == "FEE_CHARGED":
            state["total_fees"] += evt.data.get("fee", 0.0)
        elif evt.event_type == "POSITION_OPENED":
            state["open_positions"] += 1
        elif evt.event_type == "POSITION_CLOSED":
            state["open_positions"] = max(0, state["open_positions"] - 1)
            state["closed_positions"] += 1
    return state


def reduce_strategy(events: list[DomainEvent]) -> dict[str, Any]:
    """Project strategy state from event timeline."""
    state: dict[str, Any] = {
        "status": "inactive",
        "signals_generated": 0,
        "trades_executed": 0,
        "errors": 0,
        "activated_at": None,
        "deactivated_at": None,
        "last_signal_at": None,
    }
    for evt in sorted(events, key=lambda e: e.ts):
        if evt.event_type == "STRATEGY_ACTIVATED":
            state["status"] = "active"
            state["activated_at"] = evt.ts
        elif evt.event_type == "STRATEGY_DEACTIVATED":
            state["status"] = "inactive"
            state["deactivated_at"] = evt.ts
        elif evt.event_type == "STRATEGY_SIGNAL":
            state["signals_generated"] += 1
            state["last_signal_at"] = evt.ts
        elif evt.event_type == "STRATEGY_ERROR":
            state["errors"] += 1
    return state


# ---------------------------------------------------------------------------
# Reducer registry
# ---------------------------------------------------------------------------
REDUCERS: dict[str, Callable[[list[DomainEvent]], dict[str, Any]]] = {
    "position": reduce_position,
    "regime": reduce_regime,
    "portfolio": reduce_portfolio,
    "strategy": reduce_strategy,
}


def get_state_at(
    events: list[DomainEvent],
    entity_type: str,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """
    Get state of entity at a specific point in time (temporal replay).

    Args:
        events: Full event list (will be filtered by entity_type).
        entity_type: The entity type to project.
        timestamp: If provided, only events at or before this time are
                   included. If None, all events are used.

    Returns:
        Projected state dict, or empty dict if no reducer exists.
    """
    filtered = [e for e in events if e.entity_type == entity_type]
    if timestamp is not None:
        filtered = [e for e in filtered if e.ts <= timestamp]
    reducer = REDUCERS.get(entity_type)
    if reducer is None:
        return {}
    return reducer(filtered)
