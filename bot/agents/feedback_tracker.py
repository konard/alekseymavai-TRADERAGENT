"""Expert Feedback Tracker — scores expert votes against trade outcomes."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from bot.core.event_bus import EventBus, DomainEvent

from bot.agents.base_expert import Verdict, CommitteeDecision

logger = logging.getLogger(__name__)


@dataclass
class ExpertScorecard:
    expert_name: str
    total_votes: int = 0
    correct_votes: int = 0
    incorrect_votes: int = 0
    deferred_votes: int = 0
    accuracy: float = 0.0
    profit_when_approved: float = 0.0
    loss_when_approved: float = 0.0
    avoided_losses: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "expert_name": self.expert_name,
            "total_votes": self.total_votes,
            "correct_votes": self.correct_votes,
            "incorrect_votes": self.incorrect_votes,
            "deferred_votes": self.deferred_votes,
            "accuracy": round(self.accuracy, 4),
            "profit_when_approved": round(self.profit_when_approved, 4),
            "loss_when_approved": round(self.loss_when_approved, 4),
            "avoided_losses": round(self.avoided_losses, 4),
        }


class ExpertFeedbackTracker:
    """Tracks expert vote accuracy against actual trade outcomes."""

    def __init__(self, event_bus: "EventBus | None" = None) -> None:
        self._bus = event_bus
        self._pending_decisions: dict[str, dict[str, Any]] = {}
        self._scorecards: dict[str, ExpertScorecard] = {}
        self._unsub_decision: Any = None
        self._unsub_position: Any = None

    def record_decision(
        self,
        decision: CommitteeDecision,
        position_id: str,
        regime: str = "",
        market_data: dict | None = None,
    ) -> None:
        """Store a pending decision to be scored when the outcome is known."""
        self._pending_decisions[decision.session_id] = {
            "decision": decision,
            "position_id": position_id,
            "regime": regime,
            "market_data": market_data or {},
        }

    def record_outcome(self, position_id: str, pnl: float) -> list[tuple[str, bool]]:
        """Score each expert's vote against the actual trade outcome.

        Returns list of (expert_name, was_correct) tuples.
        """
        # Find matching session by position_id
        session_id = None
        for sid, pending in self._pending_decisions.items():
            if pending["position_id"] == position_id:
                session_id = sid
                break

        if session_id is None:
            return []

        pending = self._pending_decisions.pop(session_id)
        decision: CommitteeDecision = pending["decision"]
        results: list[tuple[str, bool]] = []

        for vote in decision.votes:
            expert = vote.expert_name
            if expert not in self._scorecards:
                self._scorecards[expert] = ExpertScorecard(expert_name=expert)

            sc = self._scorecards[expert]
            sc.total_votes += 1

            if vote.verdict == Verdict.DEFER:
                sc.deferred_votes += 1
                continue

            if vote.verdict == Verdict.APPROVE:
                if pnl > 0:
                    sc.correct_votes += 1
                    sc.profit_when_approved += pnl
                    results.append((expert, True))
                else:
                    sc.incorrect_votes += 1
                    sc.loss_when_approved += pnl
                    results.append((expert, False))
            elif vote.verdict == Verdict.REJECT:
                if pnl <= 0:
                    sc.correct_votes += 1
                    sc.avoided_losses += abs(pnl)
                    results.append((expert, True))
                else:
                    sc.incorrect_votes += 1
                    results.append((expert, False))

            denominator = sc.correct_votes + sc.incorrect_votes
            sc.accuracy = sc.correct_votes / denominator if denominator > 0 else 0.0

        return results

    def get_scorecard(self, expert_name: str) -> ExpertScorecard:
        if expert_name not in self._scorecards:
            self._scorecards[expert_name] = ExpertScorecard(expert_name=expert_name)
        return self._scorecards[expert_name]

    def get_all_scorecards(self) -> list[ExpertScorecard]:
        return list(self._scorecards.values())

    def get_accuracy(self, expert_name: str) -> float:
        return self.get_scorecard(expert_name).accuracy

    async def _on_decision_made(self, event: "DomainEvent") -> None:
        """Handle DECISION_MADE event from EventBus."""
        data = event.data
        decision = data.get("decision")
        position_id = data.get("position_id", "")
        regime = data.get("regime", "")
        market_data = data.get("market_data")
        if decision is not None:
            self.record_decision(decision, position_id, regime, market_data)

    async def _on_position_closed(self, event: "DomainEvent") -> None:
        """Handle POSITION_CLOSED event from EventBus."""
        data = event.data
        position_id = data.get("position_id", event.entity_id)
        pnl = data.get("pnl", 0.0)
        self.record_outcome(position_id, pnl)

    async def start(self) -> None:
        """Subscribe to DECISION_MADE and POSITION_CLOSED on EventBus."""
        if self._bus is None:
            return
        self._unsub_decision = self._bus.subscribe("DECISION_MADE", self._on_decision_made)
        self._unsub_position = self._bus.subscribe("POSITION_CLOSED", self._on_position_closed)

    async def stop(self) -> None:
        """Unsubscribe from EventBus events."""
        if self._unsub_decision is not None:
            self._unsub_decision()
            self._unsub_decision = None
        if self._unsub_position is not None:
            self._unsub_position()
            self._unsub_position = None

    def get_stats(self) -> dict[str, Any]:
        return {
            "pending_decisions": len(self._pending_decisions),
            "experts_tracked": len(self._scorecards),
            "scorecards": {name: sc.to_dict() for name, sc in self._scorecards.items()},
        }
