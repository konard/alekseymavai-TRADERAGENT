"""Event system for bot orchestration using Redis Pub/Sub."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Event types for bot orchestration."""

    # Lifecycle events
    BOT_STARTED = "bot_started"
    BOT_STOPPED = "bot_stopped"
    BOT_PAUSED = "bot_paused"
    BOT_RESUMED = "bot_resumed"
    BOT_EMERGENCY_STOP = "bot_emergency_stop"

    # Trading events
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_FAILED = "order_failed"

    # Strategy events
    GRID_INITIALIZED = "grid_initialized"
    GRID_REBALANCED = "grid_rebalanced"
    DCA_TRIGGERED = "dca_triggered"
    TAKE_PROFIT_HIT = "take_profit_hit"

    # Strategy lifecycle events (v2.0)
    STRATEGY_REGISTERED = "strategy_registered"
    STRATEGY_STARTED = "strategy_started"
    STRATEGY_STOPPED = "strategy_stopped"
    STRATEGY_PAUSED = "strategy_paused"
    STRATEGY_RESUMED = "strategy_resumed"
    STRATEGY_ERROR = "strategy_error"
    STRATEGY_RESTARTED = "strategy_restarted"

    # Market regime events (v2.0)
    REGIME_DETECTED = "regime_detected"
    REGIME_CHANGED = "regime_changed"
    STRATEGY_SWITCH_RECOMMENDED = "strategy_switch_recommended"

    # Health events (v2.0)
    HEALTH_CHECK_COMPLETED = "health_check_completed"
    HEALTH_DEGRADED = "health_degraded"
    HEALTH_CRITICAL = "health_critical"

    # Strategy transition events (v2.1)
    STRATEGY_TRANSITION_STARTED = "strategy_transition_started"
    STRATEGY_TRANSITION_COMPLETED = "strategy_transition_completed"

    # Strategy lock events (v2.1)
    STRATEGY_LOCKED = "strategy_locked"
    STRATEGY_UNLOCKED = "strategy_unlocked"

    # Risk events
    RISK_LIMIT_HIT = "risk_limit_hit"
    STOP_LOSS_TRIGGERED = "stop_loss_triggered"
    POSITION_LIMIT_REACHED = "position_limit_reached"

    # Hybrid strategy events (v2.0)
    GRID_BREAKOUT = "grid_breakout"
    HYBRID_MODE_ACTIVATED = "hybrid_mode_activated"
    HYBRID_TRANSITION = "hybrid_transition"

    # Recovery events (v2.2) — DCA cascade when Grid hits lower boundary
    RECOVERY_ENTERED = "recovery_entered"
    RECOVERY_DCA_PLACED = "recovery_dca_placed"
    RECOVERY_TP_HIT = "recovery_tp_hit"
    RECOVERY_TIMEOUT = "recovery_timeout"
    RECOVERY_EXITED = "recovery_exited"

    # Price events
    PRICE_UPDATED = "price_updated"

    # Error events
    ERROR_OCCURRED = "error_occurred"
    EXCHANGE_ERROR = "exchange_error"


@dataclass
class TradingEvent:
    """Trading event data structure."""

    event_type: EventType
    bot_name: str
    timestamp: str
    data: dict[str, Any]

    @classmethod
    def create(
        cls,
        event_type: EventType,
        bot_name: str,
        data: dict[str, Any] | None = None,
    ) -> "TradingEvent":
        """
        Create a new trading event.

        Args:
            event_type: Type of event
            bot_name: Name of the bot
            data: Event-specific data

        Returns:
            TradingEvent instance
        """
        return cls(
            event_type=event_type,
            bot_name=bot_name,
            timestamp=datetime.now(timezone.utc).isoformat(),
            data=data or {},
        )

    def to_json(self) -> str:
        """
        Serialize event to JSON string.

        Returns:
            JSON string representation
        """
        event_dict = asdict(self)
        # Convert EventType enum to string
        event_dict["event_type"] = self.event_type.value
        # Convert Decimal values to strings
        event_dict["data"] = self._convert_decimals(event_dict["data"])
        return json.dumps(event_dict)

    @classmethod
    def from_json(cls, json_str: str) -> "TradingEvent":
        """
        Deserialize event from JSON string.

        Args:
            json_str: JSON string

        Returns:
            TradingEvent instance
        """
        event_dict = json.loads(json_str)
        event_dict["event_type"] = EventType(event_dict["event_type"])
        return cls(**event_dict)

    @staticmethod
    def _convert_decimals(data: dict[str, Any]) -> dict[str, Any]:
        """
        Convert Decimal values to strings for JSON serialization.

        Args:
            data: Data dictionary

        Returns:
            Dictionary with Decimals converted to strings
        """
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, Decimal):
                result[key] = str(value)
            elif isinstance(value, dict):
                result[key] = TradingEvent._convert_decimals(value)
            elif isinstance(value, list):
                result[key] = [str(item) if isinstance(item, Decimal) else item for item in value]
            else:
                result[key] = value
        return result
