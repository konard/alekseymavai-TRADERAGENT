"""Investment Committee — orchestrates expert debate and produces collective decisions."""

import uuid
from typing import Any

from bot.agents.base_expert import (
    BaseExpert,
    Argument,
    ArgumentType,
    Vote,
    Verdict,
    CommitteeDecision,
)
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class InvestmentCommittee:
    """Orchestrates expert agents in a debate protocol.

    Flow:
    1. SESSION_STARTED
    2. Each expert analyzes -> ARGUMENT_RAISED
    3. Cross-challenge round -> ARGUMENT_CHALLENGED / ARGUMENT_SUPPORTED
    4. Contradiction detection -> CONTRADICTION_FOUND
    5. Voting round -> VOTE_CAST per expert
    6. Decision aggregation -> DECISION_MADE

    The committee is synchronous (no actual LLM calls) — each expert
    uses rule-based analysis. This keeps latency under 1ms per signal.
    """

    def __init__(
        self,
        experts: list[BaseExpert] | None = None,
        event_bus: Any = None,
    ) -> None:
        self._experts: list[BaseExpert] = experts or []
        self._bus = event_bus
        self._history: list[CommitteeDecision] = []
        self._max_history = 500

    def add_expert(self, expert: BaseExpert) -> None:
        """Register an expert in the committee."""
        self._experts.append(expert)
        if self._bus:
            expert._bus = self._bus

    @property
    def experts(self) -> list[BaseExpert]:
        return list(self._experts)

    @property
    def history(self) -> list[CommitteeDecision]:
        return list(self._history)

    async def evaluate(self, signal: dict, market_data: dict) -> CommitteeDecision:
        """Run full committee evaluation for a trading signal.

        Args:
            signal: dict with keys: direction, strategy, price, symbol,
                    risk_pct, stop_loss, take_profit
            market_data: dict with keys: smc_context, adx, regime, trend_strength,
                         ema_spread, balance, drawdown, daily_loss, open_positions, etc.

        Returns:
            CommitteeDecision with verdict, score, arguments, votes, contradictions
        """
        session_id = f"session_{uuid.uuid4().hex[:12]}"
        all_arguments: list[Argument] = []
        all_votes: list[Vote] = []
        contradictions: list[dict] = []

        # Publish session start
        await self._publish_session_event(
            session_id,
            "SESSION_STARTED",
            {
                "signal": {k: str(v) for k, v in signal.items()},
                "experts": [e.name for e in self._experts],
            },
        )

        # Phase 1: Each expert analyzes independently
        for expert in self._experts:
            try:
                argument = expert.analyze(signal, market_data)
                all_arguments.append(argument)
                await expert._publish_argument(argument, session_id)
            except Exception as e:
                logger.warning(
                    "expert_analyze_failed", expert=expert.name, error=str(e)
                )

        # Phase 2: Cross-challenge round
        for expert in self._experts:
            for arg in list(all_arguments):
                if arg.expert_name == expert.name:
                    continue
                try:
                    challenge = expert.challenge(arg, signal, market_data)
                    if challenge:
                        all_arguments.append(challenge)
                        await expert._publish_argument(challenge, session_id)
                except Exception as e:
                    logger.warning(
                        "expert_challenge_failed", expert=expert.name, error=str(e)
                    )

        # Phase 3: Detect contradictions
        contradictions = self._detect_contradictions(all_arguments)
        for contradiction in contradictions:
            await self._publish_session_event(
                session_id, "CONTRADICTION_FOUND", contradiction
            )

        # Phase 4: Voting round
        for expert in self._experts:
            try:
                vote = expert.vote(signal, market_data, all_arguments)
                all_votes.append(vote)
                await expert._publish_vote(vote, session_id)
            except Exception as e:
                logger.warning(
                    "expert_vote_failed", expert=expert.name, error=str(e)
                )

        # Phase 5: Aggregate decision
        decision = self._aggregate_decision(
            votes=all_votes,
            arguments=all_arguments,
            contradictions=contradictions,
            session_id=session_id,
        )

        # Publish decision
        await self._publish_session_event(
            session_id, "DECISION_MADE", decision.to_dict()
        )

        # Store in history
        self._history.insert(0, decision)
        if len(self._history) > self._max_history:
            self._history = self._history[: self._max_history]

        logger.info(
            "committee_decision",
            verdict=decision.verdict.value,
            score=f"{decision.score:.3f}",
            votes=f"{len(all_votes)} experts",
            arguments=f"{len(all_arguments)} args",
            contradictions=f"{len(contradictions)} conflicts",
            session_id=session_id,
        )

        return decision

    def _detect_contradictions(self, arguments: list[Argument]) -> list[dict]:
        """Detect logical contradictions between arguments."""
        contradictions: list[dict] = []
        raised = [a for a in arguments if a.argument_type == ArgumentType.RAISED]

        # Find pairs where one is very bullish and another is very bearish
        for i, a in enumerate(raised):
            for b in raised[i + 1 :]:
                # Contradiction: one high confidence, other low, on same signal
                if abs(a.confidence - b.confidence) > 0.4:
                    contradictions.append(
                        {
                            "expert_a": a.expert_name,
                            "expert_b": b.expert_name,
                            "thesis_a": a.thesis,
                            "thesis_b": b.thesis,
                            "confidence_delta": round(
                                abs(a.confidence - b.confidence), 3
                            ),
                            "argument_ids": [a.argument_id, b.argument_id],
                        }
                    )

        # Also count challenges as contradictions
        challenges = [
            a for a in arguments if a.argument_type == ArgumentType.CHALLENGED
        ]
        for c in challenges:
            contradictions.append(
                {
                    "challenger": c.expert_name,
                    "thesis": c.thesis,
                    "challenges": c.challenges,
                    "type": "challenge",
                }
            )

        return contradictions

    def _aggregate_decision(
        self,
        votes: list[Vote],
        arguments: list[Argument],
        contradictions: list[dict],
        session_id: str,
    ) -> CommitteeDecision:
        """Aggregate votes into final decision using weighted voting."""
        if not votes:
            return CommitteeDecision(
                verdict=Verdict.DEFER,
                score=0.0,
                arguments=arguments,
                votes=votes,
                contradictions=contradictions,
                reasoning="Нет голосов",
                session_id=session_id,
            )

        # Build weight map
        expert_weights = {e.name: e.weight for e in self._experts}

        # Calculate weighted scores per verdict
        verdict_scores: dict[Verdict, float] = {v: 0.0 for v in Verdict}
        total_weight = 0.0

        for vote in votes:
            w = expert_weights.get(vote.expert_name, 1.0)
            weighted_conf = vote.confidence * w
            verdict_scores[vote.verdict] += weighted_conf
            total_weight += w

        # Normalize
        if total_weight > 0:
            for v in verdict_scores:
                verdict_scores[v] /= total_weight

        # Winner is the verdict with highest weighted score
        winner = max(verdict_scores, key=lambda v: verdict_scores[v])
        score = verdict_scores[winner]

        # Contradiction penalty: reduce score if many contradictions
        contradiction_penalty = min(0.2, len(contradictions) * 0.05)
        score = max(0.0, score - contradiction_penalty)

        # Build reasoning
        approve_count = sum(1 for v in votes if v.verdict == Verdict.APPROVE)
        reject_count = sum(1 for v in votes if v.verdict == Verdict.REJECT)
        defer_count = sum(1 for v in votes if v.verdict == Verdict.DEFER)

        reasoning = (
            f"Голосование: {approve_count} за, {reject_count} против, "
            f"{defer_count} воздержались. "
            f"Противоречий: {len(contradictions)}. "
            f"Итоговый score: {score:.3f}"
        )

        return CommitteeDecision(
            verdict=winner,
            score=score,
            arguments=arguments,
            votes=votes,
            contradictions=contradictions,
            reasoning=reasoning,
            session_id=session_id,
        )

    async def _publish_session_event(
        self, session_id: str, event_type: str, data: dict
    ) -> None:
        """Publish session event to EventBus."""
        if self._bus:
            from bot.core.event_bus import DomainEvent

            evt = DomainEvent(
                entity_type="session",
                entity_id=session_id,
                event_type=event_type,
                data=data,
                bot_name="committee",
            )
            await self._bus.publish(evt)

    def get_status(self) -> dict:
        """Get committee status for monitoring."""
        last = self._history[0] if self._history else None
        return {
            "experts": [
                {"name": e.name, "role": e.role, "weight": e.weight}
                for e in self._experts
            ],
            "total_sessions": len(self._history),
            "last_decision": last.to_dict() if last else None,
            "verdict_distribution": self._get_verdict_distribution(),
        }

    def _get_verdict_distribution(self) -> dict[str, int]:
        """Count verdicts across history."""
        dist: dict[str, int] = {"approve": 0, "reject": 0, "defer": 0}
        for d in self._history:
            dist[d.verdict.value] = dist.get(d.verdict.value, 0) + 1
        return dist
