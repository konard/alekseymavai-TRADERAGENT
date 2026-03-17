"""Communication skills for inter-agent interaction.

Provides:
- DebateProtocol  — structured debates between experts
- ConsensusSkill  — coalition-forming consensus tool
- EscalationSkill — escalation / human-review trigger
- MessageBus      — agent-to-agent messaging with TTL
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from bot.agents.tools.market_data import BaseTool, ToolResult


# ---------------------------------------------------------------------------
# DebateProtocol
# ---------------------------------------------------------------------------


@dataclass
class _Question:
    question_id: str
    from_expert: str
    to_expert: str
    question: str
    answer: str | None = None
    answer_data: dict[str, Any] | None = None
    asked_at: float = field(default_factory=time.time)
    answered_at: float | None = None


@dataclass
class _Debate:
    debate_id: str
    topic: str
    initiator: str
    started_at: float = field(default_factory=time.time)
    closed: bool = False
    closed_at: float | None = None
    max_duration_seconds: float = 300.0
    questions: list[_Question] = field(default_factory=list)

    @property
    def participants(self) -> set[str]:
        parts: set[str] = {self.initiator}
        for q in self.questions:
            parts.add(q.from_expert)
            parts.add(q.to_expert)
        return parts

    def is_expired(self) -> bool:
        return time.time() - self.started_at >= self.max_duration_seconds


class DebateProtocol:
    """Manages structured debates between experts."""

    def __init__(self, max_duration_seconds: float = 300.0) -> None:
        self._debates: dict[str, _Debate] = {}
        self._default_max_duration = max_duration_seconds

    # -- public API ----------------------------------------------------------

    def start_debate(self, topic: str, initiator: str) -> str:
        debate_id = f"debate_{uuid.uuid4().hex[:12]}"
        self._debates[debate_id] = _Debate(
            debate_id=debate_id,
            topic=topic,
            initiator=initiator,
            max_duration_seconds=self._default_max_duration,
        )
        return debate_id

    def ask_question(
        self,
        debate_id: str,
        from_expert: str,
        to_expert: str,
        question: str,
    ) -> str:
        debate = self._get_open_debate(debate_id)
        question_id = f"q_{uuid.uuid4().hex[:12]}"
        debate.questions.append(
            _Question(
                question_id=question_id,
                from_expert=from_expert,
                to_expert=to_expert,
                question=question,
            )
        )
        return question_id

    def answer_question(
        self,
        debate_id: str,
        question_id: str,
        answer: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        debate = self._get_open_debate(debate_id)
        for q in debate.questions:
            if q.question_id == question_id:
                q.answer = answer
                q.answer_data = data
                q.answered_at = time.time()
                return
        raise KeyError(f"Question {question_id} not found in debate {debate_id}")

    def get_debate_transcript(self, debate_id: str) -> list[dict[str, Any]]:
        debate = self._get_debate(debate_id)
        transcript: list[dict[str, Any]] = []
        for q in debate.questions:
            entry: dict[str, Any] = {
                "question_id": q.question_id,
                "from": q.from_expert,
                "to": q.to_expert,
                "question": q.question,
                "asked_at": q.asked_at,
            }
            if q.answer is not None:
                entry["answer"] = q.answer
                entry["answer_data"] = q.answer_data
                entry["answered_at"] = q.answered_at
            transcript.append(entry)
        return transcript

    def close_debate(self, debate_id: str) -> dict[str, Any]:
        debate = self._get_debate(debate_id)
        now = time.time()
        debate.closed = True
        debate.closed_at = now
        return {
            "debate_id": debate_id,
            "topic": debate.topic,
            "participants": sorted(debate.participants),
            "question_count": len(debate.questions),
            "duration": now - debate.started_at,
        }

    # -- internal helpers ----------------------------------------------------

    def _get_debate(self, debate_id: str) -> _Debate:
        if debate_id not in self._debates:
            raise KeyError(f"Debate {debate_id} not found")
        return self._debates[debate_id]

    def _get_open_debate(self, debate_id: str) -> _Debate:
        debate = self._get_debate(debate_id)
        if debate.closed:
            raise ValueError(f"Debate {debate_id} is already closed")
        if debate.is_expired():
            self.close_debate(debate_id)
            raise ValueError(f"Debate {debate_id} auto-closed (exceeded max duration)")
        return debate


# ---------------------------------------------------------------------------
# ConsensusSkill
# ---------------------------------------------------------------------------


class ConsensusSkill(BaseTool):
    """Forms coalitions between experts who agree on a position."""

    name = "form_consensus"
    description = "Group experts by position and determine consensus"

    def execute(self, params: dict[str, Any]) -> ToolResult:
        experts: list[dict[str, Any]] = params.get("experts", [])
        if not experts:
            return ToolResult(success=False, error="No experts provided")

        # Group by position
        groups: dict[str, list[dict[str, Any]]] = {}
        for exp in experts:
            pos = exp.get("position", "neutral")
            groups.setdefault(pos, []).append(exp)

        # Find largest coalition
        best_position: str = "none"
        best_group: list[dict[str, Any]] = []
        for pos, members in groups.items():
            if len(members) > len(best_group):
                best_group = members
                best_position = pos

        coalition_names = [e["name"] for e in best_group]
        agreement_ratio = len(best_group) / len(experts) if experts else 0.0

        # Weighted-average confidence of the coalition
        total_conf = sum(e.get("confidence", 0.0) for e in best_group)
        coalition_confidence = total_conf / len(best_group) if best_group else 0.0

        # Dissenters
        dissent: list[dict[str, Any]] = []
        for exp in experts:
            if exp["name"] not in coalition_names:
                dissent.append(
                    {
                        "name": exp["name"],
                        "position": exp.get("position", "neutral"),
                        "confidence": exp.get("confidence", 0.0),
                    }
                )

        consensus = best_position if agreement_ratio >= 0.5 else "none"

        return ToolResult(
            success=True,
            data={
                "consensus": consensus,
                "coalition": coalition_names,
                "coalition_confidence": coalition_confidence,
                "dissent": dissent,
                "agreement_ratio": agreement_ratio,
            },
        )


# ---------------------------------------------------------------------------
# EscalationSkill
# ---------------------------------------------------------------------------


class EscalationSkill(BaseTool):
    """Triggers escalation when experts strongly disagree."""

    name = "escalate"
    description = "Escalate disagreements for human review"

    def __init__(
        self,
        escalation_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._resolved: dict[str, dict[str, Any]] = {}
        self._callback = escalation_callback

    def execute(self, params: dict[str, Any]) -> ToolResult:
        escalation_id = f"esc_{uuid.uuid4().hex[:12]}"
        escalation: dict[str, Any] = {
            "escalation_id": escalation_id,
            "reason": params.get("reason", ""),
            "expert_opinions": params.get("expert_opinions", {}),
            "urgency": params.get("urgency", "medium"),
            "context": params.get("context", {}),
            "created_at": time.time(),
        }
        self._pending[escalation_id] = escalation

        if self._callback is not None:
            self._callback(escalation)

        return ToolResult(
            success=True,
            data={
                "escalation_id": escalation_id,
                "summary": (
                    f"Escalation created: {params.get('reason', '')} "
                    f"[urgency={params.get('urgency', 'medium')}]"
                ),
            },
        )

    def get_pending_escalations(self) -> list[dict[str, Any]]:
        return list(self._pending.values())

    def resolve_escalation(self, escalation_id: str, resolution: str) -> None:
        if escalation_id not in self._pending:
            raise KeyError(f"Escalation {escalation_id} not found in pending")
        esc = self._pending.pop(escalation_id)
        esc["resolution"] = resolution
        esc["resolved_at"] = time.time()
        self._resolved[escalation_id] = esc


# ---------------------------------------------------------------------------
# MessageBus
# ---------------------------------------------------------------------------


@dataclass
class _Message:
    message_id: str
    from_agent: str
    to_agent: str
    message_type: str
    payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    read: bool = False
    ttl: float = 3600.0

    def is_expired(self) -> bool:
        return time.time() - self.created_at >= self.ttl


class MessageBus:
    """Simple agent-to-agent messaging with TTL-based expiry."""

    def __init__(self, default_ttl: float = 3600.0) -> None:
        self._agents: set[str] = set()
        self._messages: dict[str, _Message] = {}
        self._inboxes: dict[str, list[str]] = {}  # agent -> [message_id, ...]
        self._default_ttl = default_ttl

    def register_agent(self, agent_name: str) -> None:
        self._agents.add(agent_name)
        self._inboxes.setdefault(agent_name, [])

    def get_agents(self) -> list[str]:
        return sorted(self._agents)

    def send(
        self,
        from_agent: str,
        to_agent: str,
        message_type: str,
        payload: dict[str, Any],
    ) -> str:
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        msg = _Message(
            message_id=msg_id,
            from_agent=from_agent,
            to_agent=to_agent,
            message_type=message_type,
            payload=payload,
            ttl=self._default_ttl,
        )
        self._messages[msg_id] = msg
        self._inboxes.setdefault(to_agent, []).append(msg_id)
        return msg_id

    def get_inbox(self, agent_name: str) -> list[dict[str, Any]]:
        self._prune(agent_name)
        result: list[dict[str, Any]] = []
        for msg_id in list(self._inboxes.get(agent_name, [])):
            msg = self._messages.get(msg_id)
            if msg is None or msg.read:
                continue
            result.append(
                {
                    "message_id": msg.message_id,
                    "from": msg.from_agent,
                    "to": msg.to_agent,
                    "type": msg.message_type,
                    "payload": msg.payload,
                    "created_at": msg.created_at,
                }
            )
        return result

    def mark_read(self, message_id: str) -> None:
        if message_id in self._messages:
            self._messages[message_id].read = True

    def broadcast(
        self,
        from_agent: str,
        message_type: str,
        payload: dict[str, Any],
    ) -> list[str]:
        ids: list[str] = []
        for agent in sorted(self._agents):
            if agent != from_agent:
                ids.append(self.send(from_agent, agent, message_type, payload))
        return ids

    # -- internal ------------------------------------------------------------

    def _prune(self, agent_name: str) -> None:
        """Remove expired messages from an agent's inbox."""
        inbox = self._inboxes.get(agent_name, [])
        alive: list[str] = []
        for msg_id in inbox:
            msg = self._messages.get(msg_id)
            if msg is not None and not msg.is_expired():
                alive.append(msg_id)
            else:
                self._messages.pop(msg_id, None)
        self._inboxes[agent_name] = alive
