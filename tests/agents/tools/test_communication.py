"""Tests for bot.agents.tools.communication module."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from bot.agents.tools.communication import (
    ConsensusSkill,
    DebateProtocol,
    EscalationSkill,
    MessageBus,
)


# ===== DebateProtocol ========================================================


class TestDebateProtocol:
    def test_start_debate_returns_id(self):
        dp = DebateProtocol()
        debate_id = dp.start_debate("BTC direction", "smc")
        assert debate_id.startswith("debate_")

    def test_ask_and_answer_question(self):
        dp = DebateProtocol()
        did = dp.start_debate("ETH trend", "smc")
        qid = dp.ask_question(did, "smc", "trend", "Is ETH bullish?")
        assert qid.startswith("q_")
        dp.answer_question(did, qid, "Yes, EMA crossover confirms.", {"ema": 3200})

        transcript = dp.get_debate_transcript(did)
        assert len(transcript) == 1
        assert transcript[0]["answer"] == "Yes, EMA crossover confirms."
        assert transcript[0]["answer_data"] == {"ema": 3200}

    def test_transcript_ordering(self):
        dp = DebateProtocol()
        did = dp.start_debate("ordering", "a")
        q1 = dp.ask_question(did, "a", "b", "first?")
        q2 = dp.ask_question(did, "b", "a", "second?")
        q3 = dp.ask_question(did, "a", "c", "third?")
        transcript = dp.get_debate_transcript(did)
        assert [t["question_id"] for t in transcript] == [q1, q2, q3]

    def test_close_debate_summary(self):
        dp = DebateProtocol()
        did = dp.start_debate("summary test", "smc")
        dp.ask_question(did, "smc", "trend", "q1")
        dp.ask_question(did, "trend", "risk", "q2")
        summary = dp.close_debate(did)
        assert summary["topic"] == "summary test"
        assert summary["question_count"] == 2
        assert set(summary["participants"]) == {"smc", "trend", "risk"}
        assert summary["duration"] >= 0

    def test_close_debate_prevents_further_questions(self):
        dp = DebateProtocol()
        did = dp.start_debate("closed", "x")
        dp.close_debate(did)
        with pytest.raises(ValueError, match="already closed"):
            dp.ask_question(did, "x", "y", "too late?")

    def test_auto_close_on_expiry(self):
        dp = DebateProtocol(max_duration_seconds=0.0)
        did = dp.start_debate("expire", "a")
        # Even a tiny sleep ensures expiry
        time.sleep(0.01)
        with pytest.raises(ValueError, match="auto-closed"):
            dp.ask_question(did, "a", "b", "expired?")

    def test_answer_unknown_question_raises(self):
        dp = DebateProtocol()
        did = dp.start_debate("t", "a")
        with pytest.raises(KeyError):
            dp.answer_question(did, "nonexistent_q", "nope")

    def test_unknown_debate_raises(self):
        dp = DebateProtocol()
        with pytest.raises(KeyError):
            dp.get_debate_transcript("bogus")


# ===== ConsensusSkill ========================================================


class TestConsensusSkill:
    def _experts(self, *specs):
        return [
            {"name": n, "position": p, "confidence": c}
            for n, p, c in specs
        ]

    def test_unanimous_long(self):
        cs = ConsensusSkill()
        result = cs.execute({
            "experts": self._experts(
                ("smc", "long", 0.9),
                ("trend", "long", 0.8),
                ("risk", "long", 0.7),
            ),
        })
        assert result.success
        assert result.data["consensus"] == "long"
        assert result.data["agreement_ratio"] == 1.0
        assert len(result.data["dissent"]) == 0

    def test_split_vote_majority_wins(self):
        cs = ConsensusSkill()
        result = cs.execute({
            "experts": self._experts(
                ("smc", "long", 0.8),
                ("trend", "long", 0.7),
                ("risk", "short", 0.5),
            ),
        })
        assert result.data["consensus"] == "long"
        assert set(result.data["coalition"]) == {"smc", "trend"}
        assert len(result.data["dissent"]) == 1
        assert result.data["dissent"][0]["name"] == "risk"

    def test_no_consensus_when_all_disagree(self):
        cs = ConsensusSkill()
        result = cs.execute({
            "experts": self._experts(
                ("smc", "long", 0.8),
                ("trend", "short", 0.7),
                ("risk", "neutral", 0.5),
            ),
        })
        # Each group has 1/3 < 0.5 → no consensus
        assert result.data["consensus"] == "none"

    def test_confidence_weighting(self):
        cs = ConsensusSkill()
        result = cs.execute({
            "experts": self._experts(
                ("smc", "long", 0.9),
                ("trend", "long", 0.5),
            ),
        })
        assert result.data["coalition_confidence"] == pytest.approx(0.7)

    def test_dissent_list_contents(self):
        cs = ConsensusSkill()
        result = cs.execute({
            "experts": self._experts(
                ("a", "long", 0.9),
                ("b", "long", 0.8),
                ("c", "short", 0.6),
            ),
        })
        dissent = result.data["dissent"]
        assert len(dissent) == 1
        assert dissent[0]["position"] == "short"
        assert dissent[0]["confidence"] == 0.6

    def test_empty_experts(self):
        cs = ConsensusSkill()
        result = cs.execute({"experts": []})
        assert not result.success

    def test_two_way_tie_picks_one(self):
        """When two positions tie, one is picked but ratio < 0.5 → none."""
        cs = ConsensusSkill()
        result = cs.execute({
            "experts": self._experts(
                ("a", "long", 0.9),
                ("b", "short", 0.9),
            ),
        })
        # 1/2 = 0.5, so >= 0.5 threshold is met
        assert result.data["consensus"] in ("long", "short")
        assert result.data["agreement_ratio"] == 0.5


# ===== EscalationSkill =======================================================


class TestEscalationSkill:
    def test_create_escalation(self):
        es = EscalationSkill()
        result = es.execute({
            "reason": "SMC vs Trend conflict",
            "expert_opinions": {"smc": "long", "trend": "short"},
            "urgency": "high",
            "context": {"symbol": "BTCUSDT"},
        })
        assert result.success
        eid = result.data["escalation_id"]
        assert eid.startswith("esc_")
        assert "high" in result.data["summary"]

    def test_callback_invoked(self):
        cb = MagicMock()
        es = EscalationSkill(escalation_callback=cb)
        es.execute({"reason": "test", "urgency": "critical"})
        cb.assert_called_once()
        arg = cb.call_args[0][0]
        assert arg["urgency"] == "critical"

    def test_pending_and_resolve(self):
        es = EscalationSkill()
        r1 = es.execute({"reason": "r1", "urgency": "low"})
        r2 = es.execute({"reason": "r2", "urgency": "medium"})
        assert len(es.get_pending_escalations()) == 2

        es.resolve_escalation(r1.data["escalation_id"], "Handled by trader")
        pending = es.get_pending_escalations()
        assert len(pending) == 1
        assert pending[0]["escalation_id"] == r2.data["escalation_id"]

    def test_resolve_unknown_raises(self):
        es = EscalationSkill()
        with pytest.raises(KeyError):
            es.resolve_escalation("esc_unknown", "nope")

    def test_urgency_levels(self):
        es = EscalationSkill()
        for level in ("low", "medium", "high", "critical"):
            result = es.execute({"reason": f"test {level}", "urgency": level})
            assert result.success
            assert level in result.data["summary"]


# ===== MessageBus ============================================================


class TestMessageBus:
    def test_register_and_get_agents(self):
        bus = MessageBus()
        bus.register_agent("smc")
        bus.register_agent("trend")
        assert bus.get_agents() == ["smc", "trend"]

    def test_send_and_receive(self):
        bus = MessageBus()
        bus.register_agent("a")
        bus.register_agent("b")
        mid = bus.send("a", "b", "signal", {"direction": "long"})
        assert mid.startswith("msg_")

        inbox = bus.get_inbox("b")
        assert len(inbox) == 1
        assert inbox[0]["from"] == "a"
        assert inbox[0]["payload"]["direction"] == "long"

    def test_mark_read(self):
        bus = MessageBus()
        bus.register_agent("a")
        mid = bus.send("x", "a", "info", {})
        bus.mark_read(mid)
        assert bus.get_inbox("a") == []

    def test_broadcast(self):
        bus = MessageBus()
        for name in ("a", "b", "c"):
            bus.register_agent(name)
        ids = bus.broadcast("a", "alert", {"msg": "heads up"})
        assert len(ids) == 2  # b and c
        assert len(bus.get_inbox("b")) == 1
        assert len(bus.get_inbox("c")) == 1
        assert bus.get_inbox("a") == []  # sender excluded

    def test_empty_inbox(self):
        bus = MessageBus()
        bus.register_agent("lonely")
        assert bus.get_inbox("lonely") == []

    def test_ttl_expiry(self):
        bus = MessageBus(default_ttl=0.0)
        bus.register_agent("a")
        bus.send("x", "a", "old", {})
        time.sleep(0.01)
        assert bus.get_inbox("a") == []

    def test_multiple_messages_ordering(self):
        bus = MessageBus()
        bus.register_agent("r")
        bus.send("s1", "r", "t", {"seq": 1})
        bus.send("s2", "r", "t", {"seq": 2})
        inbox = bus.get_inbox("r")
        assert inbox[0]["payload"]["seq"] == 1
        assert inbox[1]["payload"]["seq"] == 2

    def test_unregistered_agent_can_receive(self):
        """Messages to unregistered agents still get stored."""
        bus = MessageBus()
        bus.send("a", "ghost", "ping", {})
        assert len(bus.get_inbox("ghost")) == 1
