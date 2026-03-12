"""
File-based persistence layer for domain events.

Stores events as JSON lines (.jsonl) organized by entity, with a global
append-only log for full replay. Designed for later replacement with a
database-backed store without changing the interface.
"""

import json
import os
from pathlib import Path

from bot.core.event_bus import DomainEvent
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class EventStore:
    """
    Persistent event store using JSON lines files.

    File layout:
        {storage_dir}/{entity_type}/{entity_id}.jsonl  - per-entity timeline
        {storage_dir}/_all.jsonl                        - global append log
    """

    def __init__(self, storage_dir: str = "data/events") -> None:
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        logger.info("event_store_created", storage_dir=str(self._storage_dir))

    async def append(self, event: DomainEvent) -> None:
        """
        Append an event to both the entity-specific file and the global log.

        Writes are synchronous (small JSON lines) but the method is async
        to allow a future migration to async I/O without API changes.
        """
        json_line = event.to_json() + "\n"

        try:
            # Per-entity file
            entity_dir = self._storage_dir / event.entity_type
            entity_dir.mkdir(parents=True, exist_ok=True)
            entity_file = entity_dir / f"{event.entity_id}.jsonl"
            with open(entity_file, "a", encoding="utf-8") as f:
                f.write(json_line)

            # Global log
            all_file = self._storage_dir / "_all.jsonl"
            with open(all_file, "a", encoding="utf-8") as f:
                f.write(json_line)

        except Exception:
            logger.exception(
                "event_store_append_error",
                event_id=event.event_id,
                event_type=event.event_type,
            )

    async def load_timeline(
        self, entity_type: str, entity_id: str
    ) -> list[DomainEvent]:
        """Load all events for a specific entity from its timeline file."""
        entity_file = self._storage_dir / entity_type / f"{entity_id}.jsonl"
        return self._load_file(entity_file)

    async def load_all(self, limit: int = 1000) -> list[DomainEvent]:
        """Load the most recent events from the global log."""
        all_file = self._storage_dir / "_all.jsonl"
        events = self._load_file(all_file)
        if limit > 0:
            return events[-limit:]
        return events

    async def load_since(self, ts: float) -> list[DomainEvent]:
        """Load all events with timestamp >= ts from the global log."""
        all_file = self._storage_dir / "_all.jsonl"
        events = self._load_file(all_file)
        return [e for e in events if e.ts >= ts]

    async def get_stats(self) -> dict:
        """
        Return statistics: total event count and count per entity_type.

        Scans the storage directory structure to count .jsonl files and lines.
        """
        stats: dict = {"total": 0, "by_entity_type": {}}

        try:
            all_file = self._storage_dir / "_all.jsonl"
            if all_file.exists():
                with open(all_file, "r", encoding="utf-8") as f:
                    stats["total"] = sum(1 for line in f if line.strip())

            for entry in sorted(self._storage_dir.iterdir()):
                if entry.is_dir() and entry.name != "__pycache__":
                    count = 0
                    for jsonl_file in entry.glob("*.jsonl"):
                        with open(jsonl_file, "r", encoding="utf-8") as f:
                            count += sum(1 for line in f if line.strip())
                    stats["by_entity_type"][entry.name] = count

        except Exception:
            logger.exception("event_store_stats_error")

        return stats

    def _load_file(self, file_path: Path) -> list[DomainEvent]:
        """Load events from a JSONL file, skipping malformed lines."""
        events: list[DomainEvent] = []
        if not file_path.exists():
            return events

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(DomainEvent.from_json(line))
                    except (json.JSONDecodeError, TypeError, KeyError) as exc:
                        logger.warning(
                            "event_store_parse_error",
                            file=str(file_path),
                            line_num=line_num,
                            error=str(exc),
                        )
        except Exception:
            logger.exception(
                "event_store_load_error",
                file=str(file_path),
            )

        return events
