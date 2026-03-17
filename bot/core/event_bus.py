"""
Async in-process event bus with priority dispatch and event sourcing support.

Provides DomainEvent (extends the spirit of TradingEvent with event sourcing fields)
and EventBus for pub/sub within a single bot process.
"""

import asyncio
import json
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum
from typing import Any

from bot.utils.logger import get_logger

logger = get_logger(__name__)


class EventPriority(IntEnum):
    """Priority levels for event handlers. Lower value = higher priority."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass
class DomainEvent:
    """
    Domain event with event sourcing fields.

    Extends the spirit of TradingEvent from bot/orchestrator/events.py
    but adds causality tracking, priority, and entity identification
    for full event sourcing support.
    """

    entity_type: str
    entity_id: str
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    bot_name: str = ""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = field(default_factory=lambda: time.time())
    causes: list[str] = field(default_factory=list)
    enables: list[str] = field(default_factory=list)
    priority: EventPriority = EventPriority.NORMAL

    def to_json(self) -> str:
        """Serialize event to JSON string."""
        return json.dumps(
            {
                "event_id": self.event_id,
                "entity_type": self.entity_type,
                "entity_id": self.entity_id,
                "event_type": self.event_type,
                "data": self._convert_decimals(self.data),
                "ts": self.ts,
                "causes": self.causes,
                "enables": self.enables,
                "bot_name": self.bot_name,
                "priority": self.priority.value,
            }
        )

    @classmethod
    def from_json(cls, json_str: str) -> "DomainEvent":
        """Deserialize event from JSON string."""
        d = json.loads(json_str)
        d["priority"] = EventPriority(d.get("priority", EventPriority.NORMAL))
        return cls(**d)

    @staticmethod
    def _convert_decimals(data: dict[str, Any]) -> dict[str, Any]:
        """
        Convert Decimal values to strings for JSON serialization.

        Reuses the pattern from TradingEvent._convert_decimals but handles
        nested structures recursively.
        """
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, Decimal):
                result[key] = str(value)
            elif isinstance(value, dict):
                result[key] = DomainEvent._convert_decimals(value)
            elif isinstance(value, list):
                result[key] = [
                    (
                        str(item)
                        if isinstance(item, Decimal)
                        else DomainEvent._convert_decimals(item) if isinstance(item, dict) else item
                    )
                    for item in value
                ]
            else:
                result[key] = value
        return result


class EventBus:
    """
    Async in-process event bus with priority-based dispatch.

    Features:
    - Subscribe by event_type or by entity_type+entity_id
    - Handlers dispatched in priority order (CRITICAL first)
    - Ring buffer of recent events for timeline queries
    - Thread-safe writes via asyncio.Lock
    - Handler errors are logged but never propagate
    """

    def __init__(self, bot_name: str, max_history: int = 10000) -> None:
        self._bot_name = bot_name
        self._max_history = max_history

        # event_type -> list of (priority, handler)
        self._handlers: dict[str, list[tuple[EventPriority, Callable]]] = {}
        # "entity_type:entity_id" -> list of handlers
        self._entity_handlers: dict[str, list[Callable]] = {}
        # "entity_type:entity_id" -> list of events
        self._timelines: dict[str, list[DomainEvent]] = {}
        # Global ring buffer
        self._all_events: deque[DomainEvent] = deque(maxlen=max_history)

        self._lock = asyncio.Lock()

        logger.info("event_bus_created", bot_name=bot_name, max_history=max_history)

    async def publish(self, event: DomainEvent) -> None:
        """
        Publish an event: store in timeline, then dispatch to handlers.

        Handlers are called in priority order. Both sync and async handlers
        are supported. Errors in handlers are logged but never propagate.
        """
        if not event.bot_name:
            event.bot_name = self._bot_name

        timeline_key = f"{event.entity_type}:{event.entity_id}"

        async with self._lock:
            self._all_events.append(event)
            if timeline_key not in self._timelines:
                self._timelines[timeline_key] = []
            self._timelines[timeline_key].append(event)

        # Collect and sort handlers by priority
        handlers_to_call: list[tuple[EventPriority, Callable]] = []

        # Event type handlers
        if event.event_type in self._handlers:
            handlers_to_call.extend(self._handlers[event.event_type])

        # Entity handlers
        if timeline_key in self._entity_handlers:
            for handler in self._entity_handlers[timeline_key]:
                handlers_to_call.append((EventPriority.NORMAL, handler))

        # Sort by priority (lower value = higher priority)
        handlers_to_call.sort(key=lambda h: h[0])

        # Dispatch
        for _priority, handler in handlers_to_call:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "event_handler_error",
                    event_type=event.event_type,
                    event_id=event.event_id,
                    handler=getattr(handler, "__name__", str(handler)),
                )

    def subscribe(
        self,
        event_type: str,
        handler: Callable,
        priority: EventPriority = EventPriority.NORMAL,
    ) -> Callable:
        """
        Subscribe a handler to an event type.

        Args:
            event_type: The event type to subscribe to.
            handler: Sync or async callable accepting a DomainEvent.
            priority: Dispatch priority (CRITICAL dispatched first).

        Returns:
            An unsubscribe callable.
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        entry = (priority, handler)
        self._handlers[event_type].append(entry)

        def unsubscribe() -> None:
            try:
                self._handlers[event_type].remove(entry)
            except ValueError:
                pass

        return unsubscribe

    def subscribe_entity(
        self,
        entity_type: str,
        entity_id: str,
        handler: Callable,
    ) -> Callable:
        """
        Subscribe a handler to all events for a specific entity.

        Args:
            entity_type: Entity type (e.g. "position", "regime").
            entity_id: Entity identifier.
            handler: Sync or async callable accepting a DomainEvent.

        Returns:
            An unsubscribe callable.
        """
        key = f"{entity_type}:{entity_id}"
        if key not in self._entity_handlers:
            self._entity_handlers[key] = []
        self._entity_handlers[key].append(handler)

        def unsubscribe() -> None:
            try:
                self._entity_handlers[key].remove(handler)
            except ValueError:
                pass

        return unsubscribe

    def get_timeline(self, entity_type: str, entity_id: str) -> list[DomainEvent]:
        """Get all events for a specific entity, ordered chronologically."""
        key = f"{entity_type}:{entity_id}"
        return list(self._timelines.get(key, []))

    def get_all_events(self, limit: int = 100, offset: int = 0) -> list[DomainEvent]:
        """Get recent events from the global ring buffer."""
        events = list(self._all_events)
        return events[offset : offset + limit]

    def get_events_since(self, ts: float) -> list[DomainEvent]:
        """Get all events with timestamp >= ts."""
        return [e for e in self._all_events if e.ts >= ts]
