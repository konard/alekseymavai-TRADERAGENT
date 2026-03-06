"""
In-memory Redis stub for replay.

The BotOrchestrator uses Redis only for event pub/sub.  This module
provides lightweight fakes so the orchestrator can initialize and publish
events without a real Redis server.
"""

from __future__ import annotations


class MockPubSub:
    """Fake PubSub that silently drops all messages."""

    async def subscribe(self, *channels: str) -> None:
        pass

    async def unsubscribe(self, *channels: str) -> None:
        pass

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float = 0.0):
        return None

    async def close(self) -> None:
        pass


class MockRedis:
    """
    In-memory Redis replacement.

    Only the methods actually called by ``BotOrchestrator`` are implemented:
    ``ping``, ``publish``, ``pubsub``, ``aclose``.
    """

    def __init__(self) -> None:
        self._published: list[tuple[str, str]] = []

    # -- connection lifecycle ---------------------------------------------

    @classmethod
    def from_url(cls, url: str, **kwargs) -> MockRedis:
        """Drop-in for ``redis.from_url(...)``."""
        return cls()

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    # -- pub/sub ----------------------------------------------------------

    async def publish(self, channel: str, message: str) -> int:
        self._published.append((channel, message))
        return 1

    def pubsub(self) -> MockPubSub:
        return MockPubSub()
