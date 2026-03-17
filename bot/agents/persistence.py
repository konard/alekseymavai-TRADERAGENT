"""Agent State Persistence — saves/restores learned agent state to disk."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.agents.committee import InvestmentCommittee
    from bot.agents.feedback_tracker import ExpertFeedbackTracker
    from bot.agents.regime_profile import RegimeProfileStore
    from bot.agents.weight_calibrator import WeightCalibrator
    from bot.core.pattern_learner import PatternLearner

logger = logging.getLogger(__name__)

_VERSION = "1.0"


class AgentStatePersistence:
    """Persists agent learning state (weights, patterns, profiles, feedback) to disk.

    State is saved as JSON files in a directory:
    - weights.json — current expert weights + base weights + weight history
    - patterns.json — PatternLearner pattern library
    - regime_profiles.json — RegimeProfileStore data
    - feedback.json — ExpertFeedbackTracker scorecards
    - metadata.json — last save time, version, stats

    Usage:
        persistence = AgentStatePersistence("/path/to/state")
        persistence.save(committee, tracker, regime_store, calibrator, pattern_learner)
        persistence.restore(committee, tracker, regime_store, calibrator, pattern_learner)
    """

    _FILES = {
        "weights": "weights.json",
        "feedback": "feedback.json",
        "regime_profiles": "regime_profiles.json",
        "patterns": "patterns.json",
        "metadata": "metadata.json",
    }

    def __init__(
        self,
        state_dir: str = "data/agent_state",
        auto_save_interval: int = 0,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._auto_save_interval = auto_save_interval
        self._save_count = 0
        self._total_decisions = 0
        self._last_auto_save_at = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(
        self,
        committee: InvestmentCommittee | None = None,
        tracker: ExpertFeedbackTracker | None = None,
        regime_store: RegimeProfileStore | None = None,
        calibrator: WeightCalibrator | None = None,
        pattern_learner: PatternLearner | None = None,
    ) -> str:
        """Save all components to disk. Returns state_dir path.

        Each component is optional — only saves what's provided.
        Uses atomic write (write to .tmp then rename) to prevent corruption.
        """
        components_saved: list[str] = []

        if committee is not None or calibrator is not None:
            data = self._build_weights_data(committee, calibrator)
            self._atomic_write("weights", data)
            components_saved.append("weights")

        if tracker is not None:
            data = self._build_feedback_data(tracker)
            self._atomic_write("feedback", data)
            components_saved.append("feedback")

        if regime_store is not None:
            data = self._build_regime_data(regime_store)
            self._atomic_write("regime_profiles", data)
            components_saved.append("regime_profiles")

        if pattern_learner is not None:
            data = self._build_patterns_data(pattern_learner)
            self._atomic_write("patterns", data)
            components_saved.append("patterns")

        self._save_count += 1
        metadata = {
            "last_save_ts": time.time(),
            "version": _VERSION,
            "components": components_saved,
            "total_decisions": self._total_decisions,
            "save_count": self._save_count,
        }
        self._atomic_write("metadata", metadata)

        logger.info(
            "agent_state_saved",
            extra={"components": components_saved, "state_dir": str(self._state_dir)},
        )
        return str(self._state_dir)

    def restore(
        self,
        committee: InvestmentCommittee | None = None,
        tracker: ExpertFeedbackTracker | None = None,
        regime_store: RegimeProfileStore | None = None,
        calibrator: WeightCalibrator | None = None,
        pattern_learner: PatternLearner | None = None,
    ) -> dict[str, bool]:
        """Restore state from disk. Returns {component: was_restored} dict.

        For committee: restores expert weights
        For tracker: restores scorecards
        For regime_store: restores RegimeAccuracy per expert per regime
        For calibrator: restores base_weights, weight_history
        For pattern_learner: restores Pattern objects
        """
        results: dict[str, bool] = {}

        if committee is not None:
            results["committee"] = self._restore_weights_to_committee(committee)

        if calibrator is not None:
            results["calibrator"] = self._restore_weights_to_calibrator(calibrator)

        if tracker is not None:
            results["tracker"] = self._restore_feedback(tracker)

        if regime_store is not None:
            results["regime_store"] = self._restore_regime_profiles(regime_store)

        if pattern_learner is not None:
            results["pattern_learner"] = self._restore_patterns(pattern_learner)

        logger.info(
            "agent_state_restored",
            extra={"results": results, "state_dir": str(self._state_dir)},
        )
        return results

    def save_if_needed(self, decision_count: int, **kwargs: Any) -> bool:
        """Save if auto_save_interval reached. Returns True if saved."""
        self._total_decisions = decision_count
        if self._auto_save_interval <= 0:
            return False
        if decision_count <= 0:
            return False
        if decision_count % self._auto_save_interval != 0:
            return False
        if decision_count == self._last_auto_save_at:
            return False
        self._last_auto_save_at = decision_count
        self.save(**kwargs)
        return True

    def get_metadata(self) -> dict:
        """Read metadata.json — last_save_ts, version, component list."""
        return self._read_file("metadata") or {}

    def exists(self) -> bool:
        """Check if state directory has saved state."""
        meta_path = self._state_dir / self._FILES["metadata"]
        return meta_path.exists()

    def clear(self) -> None:
        """Remove all saved state files."""
        for filename in self._FILES.values():
            path = self._state_dir / filename
            if path.exists():
                path.unlink()
        logger.info("agent_state_cleared", extra={"state_dir": str(self._state_dir)})

    # ------------------------------------------------------------------
    # Build serializable data
    # ------------------------------------------------------------------

    @staticmethod
    def _build_weights_data(
        committee: InvestmentCommittee | None,
        calibrator: WeightCalibrator | None,
    ) -> dict:
        data: dict[str, Any] = {}

        if committee is not None:
            data["expert_weights"] = {e.name: e.weight for e in committee.experts}

        if calibrator is not None:
            data["base_weights"] = dict(calibrator._base_weights)
            # Convert weight_history: list[tuple[float,float]] -> list[list[float,float]]
            data["weight_history"] = {
                name: [[ts, w] for ts, w in entries]
                for name, entries in calibrator._weight_history.items()
            }
        return data

    @staticmethod
    def _build_feedback_data(tracker: ExpertFeedbackTracker) -> dict:
        return {"scorecards": {name: sc.to_dict() for name, sc in tracker._scorecards.items()}}

    @staticmethod
    def _build_regime_data(regime_store: RegimeProfileStore) -> dict:
        profiles: dict[str, dict[str, dict]] = {}
        for expert_name, profile in regime_store.get_all_profiles().items():
            regimes: dict[str, dict] = {}
            for regime_name, ra in profile.get_all_regimes().items():
                regimes[regime_name] = ra.to_dict()
            profiles[expert_name] = regimes
        return {"profiles": profiles}

    @staticmethod
    def _build_patterns_data(learner: PatternLearner) -> dict:
        patterns: dict[str, dict] = {}
        for pid, pattern in learner._patterns.items():
            patterns[pid] = pattern.to_dict()
        return {"patterns": patterns}

    # ------------------------------------------------------------------
    # Restore helpers
    # ------------------------------------------------------------------

    def _restore_weights_to_committee(self, committee: InvestmentCommittee) -> bool:
        data = self._read_file("weights")
        if data is None:
            return False
        expert_weights = data.get("expert_weights", {})
        if not expert_weights:
            return False
        for expert in committee.experts:
            if expert.name in expert_weights:
                expert.weight = expert_weights[expert.name]
        return True

    def _restore_weights_to_calibrator(self, calibrator: WeightCalibrator) -> bool:
        data = self._read_file("weights")
        if data is None:
            return False

        restored = False
        if "base_weights" in data:
            calibrator._base_weights = dict(data["base_weights"])
            restored = True
        if "weight_history" in data:
            calibrator._weight_history = {
                name: [(ts, w) for ts, w in entries]
                for name, entries in data["weight_history"].items()
            }
            restored = True
        return restored

    def _restore_feedback(self, tracker: ExpertFeedbackTracker) -> bool:
        data = self._read_file("feedback")
        if data is None:
            return False
        scorecards_data = data.get("scorecards", {})
        if not scorecards_data:
            return False

        from bot.agents.feedback_tracker import ExpertScorecard

        for name, sc_data in scorecards_data.items():
            tracker._scorecards[name] = ExpertScorecard(
                expert_name=sc_data.get("expert_name", name),
                total_votes=sc_data.get("total_votes", 0),
                correct_votes=sc_data.get("correct_votes", 0),
                incorrect_votes=sc_data.get("incorrect_votes", 0),
                deferred_votes=sc_data.get("deferred_votes", 0),
                accuracy=sc_data.get("accuracy", 0.0),
                profit_when_approved=sc_data.get("profit_when_approved", 0.0),
                loss_when_approved=sc_data.get("loss_when_approved", 0.0),
                avoided_losses=sc_data.get("avoided_losses", 0.0),
            )
        return True

    def _restore_regime_profiles(self, regime_store: RegimeProfileStore) -> bool:
        data = self._read_file("regime_profiles")
        if data is None:
            return False
        profiles_data = data.get("profiles", {})
        if not profiles_data:
            return False

        from bot.agents.regime_profile import ExpertRegimeProfile, RegimeAccuracy

        for expert_name, regimes in profiles_data.items():
            profile = ExpertRegimeProfile(expert_name)
            for regime_name, ra_data in regimes.items():
                ra = RegimeAccuracy(
                    regime=ra_data.get("regime", regime_name),
                    total=ra_data.get("total", 0),
                    correct=ra_data.get("correct", 0),
                    incorrect=ra_data.get("incorrect", 0),
                    deferred=ra_data.get("deferred", 0),
                    accuracy=ra_data.get("accuracy", 0.0),
                    avg_pnl_when_approved=ra_data.get("avg_pnl_when_approved", 0.0),
                )
                profile._regime_stats[regime_name] = ra
            regime_store._profiles[expert_name] = profile
        return True

    def _restore_patterns(self, learner: PatternLearner) -> bool:
        data = self._read_file("patterns")
        if data is None:
            return False
        patterns_data = data.get("patterns", {})
        if not patterns_data:
            return False

        from bot.core.pattern_learner import Pattern

        for pid, p_data in patterns_data.items():
            pattern = Pattern(
                pattern_id=p_data.get("pattern_id", pid),
                name=p_data.get("name", ""),
                description=p_data.get("description", ""),
                conditions=p_data.get("conditions", {}),
                occurrences=p_data.get("occurrences", 0),
                wins=p_data.get("wins", 0),
                losses=p_data.get("losses", 0),
                total_pnl=p_data.get("total_pnl", 0.0),
                avg_pnl=p_data.get("avg_pnl", 0.0),
                win_rate=p_data.get("win_rate", 0.0),
                confidence=p_data.get("confidence", 0.0),
                last_seen=p_data.get("last_seen", 0.0),
                tags=p_data.get("tags", []),
            )
            learner._patterns[pid] = pattern
        return True

    # ------------------------------------------------------------------
    # File I/O helpers
    # ------------------------------------------------------------------

    def _atomic_write(self, component: str, data: dict) -> None:
        """Write data as JSON using atomic rename to prevent corruption."""
        path = self._state_dir / self._FILES[component]
        tmp_path = path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(str(tmp_path), str(path))
        except Exception:
            # Clean up tmp file on failure
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def _read_file(self, component: str) -> dict[Any, Any] | None:
        """Read a component JSON file. Returns None if not found or invalid."""
        path = self._state_dir / self._FILES[component]
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "agent_state_read_failed",
                extra={"component": component, "error": str(e)},
            )
            return None
