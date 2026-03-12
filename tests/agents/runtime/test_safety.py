from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from bot.agents.runtime.safety import (
    AuditEntry,
    AuditTrail,
    BreakerState,
    CircuitBreaker,
)


# ── CircuitBreaker tests ──────────────────────────────────────────────


class TestCircuitBreakerNormal:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker()
        assert cb.get_state() == BreakerState.CLOSED

    def test_closed_allows_requests(self):
        cb = CircuitBreaker()
        assert cb.is_allowed() is True

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure("err1")
        cb.record_failure("err2")
        cb.record_success()
        cb.record_failure("err3")
        # Only 1 consecutive failure after reset, threshold is 3
        assert cb.get_state() == BreakerState.CLOSED


class TestCircuitBreakerTrip:
    def test_trip_on_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure("a")
        cb.record_failure("b")
        assert cb.get_state() == BreakerState.CLOSED
        cb.record_failure("c")
        assert cb.get_state() == BreakerState.OPEN

    def test_open_blocks_requests(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=9999)
        cb.record_failure("x")
        assert cb.is_allowed() is False

    def test_manual_trip(self):
        cb = CircuitBreaker()
        cb.trip(reason="manual_test")
        assert cb.get_state() == BreakerState.OPEN
        status = cb.get_status()
        assert status["last_failure_reason"] == "manual_test"
        assert status["total_trips"] == 1

    def test_manual_reset(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.get_state() == BreakerState.OPEN
        cb.reset()
        assert cb.get_state() == BreakerState.CLOSED
        assert cb.is_allowed() is True


class TestCircuitBreakerTimeout:
    def test_auto_reset_to_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=0.01)
        cb.record_failure("timeout_test")
        assert cb.get_state() == BreakerState.OPEN
        time.sleep(0.02)
        assert cb.is_allowed() is True
        assert cb.get_state() == BreakerState.HALF_OPEN


class TestCircuitBreakerHalfOpen:
    def test_half_open_success_recovery(self):
        cb = CircuitBreaker(
            failure_threshold=1, reset_timeout_seconds=0.01, half_open_max_trials=2
        )
        cb.record_failure()
        time.sleep(0.02)
        cb.is_allowed()  # transitions to HALF_OPEN
        assert cb.get_state() == BreakerState.HALF_OPEN
        cb.record_half_open_result(True)
        cb.record_half_open_result(True)
        assert cb.get_state() == BreakerState.CLOSED

    def test_half_open_failure_retrips(self):
        cb = CircuitBreaker(
            failure_threshold=1, reset_timeout_seconds=0.01, half_open_max_trials=3
        )
        cb.record_failure()
        time.sleep(0.02)
        cb.is_allowed()
        assert cb.get_state() == BreakerState.HALF_OPEN
        cb.record_half_open_result(False)
        assert cb.get_state() == BreakerState.OPEN

    def test_half_open_allows_requests(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.is_allowed()
        assert cb.get_state() == BreakerState.HALF_OPEN
        assert cb.is_allowed() is True


class TestCircuitBreakerAnomalyPnl:
    def test_anomaly_pnl_trips(self):
        cb = CircuitBreaker(anomaly_pnl_threshold=-0.05)
        cb.record_pnl(-0.06)
        assert cb.get_state() == BreakerState.OPEN
        assert cb.get_status()["last_failure_reason"] == "anomaly_pnl"

    def test_normal_pnl_no_trip(self):
        cb = CircuitBreaker(anomaly_pnl_threshold=-0.05)
        cb.record_pnl(-0.03)
        assert cb.get_state() == BreakerState.CLOSED


class TestCircuitBreakerCallback:
    def test_on_trip_callback_called(self):
        mock_cb = MagicMock()
        cb = CircuitBreaker(failure_threshold=1)
        cb.on_trip_callback = mock_cb
        cb.record_failure("boom")
        mock_cb.assert_called_once_with("boom")

    def test_callback_not_called_when_none(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.on_trip_callback = None
        cb.record_failure("boom")  # should not raise


class TestCircuitBreakerStats:
    def test_get_stats_returns_dict(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure("e1")
        stats = cb.get_stats()
        assert stats["consecutive_failures"] == 1
        assert stats["state"] == "CLOSED"
        assert stats["total_trips"] == 0

    def test_stats_after_trip(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure("e")
        stats = cb.get_stats()
        assert stats["state"] == "OPEN"
        assert stats["total_trips"] == 1
        assert stats["tripped_at"] is not None


# ── AuditTrail tests ──────────────────────────────────────────────────


class TestAuditTrailRecord:
    def test_record_creates_entry(self):
        trail = AuditTrail()
        entry = trail.record("TRADE_EXECUTED", "agent_a", {"price": 100})
        assert isinstance(entry, AuditEntry)
        assert entry.action == "TRADE_EXECUTED"
        assert entry.actor == "agent_a"
        assert entry.details == {"price": 100}
        assert len(entry.entry_id) == 16
        assert len(entry.entry_hash) == 64  # sha256 hex

    def test_genesis_hash(self):
        trail = AuditTrail()
        entry = trail.record("INIT", "system", {})
        assert entry.prev_hash == "genesis"


class TestAuditTrailIntegrity:
    def test_valid_chain(self):
        trail = AuditTrail()
        trail.record("A", "x", {})
        trail.record("B", "y", {"k": 1})
        trail.record("C", "z", {"k": 2})
        result = trail.verify_integrity()
        assert result["valid"] is True
        assert result["entries_checked"] == 3
        assert result["first_invalid"] is None

    def test_tamper_detection_modify_details(self):
        trail = AuditTrail()
        trail.record("A", "x", {})
        trail.record("B", "y", {"amount": 50})
        # Tamper with the second entry's details
        trail._entries[1].details["amount"] = 9999
        result = trail.verify_integrity()
        assert result["valid"] is False
        assert result["first_invalid"] == 1

    def test_tamper_detection_modify_prev_hash(self):
        trail = AuditTrail()
        trail.record("A", "x", {})
        trail.record("B", "y", {})
        trail._entries[1].prev_hash = "tampered"
        result = trail.verify_integrity()
        assert result["valid"] is False

    def test_empty_chain_valid(self):
        trail = AuditTrail()
        result = trail.verify_integrity()
        assert result["valid"] is True
        assert result["entries_checked"] == 0


class TestAuditTrailGetEntries:
    def test_get_entries_limit_offset(self):
        trail = AuditTrail()
        for i in range(10):
            trail.record(f"ACTION_{i}", "bot", {"i": i})
        entries = trail.get_entries(limit=3, offset=2)
        assert len(entries) == 3
        assert entries[0].action == "ACTION_2"

    def test_get_entries_filter_action(self):
        trail = AuditTrail()
        trail.record("TRADE", "a", {})
        trail.record("VOTE", "b", {})
        trail.record("TRADE", "c", {})
        entries = trail.get_entries(action="TRADE")
        assert len(entries) == 2

    def test_get_entries_filter_actor(self):
        trail = AuditTrail()
        trail.record("X", "alice", {})
        trail.record("Y", "bob", {})
        trail.record("Z", "alice", {})
        entries = trail.get_entries(actor="alice")
        assert len(entries) == 2


class TestAuditTrailSearch:
    def test_search_in_action(self):
        trail = AuditTrail()
        trail.record("TRADE_EXECUTED", "bot", {})
        trail.record("STOP_MOVED", "bot", {})
        results = trail.search("TRADE")
        assert len(results) == 1
        assert results[0].action == "TRADE_EXECUTED"

    def test_search_in_details(self):
        trail = AuditTrail()
        trail.record("LOG", "sys", {"msg": "connection_lost"})
        trail.record("LOG", "sys", {"msg": "ok"})
        results = trail.search("connection_lost")
        assert len(results) == 1


class TestAuditTrailExport:
    def test_export_json(self):
        trail = AuditTrail()
        trail.record("A", "x", {"v": 1})
        trail.record("B", "y", {"v": 2})
        exported = trail.export_json()
        assert len(exported) == 2
        assert isinstance(exported[0], dict)
        assert exported[0]["action"] == "A"
        assert exported[1]["prev_hash"] == exported[0]["entry_hash"]


class TestAuditTrailChainHead:
    def test_chain_head(self):
        trail = AuditTrail()
        assert trail.get_chain_head() is None
        trail.record("FIRST", "s", {})
        e2 = trail.record("SECOND", "s", {})
        assert trail.get_chain_head() is e2

    def test_get_entry_by_id(self):
        trail = AuditTrail()
        e = trail.record("TEST", "bot", {"x": 1})
        found = trail.get_entry(e.entry_id)
        assert found is e
        assert trail.get_entry("nonexistent") is None


class TestAuditTrailMaxEntries:
    def test_max_entries_cap(self):
        trail = AuditTrail(max_entries=5)
        for i in range(10):
            trail.record(f"A{i}", "bot", {})
        assert len(trail._entries) == 5
        assert trail._entries[0].action == "A5"


class TestAuditTrailStats:
    def test_stats(self):
        trail = AuditTrail()
        trail.record("TRADE", "alice", {})
        trail.record("VOTE", "bob", {})
        trail.record("TRADE", "alice", {"k": 1})
        stats = trail.get_stats()
        assert stats["total_entries"] == 3
        assert stats["chain_valid"] is True
        assert stats["unique_actors"] == 2
        assert stats["unique_actions"] == 2
        assert stats["first_entry_ts"] is not None
        assert stats["last_entry_ts"] >= stats["first_entry_ts"]


class TestAuditEntryToDict:
    def test_to_dict_roundtrip(self):
        trail = AuditTrail()
        entry = trail.record("ACT", "actor", {"key": "val"})
        d = entry.to_dict()
        assert d["entry_id"] == entry.entry_id
        assert d["entry_hash"] == entry.entry_hash
        assert d["details"] == {"key": "val"}
