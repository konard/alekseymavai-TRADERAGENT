"""Base expert agent with argument/vote protocol."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.core.event_bus import EventBus


class Verdict(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    DEFER = "defer"


class ArgumentType(str, Enum):
    RAISED = "raised"
    CHALLENGED = "challenged"
    SUPPORTED = "supported"
    SYNTHESIZED = "synthesized"


@dataclass
class Argument:
    expert_name: str
    argument_type: ArgumentType
    thesis: str  # human-readable reasoning
    confidence: float  # 0.0 - 1.0
    evidence: dict[str, Any] = field(default_factory=dict)
    challenges: list[str] = field(default_factory=list)  # argument_ids this challenges
    argument_id: str = field(default_factory=lambda: f"arg_{uuid.uuid4().hex[:12]}")
    ts: float = field(default_factory=time.time)


@dataclass
class Vote:
    expert_name: str
    verdict: Verdict
    confidence: float
    reasoning: str
    key_factors: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)


@dataclass
class CommitteeDecision:
    verdict: Verdict
    score: float  # weighted confidence 0.0-1.0
    arguments: list[Argument] = field(default_factory=list)
    votes: list[Vote] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    reasoning: str = ""
    session_id: str = field(default_factory=lambda: f"session_{uuid.uuid4().hex[:12]}")
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "score": round(self.score, 4),
            "reasoning": self.reasoning,
            "session_id": self.session_id,
            "votes": [
                {
                    "expert": v.expert_name,
                    "verdict": v.verdict.value,
                    "confidence": round(v.confidence, 4),
                    "reasoning": v.reasoning,
                }
                for v in self.votes
            ],
            "arguments_count": len(self.arguments),
            "contradictions_count": len(self.contradictions),
            "ts": self.ts,
        }


class BaseExpert:
    """Base class for expert agents.

    Each expert:
    - Has a name and role description
    - Analyzes signals from their domain perspective
    - Produces Arguments (thesis + evidence + confidence)
    - Can challenge other experts' arguments
    - Casts a Vote (APPROVE/REJECT/DEFER + confidence)
    """

    name: str = "base_expert"
    role: str = "Base Expert"
    weight: float = 1.0  # vote weight in committee

    def __init__(self, event_bus: EventBus | None = None):
        self._bus = event_bus
        self._last_analysis: dict[str, Any] = {}

    def analyze(self, signal: dict[str, Any], market_data: dict[str, Any]) -> Argument:
        """Analyze a signal and produce an argument.

        Args:
            signal: Trading signal dict with keys: direction, strategy, price, symbol, etc.
            market_data: Market context dict with keys: df_h1, df_m5, regime, adx, atr, etc.

        Returns:
            Argument with thesis, confidence, and evidence.
        """
        raise NotImplementedError

    def challenge(
        self, argument: Argument, signal: dict[str, Any], market_data: dict[str, Any]
    ) -> Argument | None:
        """Optionally challenge another expert's argument.

        Returns None if no challenge (agrees), or a counter-Argument.
        """
        return None

    def vote(
        self, signal: dict[str, Any], market_data: dict[str, Any], arguments: list[Argument]
    ) -> Vote:
        """Cast final vote after seeing all arguments.

        Args:
            signal: Original signal
            market_data: Market context
            arguments: All arguments raised (including challenges)

        Returns:
            Vote with verdict and confidence
        """
        raise NotImplementedError

    async def _publish_argument(self, argument: Argument, session_id: str) -> None:
        """Publish argument as DomainEvent."""
        if self._bus:
            from bot.core.event_bus import DomainEvent

            evt = DomainEvent(
                entity_type="session",
                entity_id=session_id,
                event_type=f"ARGUMENT_{argument.argument_type.value.upper()}",
                data={
                    "expert": argument.expert_name,
                    "thesis": argument.thesis,
                    "confidence": argument.confidence,
                    "evidence": argument.evidence,
                    "argument_id": argument.argument_id,
                    "challenges": argument.challenges,
                },
                bot_name=self.name,
            )
            await self._bus.publish(evt)

    async def _publish_vote(self, vote: Vote, session_id: str) -> None:
        """Publish vote as DomainEvent."""
        if self._bus:
            from bot.core.event_bus import DomainEvent

            evt = DomainEvent(
                entity_type="session",
                entity_id=session_id,
                event_type="VOTE_CAST",
                data={
                    "expert": vote.expert_name,
                    "verdict": vote.verdict.value,
                    "confidence": vote.confidence,
                    "reasoning": vote.reasoning,
                    "key_factors": vote.key_factors,
                },
                bot_name=self.name,
            )
            await self._bus.publish(evt)
