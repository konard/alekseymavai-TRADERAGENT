"""Committee Backtest Adapter — gates signals through Investment Committee during backtest."""

from __future__ import annotations

import logging
import uuid
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from bot.agents.committee import InvestmentCommittee
    from bot.agents.feedback_tracker import ExpertFeedbackTracker
    from bot.agents.regime_profile import RegimeProfileStore
    from bot.agents.weight_calibrator import WeightCalibrator

from bot.agents.base_expert import Verdict

logger = logging.getLogger(__name__)


class CommitteeBacktestAdapter:
    """Gates trading signals through the Investment Committee during backtests.

    Wraps InvestmentCommittee.evaluate() to provide:
    - Signal gating (APPROVE / REJECT / DEFER)
    - Outcome recording via ExpertFeedbackTracker
    - Periodic weight recalibration via WeightCalibrator
    - Regime-specific outcome tracking via RegimeProfileStore
    """

    def __init__(
        self,
        committee: "InvestmentCommittee",
        feedback_tracker: "ExpertFeedbackTracker",
        regime_store: "RegimeProfileStore | None" = None,
        calibrator: "WeightCalibrator | None" = None,
        calibrate_every_n: int = 20,
    ) -> None:
        self._committee = committee
        self._tracker = feedback_tracker
        self._regime_store = regime_store
        self._calibrator = calibrator
        self._calibrate_every_n = calibrate_every_n

        self._session_to_position: dict[str, str] = {}
        self._decisions_since_calibration: int = 0

        # Counters for stats
        self._total_evaluated: int = 0
        self._approved: int = 0
        self._rejected: int = 0
        self._deferred: int = 0

    async def should_execute(
        self,
        signal: dict[str, Any],
        market_data: dict[str, Any],
        current_regime: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        """Gate: evaluate signal through committee.

        Returns (should_execute, decision_dict):
        - True if committee verdict is APPROVE
        - False if REJECT or DEFER

        Also: records decision in feedback_tracker with position_id from signal.
        Triggers calibration if calibrate_every_n reached.
        """
        decision = await self._committee.evaluate(signal, market_data)

        # Generate a position_id for tracking
        position_id = signal.get("position_id", f"bt_pos_{uuid.uuid4().hex[:12]}")

        # Map session -> position for later outcome recording
        self._session_to_position[decision.session_id] = position_id

        # Record decision in feedback tracker
        self._tracker.record_decision(
            decision=decision,
            position_id=position_id,
            regime=current_regime,
            market_data=market_data,
        )

        # Update counters
        self._total_evaluated += 1
        if decision.verdict == Verdict.APPROVE:
            self._approved += 1
        elif decision.verdict == Verdict.REJECT:
            self._rejected += 1
        else:
            self._deferred += 1

        # Check if calibration is due
        self._decisions_since_calibration += 1
        if (
            self._calibrator is not None
            and self._decisions_since_calibration >= self._calibrate_every_n
        ):
            self._run_calibration(current_regime)

        should_exec = decision.verdict == Verdict.APPROVE
        return should_exec, decision.to_dict()

    def record_outcome(self, position_id: str, pnl: float, regime: str = "") -> None:
        """Score all expert votes for this closed position.

        - Calls feedback_tracker.record_outcome()
        - If regime_store: record regime-specific outcomes
        - If calibrator + calibrate_every_n reached: recalibrate weights
        """
        # Score expert votes via feedback tracker
        results = self._tracker.record_outcome(position_id, pnl)

        # Record regime-specific outcomes if regime_store is available
        if self._regime_store and regime and results:
            # Find the decision that corresponds to this position
            session_id = None
            for sid, pid in self._session_to_position.items():
                if pid == position_id:
                    session_id = sid
                    break

            for expert_name, was_correct in results:
                # Find the vote verdict for this expert
                verdict = Verdict.APPROVE  # default
                if session_id and session_id in self._tracker._pending_decisions:
                    pending = self._tracker._pending_decisions[session_id]
                    decision = pending["decision"]
                    for vote in decision.votes:
                        if vote.expert_name == expert_name:
                            verdict = vote.verdict
                            break

                self._regime_store.record_outcome(
                    expert_name=expert_name,
                    regime=regime,
                    verdict=verdict,
                    was_correct=was_correct,
                    pnl=pnl,
                )

            # Clean up session mapping
            if session_id:
                self._session_to_position.pop(session_id, None)

    def _run_calibration(self, current_regime: str = "") -> None:
        """Run weight calibration and reset counter."""
        if self._calibrator is None:
            return

        self._calibrator.calibrate(
            committee=self._committee,
            tracker=self._tracker,
            regime_store=self._regime_store,
            current_regime=current_regime or None,
        )
        self._decisions_since_calibration = 0

        logger.info(
            "committee_weights_calibrated",
            extra={
                "calibration_count": self._calibrator._calibration_count,
                "total_evaluated": self._total_evaluated,
            },
        )

    def get_stats(self) -> dict[str, Any]:
        """Summary: total_evaluated, approved, rejected, deferred, filter_rate."""
        total = self._total_evaluated
        return {
            "total_evaluated": total,
            "approved": self._approved,
            "rejected": self._rejected,
            "deferred": self._deferred,
            "filter_rate": ((self._rejected + self._deferred) / total if total > 0 else 0.0),
        }
