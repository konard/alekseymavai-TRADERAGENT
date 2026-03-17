"""Pattern-Aware Investment Committee — auto-enriches signals with pattern evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bot.agents.base_expert import BaseExpert, CommitteeDecision
from bot.agents.committee import InvestmentCommittee
from bot.agents.pattern_evidence_provider import PatternEvidenceProvider
from bot.utils.logger import get_logger

if TYPE_CHECKING:
    from bot.core.event_bus import EventBus
    from bot.core.pattern_learner import PatternLearner

logger = get_logger(__name__)


class PatternAwareCommittee(InvestmentCommittee):
    """Investment Committee that automatically enriches market_data with pattern evidence.

    Before each evaluation, the committee queries PatternLearner for matching
    patterns and injects them into market_data so all experts can see
    historical pattern context alongside their usual inputs.
    """

    def __init__(
        self,
        experts: list[BaseExpert] | None = None,
        event_bus: EventBus | None = None,
        pattern_learner: PatternLearner | None = None,
    ) -> None:
        super().__init__(experts=experts, event_bus=event_bus)
        self._pattern_learner = pattern_learner
        self._provider = PatternEvidenceProvider(pattern_learner) if pattern_learner else None

    async def evaluate(
        self, signal: dict[str, Any], market_data: dict[str, Any]
    ) -> CommitteeDecision:
        """Enrich market_data with pattern evidence, then run standard committee evaluation."""
        if self._provider:
            enriched = self._provider.enrich_market_data(signal, market_data)
        else:
            enriched = market_data

        return await super().evaluate(signal, enriched)

    def get_status(self) -> dict[str, Any]:
        """Get committee status including pattern learner stats."""
        base = super().get_status()
        if self._pattern_learner:
            base["pattern_learner"] = self._pattern_learner.get_stats()
        return base
