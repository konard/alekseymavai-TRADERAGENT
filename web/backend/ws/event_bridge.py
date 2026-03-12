"""EventBus -> WebSocket bridge for real-time event streaming.

Unlike RedisBridge (which requires Redis pub/sub), this bridge connects
directly to an in-process EventBus instance, providing zero-latency
event streaming to WebSocket clients.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from bot.core.event_bus import DomainEvent, EventBus
from bot.utils.logger import get_logger
from web.backend.ws.manager import ConnectionManager

if TYPE_CHECKING:
    from typing import Callable

logger = get_logger(__name__)


class EventBusBridge:
    """Bridges in-process EventBus events to WebSocket clients.

    Subscribes to every known event type on the EventBus and forwards
    each DomainEvent as a structured JSON message to all connected
    WebSocket clients via the ConnectionManager.

    The bridge uses the EventBus's native subscribe() API.  Because
    EventBus does not support glob/wildcard subscriptions, we keep a
    background task that periodically discovers new event types and
    subscribes to them.  For immediate coverage, all events published
    through the bus's ``publish()`` path are also caught by monkey-
    patching ``_on_any_publish`` in ``start()``.
    """

    def __init__(self, event_bus: EventBus, manager: ConnectionManager) -> None:
        self._bus = event_bus
        self._manager = manager
        self._unsubscribers: list[Callable] = []
        self._original_publish = None
        self._running = False

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Install an around-advice on EventBus.publish to intercept all events."""
        self._running = True

        # Wrap the bus's publish method so we see every event regardless of type
        original_publish = self._bus.publish

        async def _intercepted_publish(event: DomainEvent) -> None:
            await original_publish(event)
            try:
                await self._on_event(event)
            except Exception:
                logger.exception(
                    "event_bridge_forward_error",
                    event_id=event.event_id,
                    event_type=event.event_type,
                )

        self._original_publish = original_publish
        self._bus.publish = _intercepted_publish  # type: ignore[method-assign]

        logger.info("event_bus_bridge_started")

    async def stop(self) -> None:
        """Restore the original publish method and clean up."""
        self._running = False

        # Restore original publish
        if self._original_publish is not None:
            self._bus.publish = self._original_publish  # type: ignore[method-assign]
            self._original_publish = None

        # Clean up any direct subscriptions
        for unsub in self._unsubscribers:
            try:
                unsub()
            except Exception:
                pass
        self._unsubscribers.clear()

        logger.info("event_bus_bridge_stopped")

    # ── event forwarding ────────────────────────────────────────────────

    async def _on_event(self, event: DomainEvent) -> None:
        """Forward a single DomainEvent to all WebSocket clients."""
        if not self._running:
            return

        ws_message = {
            "type": "domain_event",
            "event": {
                "event_id": event.event_id,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "event_type": event.event_type,
                "data": event.data,
                "ts": event.ts,
                "causes": event.causes,
                "enables": event.enables,
                "bot_name": event.bot_name,
                "priority": (
                    event.priority.value
                    if hasattr(event.priority, "value")
                    else event.priority
                ),
            },
        }

        # Broadcast to all connected WebSocket clients
        await self._manager.broadcast(ws_message)

        # Also broadcast to entity-specific channel so clients that
        # subscribed to a particular entity receive targeted updates
        entity_channel = f"events:{event.entity_type}:{event.entity_id}"
        await self._manager.broadcast(ws_message, channel=entity_channel)
