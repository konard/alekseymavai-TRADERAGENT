"""Adaptive Weight Calibrator — adjusts expert weights based on rolling accuracy."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.agents.committee import InvestmentCommittee
    from bot.agents.feedback_tracker import ExpertFeedbackTracker
    from bot.agents.regime_profile import RegimeProfileStore

DEFAULT_BASE_WEIGHTS: dict[str, float] = {
    "smc_structuralist": 1.2,
    "trend_analyst": 1.0,
    "risk_manager": 1.5,
    "contrarian": 0.8,
}


class WeightCalibrator:
    """Adjusts expert weights based on rolling accuracy from FeedbackTracker
    and optionally regime-specific accuracy from RegimeProfileStore.

    Weight formula:
        new_weight = base_weight * (0.5 + accuracy)
        Clamped to [min_weight, max_weight]

    If an expert has fewer than ``min_samples`` scored votes the base weight
    is preserved unchanged.
    """

    def __init__(
        self,
        base_weights: dict[str, float] | None = None,
        min_weight: float = 0.3,
        max_weight: float = 2.5,
        min_samples: int = 10,
    ) -> None:
        self._base_weights: dict[str, float] = dict(
            base_weights if base_weights is not None else DEFAULT_BASE_WEIGHTS
        )
        self._min_weight = min_weight
        self._max_weight = max_weight
        self._min_samples = min_samples
        self._weight_history: dict[str, list[tuple[float, float]]] = {}
        self._calibration_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calibrate(
        self,
        committee: InvestmentCommittee,
        tracker: ExpertFeedbackTracker,
        regime_store: RegimeProfileStore | None = None,
        current_regime: str | None = None,
    ) -> dict[str, float]:
        """Adjust expert weights based on accuracy.

        For each expert registered in *committee*:
        * If *regime_store* and *current_regime* are provided, use
          regime-specific accuracy; otherwise use overall accuracy from
          *tracker*.
        * ``new_weight = base_weight * (0.5 + accuracy)`` clamped to
          ``[min_weight, max_weight]``.
        * If the expert has fewer than *min_samples* scored votes the base
          weight is kept unchanged.

        Returns:
            Mapping of ``expert_name -> new_weight``.
        """
        result: dict[str, float] = {}
        now = time.time()

        for expert in committee.experts:
            name = expert.name
            base = self._base_weights.get(name, 1.0)

            scorecard = tracker.get_scorecard(name)
            scored = scorecard.correct_votes + scorecard.incorrect_votes

            if scored < self._min_samples:
                # Not enough data — keep base weight
                new_weight = base
            else:
                # Determine accuracy source
                if regime_store is not None and current_regime is not None:
                    accuracy = regime_store.get_regime_accuracy(name, current_regime)
                else:
                    accuracy = tracker.get_accuracy(name)

                raw = base * (0.5 + accuracy)
                new_weight = max(self._min_weight, min(self._max_weight, raw))

            # Apply to expert
            expert.weight = new_weight

            # Record history
            if name not in self._weight_history:
                self._weight_history[name] = []
            self._weight_history[name].append((now, new_weight))

            result[name] = new_weight

        self._calibration_count += 1
        return result

    def get_weight_history(self) -> dict[str, list[tuple[float, float]]]:
        """Return full weight history: expert_name -> [(timestamp, weight), ...]."""
        return dict(self._weight_history)

    def get_current_weights(self, committee: InvestmentCommittee) -> dict[str, float]:
        """Snapshot of current expert weights from the committee."""
        return {e.name: e.weight for e in committee.experts}

    def get_stats(self) -> dict:
        """Return calibrator statistics."""
        current: dict[str, float] = {}
        for name, entries in self._weight_history.items():
            if entries:
                current[name] = entries[-1][1]
        return {
            "calibration_count": self._calibration_count,
            "current_weights": current,
            "base_weights": dict(self._base_weights),
        }

    def reset(self) -> None:
        """Reset calibrator state back to base weights (history cleared)."""
        self._weight_history.clear()
        self._calibration_count = 0
