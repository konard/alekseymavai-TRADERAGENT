"""Redis Pub/Sub event listener and notification dispatcher."""

import asyncio

import redis.asyncio as redis
from aiogram import Bot

from bot.orchestrator.events import TradingEvent
from bot.telegram.formatting import IMPORTANT_EVENTS, format_event_notification
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class EventListener:
    """Listens to Redis trading events and broadcasts notifications to Telegram."""

    def __init__(
        self,
        bot: Bot,
        allowed_chat_ids: set[int],
        bot_names: list[str],
        redis_url: str,
    ):
        self.bot = bot
        self.allowed_chat_ids = allowed_chat_ids
        self.bot_names = bot_names
        self.redis_url = redis_url
        self.redis_client: redis.Redis | None = None
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the event listener as a background task."""
        self.task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        """Cancel the listener task and clean up."""
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _listen(self) -> None:
        """Listen to Redis events and send notifications."""
        logger.info("event_listener_started")

        self.redis_client = redis.from_url(self.redis_url, encoding="utf-8", decode_responses=True)

        pubsub = self.redis_client.pubsub()
        for bot_name in self.bot_names:
            channel = f"trading_events:{bot_name}"
            await pubsub.subscribe(channel)

        logger.info("subscribed_to_events", bot_count=len(self.bot_names))

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    event = TradingEvent.from_json(message["data"])
                    await self._handle_event(event)
                except Exception as e:
                    logger.error("event_handling_failed", error=str(e))
        except asyncio.CancelledError:
            logger.info("event_listener_cancelled")
        finally:
            await pubsub.unsubscribe()
            await self.redis_client.aclose()

        logger.info("event_listener_stopped")

    async def _handle_event(self, event: TradingEvent) -> None:
        """Handle a trading event — send notification if important."""
        if event.event_type not in IMPORTANT_EVENTS:
            return

        notification = format_event_notification(event)

        for chat_id in self.allowed_chat_ids:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=notification,
                    parse_mode="Markdown",
                )
            except Exception:
                try:
                    await self.bot.send_message(chat_id=chat_id, text=notification)
                except Exception as e:
                    logger.error("notification_send_failed", chat_id=chat_id, error=str(e))
