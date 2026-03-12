"""Meta-Strategy Capital Allocator — dynamically allocates capital between strategies based on predictions and patterns."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from bot.core.predictive_model import MarkovTransitionModel
from bot.core.pattern_learner import PatternLearner
from bot.agents.regime_profile import RegimeProfileStore
from bot.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_STRATEGIES = ["grid", "dca", "trend_follower", "smc"]


@dataclass
class StrategyScore:
    strategy: str  # e.g., "grid", "dca", "trend_follower", "smc"
    regime: str
    pattern_win_rate: float  # from PatternLearner
    pattern_confidence: float  # Wilson score lower bound
    regime_probability: float  # from Markov model (how likely current regime persists)
    expert_accuracy: float  # from RegimeProfileStore
    composite_score: float  # weighted combination
    recommended_allocation: float  # 0.0-1.0 fraction of capital
    reasoning: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "regime": self.regime,
            "pattern_win_rate": round(self.pattern_win_rate, 4),
            "pattern_confidence": round(self.pattern_confidence, 4),
            "regime_probability": round(self.regime_probability, 4),
            "expert_accuracy": round(self.expert_accuracy, 4),
            "composite_score": round(self.composite_score, 4),
            "recommended_allocation": round(self.recommended_allocation, 4),
            "reasoning": self.reasoning,
        }


@dataclass
class AllocationPlan:
    allocations: dict[str, float]  # strategy -> fraction (sum = 1.0)
    scores: list[StrategyScore]
    current_regime: str
    predicted_next_regime: str
    regime_persistence_prob: float  # probability of staying in current regime
    total_confidence: float  # overall confidence in the plan
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocations": {k: round(v, 4) for k, v in self.allocations.items()},
            "scores": [s.to_dict() for s in self.scores],
            "current_regime": self.current_regime,
            "predicted_next_regime": self.predicted_next_regime,
            "regime_persistence_prob": round(self.regime_persistence_prob, 4),
            "total_confidence": round(self.total_confidence, 4),
            "ts": self.ts,
        }


class MetaStrategyAllocator:
    """Allocates capital between strategies based on regime predictions + pattern learning.

    Combines:
    1. MarkovTransitionModel: probability of regime persistence/transition
    2. PatternLearner: historical win rates per strategy/regime combo
    3. RegimeProfileStore: expert accuracy per regime (optional)

    Allocation formula:
    - composite_score = w1 * pattern_win_rate * pattern_confidence
                      + w2 * regime_persistence_prob
                      + w3 * expert_accuracy
    - Normalize scores to sum to 1.0 for allocation
    - Apply min_allocation floor (every active strategy gets at least this much)
    - Apply max_allocation ceiling (no strategy gets more than this)
    """

    def __init__(
        self,
        markov_model: MarkovTransitionModel,
        pattern_learner: PatternLearner,
        regime_store: RegimeProfileStore | None = None,
        strategies: list[str] | None = None,
        min_allocation: float = 0.05,
        max_allocation: float = 0.60,
        pattern_weight: float = 0.5,
        regime_weight: float = 0.3,
        expert_weight: float = 0.2,
    ) -> None:
        self._markov = markov_model
        self._learner = pattern_learner
        self._regime_store = regime_store
        self._strategies = strategies or list(DEFAULT_STRATEGIES)
        self._min_allocation = min_allocation
        self._max_allocation = max_allocation
        self._w_pattern = pattern_weight
        self._w_regime = regime_weight
        self._w_expert = expert_weight

        self._history: list[AllocationPlan] = []
        self._max_history: int = 200

    def allocate(self, current_regime: str, direction: str = "LONG") -> AllocationPlan:
        """Compute optimal capital allocation for current market conditions.

        Steps:
        1. Get regime persistence probability from Markov: P(stay in current_regime)
        2. Get predicted next regime
        3. For each strategy:
           a. Query PatternLearner for win_rate and confidence in current regime
           b. Query RegimeProfileStore for expert accuracy (if available)
           c. Calculate composite_score
        4. Normalize allocations (with min/max bounds)
        5. Return AllocationPlan
        """
        # Step 1-2: Markov prediction
        pred = self._markov.predict_next(current_regime)
        if pred.predicted_state == current_regime:
            regime_persistence_prob = pred.probability
        else:
            regime_persistence_prob = 1.0 - pred.probability

        predicted_next_regime = pred.predicted_state

        # Step 3: Score each strategy
        scores: list[StrategyScore] = []
        for strategy in self._strategies:
            score = self.get_strategy_score(
                strategy, current_regime, direction, regime_persistence_prob
            )
            scores.append(score)

        # Step 4: Normalize into allocations with min/max bounds
        allocations = self._normalize_allocations(scores)

        # Update scores with final allocation values
        for score in scores:
            score.recommended_allocation = allocations[score.strategy]

        # Total confidence: weighted average of pattern confidences
        confidences = [s.pattern_confidence for s in scores]
        total_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        plan = AllocationPlan(
            allocations=allocations,
            scores=scores,
            current_regime=current_regime,
            predicted_next_regime=predicted_next_regime,
            regime_persistence_prob=regime_persistence_prob,
            total_confidence=total_confidence,
            ts=time.time(),
        )

        self._history.insert(0, plan)
        if len(self._history) > self._max_history:
            self._history = self._history[: self._max_history]

        logger.info(
            "capital_allocated",
            regime=current_regime,
            predicted_next=predicted_next_regime,
            persistence_prob=round(regime_persistence_prob, 3),
            allocations={k: round(v, 3) for k, v in allocations.items()},
        )

        return plan

    def get_strategy_score(
        self,
        strategy: str,
        regime: str,
        direction: str = "LONG",
        regime_persistence_prob: float | None = None,
    ) -> StrategyScore:
        """Score a single strategy for a regime."""
        # Regime persistence from Markov (if not provided)
        if regime_persistence_prob is None:
            pred = self._markov.predict_next(regime)
            if pred.predicted_state == regime:
                regime_persistence_prob = pred.probability
            else:
                regime_persistence_prob = 1.0 - pred.probability

        # Pattern data
        context = {
            "regime": regime,
            "strategy": strategy,
            "direction": direction,
            "adx": 0,
        }
        matches = self._learner.match_signal(context)
        if matches:
            best = matches[0]
            pattern_win_rate = best.expected_win_rate
            pattern_confidence = best.pattern.confidence
        else:
            pattern_win_rate = 0.5  # neutral
            pattern_confidence = 0.0

        # Expert accuracy
        if self._regime_store is not None:
            expert_accuracy = self._regime_store.get_regime_accuracy(strategy, regime)
        else:
            expert_accuracy = 0.5  # neutral default

        # Composite score
        composite_score = (
            self._w_pattern * pattern_win_rate * pattern_confidence
            + self._w_regime * regime_persistence_prob
            + self._w_expert * expert_accuracy
        )

        # Build reasoning
        parts = []
        if pattern_confidence > 0:
            parts.append(
                f"pattern WR={pattern_win_rate:.0%} conf={pattern_confidence:.2f}"
            )
        else:
            parts.append("no pattern data")
        parts.append(f"regime_persist={regime_persistence_prob:.2f}")
        parts.append(f"expert_acc={expert_accuracy:.2f}")
        reasoning = f"{strategy} in {regime}: " + ", ".join(parts)

        return StrategyScore(
            strategy=strategy,
            regime=regime,
            pattern_win_rate=pattern_win_rate,
            pattern_confidence=pattern_confidence,
            regime_probability=regime_persistence_prob,
            expert_accuracy=expert_accuracy,
            composite_score=composite_score,
            recommended_allocation=0.0,  # will be filled after normalization
            reasoning=reasoning,
        )

    def _normalize_allocations(
        self, scores: list[StrategyScore]
    ) -> dict[str, float]:
        """Normalize composite scores into allocations respecting min/max bounds."""
        n = len(scores)
        if n == 0:
            return {}

        total_score = sum(s.composite_score for s in scores)

        # Raw proportional allocation
        if total_score > 0:
            raw = {s.strategy: s.composite_score / total_score for s in scores}
        else:
            # Equal allocation when all scores are zero
            raw = {s.strategy: 1.0 / n for s in scores}

        # Apply min floor
        allocations = {k: max(v, self._min_allocation) for k, v in raw.items()}

        # Apply max ceiling
        allocations = {k: min(v, self._max_allocation) for k, v in allocations.items()}

        # Re-normalize to sum to 1.0
        total = sum(allocations.values())
        if total > 0:
            allocations = {k: v / total for k, v in allocations.items()}

        return allocations

    def get_current_plan(self) -> AllocationPlan | None:
        """Get most recent allocation plan."""
        return self._history[0] if self._history else None

    def get_history(self, limit: int = 20) -> list[dict]:
        """Get allocation history."""
        return [p.to_dict() for p in self._history[:limit]]

    def get_stats(self) -> dict[str, Any]:
        """Summary stats."""
        current = self.get_current_plan()
        return {
            "strategies": self._strategies,
            "total_allocations": len(self._history),
            "weights": {
                "pattern": self._w_pattern,
                "regime": self._w_regime,
                "expert": self._w_expert,
            },
            "bounds": {
                "min_allocation": self._min_allocation,
                "max_allocation": self._max_allocation,
            },
            "current_plan": current.to_dict() if current else None,
        }
