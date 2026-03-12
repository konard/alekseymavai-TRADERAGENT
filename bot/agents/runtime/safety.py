from __future__ import annotations

import enum
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable


class BreakerState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 300,
        half_open_max_trials: int = 3,
        anomaly_pnl_threshold: float = -0.05,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self.half_open_max_trials = half_open_max_trials
        self.anomaly_pnl_threshold = anomaly_pnl_threshold

        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_reason = ""
        self._tripped_at: float | None = None
        self._total_trips = 0
        self._created_at = time.time()

        self._half_open_successes = 0

        self.on_trip_callback: Callable | None = None

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self, error: str = "") -> None:
        self._consecutive_failures += 1
        self._last_failure_reason = error or "failure"
        if self._consecutive_failures >= self.failure_threshold:
            self.trip(reason=self._last_failure_reason)

    def record_pnl(self, pnl_pct: float) -> None:
        if pnl_pct < self.anomaly_pnl_threshold:
            self.trip(reason="anomaly_pnl")

    def is_allowed(self) -> bool:
        if self._state == BreakerState.CLOSED:
            return True
        if self._state == BreakerState.OPEN:
            if self._tripped_at is not None and (
                time.time() - self._tripped_at >= self.reset_timeout_seconds
            ):
                self._state = BreakerState.HALF_OPEN
                self._half_open_successes = 0
                return True
            return False
        # HALF_OPEN
        return True

    def record_half_open_result(self, success: bool) -> None:
        if self._state != BreakerState.HALF_OPEN:
            return
        if not success:
            self._state = BreakerState.OPEN
            self._tripped_at = time.time()
            return
        self._half_open_successes += 1
        if self._half_open_successes >= self.half_open_max_trials:
            self.reset()

    def trip(self, reason: str = "manual") -> None:
        if self._state != BreakerState.OPEN:
            self._total_trips += 1
        self._state = BreakerState.OPEN
        self._tripped_at = time.time()
        self._last_failure_reason = reason
        if self.on_trip_callback is not None:
            self.on_trip_callback(reason)

    def reset(self) -> None:
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._tripped_at = None
        self._half_open_successes = 0

    def get_state(self) -> BreakerState:
        return self._state

    def get_status(self) -> dict:
        now = time.time()
        total_time = now - self._created_at
        # Approximate uptime: we don't track historical state durations,
        # so report whether we are currently up.
        uptime_pct = 1.0 if self._state == BreakerState.CLOSED else 0.0
        return {
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "last_failure_reason": self._last_failure_reason,
            "tripped_at": self._tripped_at,
            "total_trips": self._total_trips,
            "uptime_pct": uptime_pct,
        }

    def get_stats(self) -> dict:
        return self.get_status()


@dataclass
class AuditEntry:
    entry_id: str
    ts: float
    action: str
    actor: str
    details: dict
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "ts": self.ts,
            "action": self.action,
            "actor": self.actor,
            "details": self.details,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


def _compute_hash(
    entry_id: str, ts: float, action: str, actor: str, details: dict, prev_hash: str
) -> str:
    details_str = json.dumps(details, sort_keys=True)
    raw = f"{entry_id}:{ts}:{action}:{actor}:{details_str}:{prev_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()


class AuditTrail:
    def __init__(self, max_entries: int = 10000) -> None:
        self.max_entries = max_entries
        self._entries: list[AuditEntry] = []
        self._index: dict[str, int] = {}  # entry_id -> position

    def record(self, action: str, actor: str, details: dict) -> AuditEntry:
        entry_id = uuid.uuid4().hex[:16]
        ts = time.time()
        prev_hash = self._entries[-1].entry_hash if self._entries else "genesis"
        entry_hash = _compute_hash(entry_id, ts, action, actor, details, prev_hash)
        entry = AuditEntry(
            entry_id=entry_id,
            ts=ts,
            action=action,
            actor=actor,
            details=details,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        if len(self._entries) >= self.max_entries:
            removed = self._entries.pop(0)
            self._index.pop(removed.entry_id, None)
            # Rebuild index after shift
            self._index = {e.entry_id: i for i, e in enumerate(self._entries)}
        self._index[entry_id] = len(self._entries)
        self._entries.append(entry)
        return entry

    def verify_integrity(self) -> dict:
        if not self._entries:
            return {"valid": True, "entries_checked": 0, "first_invalid": None}
        for i, entry in enumerate(self._entries):
            prev_hash = self._entries[i - 1].entry_hash if i > 0 else "genesis"
            if entry.prev_hash != prev_hash:
                return {"valid": False, "entries_checked": i + 1, "first_invalid": i}
            expected = _compute_hash(
                entry.entry_id, entry.ts, entry.action, entry.actor,
                entry.details, entry.prev_hash,
            )
            if entry.entry_hash != expected:
                return {"valid": False, "entries_checked": i + 1, "first_invalid": i}
        return {"valid": True, "entries_checked": len(self._entries), "first_invalid": None}

    def get_entries(
        self,
        limit: int = 100,
        offset: int = 0,
        action: str | None = None,
        actor: str | None = None,
    ) -> list[AuditEntry]:
        filtered = self._entries
        if action is not None:
            filtered = [e for e in filtered if e.action == action]
        if actor is not None:
            filtered = [e for e in filtered if e.actor == actor]
        return filtered[offset : offset + limit]

    def get_entry(self, entry_id: str) -> AuditEntry | None:
        idx = self._index.get(entry_id)
        if idx is not None and idx < len(self._entries):
            return self._entries[idx]
        return None

    def get_chain_head(self) -> AuditEntry | None:
        return self._entries[-1] if self._entries else None

    def search(self, query: str) -> list[AuditEntry]:
        q = query.lower()
        results = []
        for entry in self._entries:
            text = f"{entry.action} {entry.actor} {json.dumps(entry.details)}".lower()
            if q in text:
                results.append(entry)
        return results

    def export_json(self) -> list[dict]:
        return [e.to_dict() for e in self._entries]

    def get_stats(self) -> dict:
        integrity = self.verify_integrity()
        return {
            "total_entries": len(self._entries),
            "chain_valid": integrity["valid"],
            "first_entry_ts": self._entries[0].ts if self._entries else None,
            "last_entry_ts": self._entries[-1].ts if self._entries else None,
            "unique_actors": len({e.actor for e in self._entries}),
            "unique_actions": len({e.action for e in self._entries}),
        }
