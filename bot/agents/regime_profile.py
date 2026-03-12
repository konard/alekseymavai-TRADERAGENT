"""Regime-Aware Expert Profiles — accuracy breakdown by market regime."""

from __future__ import annotations

from dataclasses import dataclass, field

from bot.agents.base_expert import Verdict

# All regime types from MarketRegime enum
REGIME_TYPES = (
    "tight_range",
    "wide_range",
    "quiet_transition",
    "volatile_transition",
    "bull_trend",
    "bear_trend",
    "accumulation",
    "distribution",
)


@dataclass
class RegimeAccuracy:
    """Accuracy statistics for one expert in one market regime."""

    regime: str
    total: int = 0
    correct: int = 0
    incorrect: int = 0
    deferred: int = 0
    accuracy: float = 0.0
    avg_pnl_when_approved: float = 0.0
    _pnl_sum: float = 0.0
    _pnl_count: int = 0

    def to_dict(self) -> dict:
        return {
            "regime": self.regime,
            "total": self.total,
            "correct": self.correct,
            "incorrect": self.incorrect,
            "deferred": self.deferred,
            "accuracy": round(self.accuracy, 4),
            "avg_pnl_when_approved": round(self.avg_pnl_when_approved, 4),
        }


class ExpertRegimeProfile:
    """Per-expert accuracy breakdown across market regimes."""

    def __init__(self, expert_name: str) -> None:
        self.expert_name = expert_name
        self._regime_stats: dict[str, RegimeAccuracy] = {}

    def _ensure_regime(self, regime: str) -> RegimeAccuracy:
        if regime not in self._regime_stats:
            self._regime_stats[regime] = RegimeAccuracy(regime=regime)
        return self._regime_stats[regime]

    def record(
        self,
        regime: str,
        verdict: Verdict,
        was_correct: bool,
        pnl: float = 0.0,
    ) -> None:
        """Record an outcome for this expert in the given regime."""
        ra = self._ensure_regime(regime)
        ra.total += 1

        if verdict == Verdict.DEFER:
            ra.deferred += 1
            return

        if was_correct:
            ra.correct += 1
        else:
            ra.incorrect += 1

        # Recalculate accuracy
        denom = ra.correct + ra.incorrect
        ra.accuracy = ra.correct / denom if denom > 0 else 0.0

        # Track PnL for APPROVE verdicts
        if verdict == Verdict.APPROVE:
            ra._pnl_sum += pnl
            ra._pnl_count += 1
            ra.avg_pnl_when_approved = (
                ra._pnl_sum / ra._pnl_count if ra._pnl_count > 0 else 0.0
            )

    def get_accuracy(self, regime: str) -> float:
        """Return accuracy for a regime, or 0.5 if no data."""
        ra = self._regime_stats.get(regime)
        if ra is None:
            return 0.5
        denom = ra.correct + ra.incorrect
        if denom == 0:
            return 0.5
        return ra.accuracy

    def get_best_regime(self) -> str:
        """Return the regime with highest accuracy (min 5 non-deferred samples)."""
        best_regime = ""
        best_acc = -1.0
        for name, ra in self._regime_stats.items():
            samples = ra.correct + ra.incorrect
            if samples >= 5 and ra.accuracy > best_acc:
                best_acc = ra.accuracy
                best_regime = name
        return best_regime

    def get_worst_regime(self) -> str:
        """Return the regime with lowest accuracy (min 5 non-deferred samples)."""
        worst_regime = ""
        worst_acc = 2.0
        for name, ra in self._regime_stats.items():
            samples = ra.correct + ra.incorrect
            if samples >= 5 and ra.accuracy < worst_acc:
                worst_acc = ra.accuracy
                worst_regime = name
        return worst_regime

    def get_all_regimes(self) -> dict[str, RegimeAccuracy]:
        return dict(self._regime_stats)

    def to_dict(self) -> dict:
        return {
            "expert_name": self.expert_name,
            "regimes": {k: v.to_dict() for k, v in self._regime_stats.items()},
        }


class RegimeProfileStore:
    """Collection of ExpertRegimeProfiles across all experts."""

    def __init__(self) -> None:
        self._profiles: dict[str, ExpertRegimeProfile] = {}

    def _ensure_profile(self, expert_name: str) -> ExpertRegimeProfile:
        if expert_name not in self._profiles:
            self._profiles[expert_name] = ExpertRegimeProfile(expert_name)
        return self._profiles[expert_name]

    def record_outcome(
        self,
        expert_name: str,
        regime: str,
        verdict: Verdict,
        was_correct: bool,
        pnl: float = 0.0,
    ) -> None:
        """Record an outcome, auto-creating the profile if needed."""
        profile = self._ensure_profile(expert_name)
        profile.record(regime, verdict, was_correct, pnl)

    def get_profile(self, expert_name: str) -> ExpertRegimeProfile:
        return self._ensure_profile(expert_name)

    def get_regime_accuracy(self, expert_name: str, regime: str) -> float:
        return self._ensure_profile(expert_name).get_accuracy(regime)

    def get_all_profiles(self) -> dict[str, ExpertRegimeProfile]:
        return dict(self._profiles)

    def get_stats(self) -> dict:
        """Summary stats across all experts and regimes."""
        total_records = 0
        total_correct = 0
        total_incorrect = 0
        total_deferred = 0
        experts_count = len(self._profiles)
        regimes_seen: set[str] = set()

        for profile in self._profiles.values():
            for regime_name, ra in profile.get_all_regimes().items():
                regimes_seen.add(regime_name)
                total_records += ra.total
                total_correct += ra.correct
                total_incorrect += ra.incorrect
                total_deferred += ra.deferred

        denom = total_correct + total_incorrect
        overall_accuracy = total_correct / denom if denom > 0 else 0.0

        return {
            "experts_count": experts_count,
            "regimes_count": len(regimes_seen),
            "total_records": total_records,
            "total_correct": total_correct,
            "total_incorrect": total_incorrect,
            "total_deferred": total_deferred,
            "overall_accuracy": round(overall_accuracy, 4),
        }

    def to_dict(self) -> dict:
        return {
            "profiles": {k: v.to_dict() for k, v in self._profiles.items()},
            "stats": self.get_stats(),
        }
