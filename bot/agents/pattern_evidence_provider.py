"""Pattern Evidence Provider — bridges PatternLearner and InvestmentCommittee."""

from __future__ import annotations

import copy
from typing import Any, TYPE_CHECKING

from bot.utils.logger import get_logger

if TYPE_CHECKING:
    from bot.core.pattern_learner import PatternLearner

logger = get_logger(__name__)


class PatternEvidenceProvider:
    """Enriches market_data with pattern match evidence from PatternLearner.

    Acts as a bridge: extracts context from signal + market_data,
    queries PatternLearner.match_signal(), and injects results back
    into market_data for downstream expert consumption.
    """

    def __init__(self, pattern_learner: PatternLearner) -> None:
        self._learner = pattern_learner

    def enrich_market_data(
        self, signal: dict[str, Any], market_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Build context from signal + market_data, query patterns, return enriched copy.

        The original market_data dict is never mutated.

        Args:
            signal: Trading signal with direction, strategy, price, etc.
            market_data: Market context with regime, adx, volatility, etc.

        Returns:
            A shallow copy of market_data with pattern_matches,
            pattern_recommendation, pattern_confidence, and pattern_win_rate added.
        """
        # Build context for pattern matching
        context: dict[str, Any] = {
            "regime": market_data.get("regime", "unknown"),
            "strategy": signal.get("strategy", "unknown"),
            "direction": signal.get("direction", "unknown"),
            "adx": market_data.get("adx"),
            "volatility": market_data.get("volatility"),
            "drawdown": market_data.get("drawdown"),
            "hour": market_data.get("hour"),
        }

        # Add optional SMC fields if present
        if market_data.get("near_demand_zone"):
            context["near_demand_zone"] = True
        if market_data.get("near_supply_zone"):
            context["near_supply_zone"] = True
        if market_data.get("bos_detected"):
            context["bos_detected"] = True
        if market_data.get("choch_detected"):
            context["choch_detected"] = True
        if market_data.get("volume_trend"):
            context["volume_trend"] = market_data["volume_trend"]

        # Query pattern learner
        matches = self._learner.match_signal(context)

        # Build enrichment
        pattern_matches = [
            {
                "pattern_name": m.pattern.name,
                "match_score": round(m.match_score, 4),
                "win_rate": round(m.pattern.win_rate, 4),
                "recommendation": m.recommendation,
                "occurrences": m.pattern.occurrences,
                "confidence": round(m.pattern.confidence, 4),
            }
            for m in matches
        ]

        # Best match = first (already sorted by match_score * confidence)
        if pattern_matches:
            best = pattern_matches[0]
            recommendation = best["recommendation"]
            confidence = best["confidence"]
            win_rate = best["win_rate"]
        else:
            recommendation = "no_data"
            confidence = 0.0
            win_rate = None

        # Return enriched copy (don't mutate original)
        enriched = copy.copy(market_data)
        enriched["pattern_matches"] = pattern_matches
        enriched["pattern_recommendation"] = recommendation
        enriched["pattern_confidence"] = confidence
        enriched["pattern_win_rate"] = win_rate

        logger.debug(
            "pattern_enrichment",
            matches=len(pattern_matches),
            recommendation=recommendation,
            confidence=confidence,
        )

        return enriched
