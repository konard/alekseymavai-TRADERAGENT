"""Committee Backtest Engine — runs paired backtests to measure committee impact."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bot.agents.base_expert import BaseExpert
from bot.agents.committee import InvestmentCommittee
from bot.agents.contrarian_expert import ContrarianExpert
from bot.agents.feedback_tracker import ExpertFeedbackTracker
from bot.agents.regime_profile import RegimeProfileStore
from bot.agents.risk_expert import RiskExpert
from bot.agents.smc_expert import SMCExpert
from bot.agents.trend_expert import TrendExpert
from bot.agents.weight_calibrator import WeightCalibrator
from bot.tests.backtesting.committee_backtest_adapter import CommitteeBacktestAdapter

logger = logging.getLogger(__name__)


@dataclass
class CommitteeBacktestResult:
    """Result of a paired backtest comparing baseline vs committee-filtered trading."""

    baseline_pnl: float = 0.0
    committee_pnl: float = 0.0
    pnl_delta: float = 0.0
    baseline_trades: int = 0
    committee_trades: int = 0
    committee_approved: int = 0
    committee_rejected: int = 0
    committee_deferred: int = 0
    filter_rate: float = 0.0
    per_expert_accuracy: dict[str, float] = field(default_factory=dict)
    weight_evolution: dict[str, list[float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_pnl": round(self.baseline_pnl, 4),
            "committee_pnl": round(self.committee_pnl, 4),
            "pnl_delta": round(self.pnl_delta, 4),
            "baseline_trades": self.baseline_trades,
            "committee_trades": self.committee_trades,
            "committee_approved": self.committee_approved,
            "committee_rejected": self.committee_rejected,
            "committee_deferred": self.committee_deferred,
            "filter_rate": round(self.filter_rate, 4),
            "per_expert_accuracy": {k: round(v, 4) for k, v in self.per_expert_accuracy.items()},
            "weight_evolution": {
                k: [round(w, 4) for w in v] for k, v in self.weight_evolution.items()
            },
        }


class CommitteeBacktestEngine:
    """Runs paired backtests to measure committee impact on trading performance.

    Compares a baseline (all signals executed) against a committee-filtered
    version (only APPROVED signals executed) to quantify the value of the
    Investment Committee as a signal filter.
    """

    def __init__(
        self,
        experts: list[BaseExpert] | None = None,
        min_samples_for_calibration: int = 10,
        calibrate_every_n: int = 20,
    ) -> None:
        self._experts = experts
        self._min_samples_for_calibration = min_samples_for_calibration
        self._calibrate_every_n = calibrate_every_n
        self._adapter: CommitteeBacktestAdapter | None = None
        self._committee: InvestmentCommittee | None = None

    async def run_paired(
        self,
        signals: list[dict[str, Any]],
    ) -> CommitteeBacktestResult:
        """Run paired comparison: all signals (baseline) vs committee-filtered.

        For each signal in signals:
          1. Baseline: always count it (add pnl to baseline_pnl, increment baseline_trades)
          2. Committee: call committee.evaluate(), if APPROVE -> add pnl, else skip
          3. Record outcome for scoring
          4. Periodically calibrate weights

        Args:
            signals: list of dicts, each with keys:
                - signal: trading signal dict
                - market_data: market context dict
                - regime: current market regime string
                - pnl: known outcome (profit/loss) of this trade

        Returns:
            CommitteeBacktestResult with comparison metrics.
        """
        # Create committee and adapter
        if self._experts:
            committee = InvestmentCommittee(experts=list(self._experts))
            experts = list(self._experts)
        else:
            committee, experts = self._create_default_committee()

        self._committee = committee

        tracker = ExpertFeedbackTracker()
        regime_store = RegimeProfileStore()
        calibrator = WeightCalibrator(
            min_samples=self._min_samples_for_calibration,
        )

        adapter = CommitteeBacktestAdapter(
            committee=committee,
            feedback_tracker=tracker,
            regime_store=regime_store,
            calibrator=calibrator,
            calibrate_every_n=self._calibrate_every_n,
        )
        self._adapter = adapter

        # Track weight evolution
        weight_snapshots: dict[str, list[float]] = {e.name: [e.weight] for e in experts}

        baseline_pnl = 0.0
        committee_pnl = 0.0
        baseline_trades = 0

        for i, entry in enumerate(signals):
            signal = entry["signal"]
            market_data = entry["market_data"]
            regime = entry.get("regime", "")
            pnl = entry.get("pnl", 0.0)

            # Baseline: always execute
            baseline_pnl += pnl
            baseline_trades += 1

            # Committee: gate through adapter
            should_exec, decision_dict = await adapter.should_execute(
                signal=signal,
                market_data=market_data,
                current_regime=regime,
            )

            # Generate position_id for outcome tracking
            position_id = signal.get("position_id", f"bt_pos_{i}")

            if should_exec:
                committee_pnl += pnl

            # Record outcome for scoring (regardless of approval)
            adapter.record_outcome(
                position_id=position_id,
                pnl=pnl,
                regime=regime,
            )

            # Record weight snapshot after potential calibration
            for expert in experts:
                weight_snapshots[expert.name].append(expert.weight)

        # Gather stats
        adapter_stats = adapter.get_stats()

        # Per-expert accuracy
        per_expert_accuracy: dict[str, float] = {}
        for expert in experts:
            scorecard = tracker.get_scorecard(expert.name)
            per_expert_accuracy[expert.name] = scorecard.accuracy

        total = adapter_stats["total_evaluated"]
        filter_rate = adapter_stats["filter_rate"]

        result = CommitteeBacktestResult(
            baseline_pnl=baseline_pnl,
            committee_pnl=committee_pnl,
            pnl_delta=committee_pnl - baseline_pnl,
            baseline_trades=baseline_trades,
            committee_trades=adapter_stats["approved"],
            committee_approved=adapter_stats["approved"],
            committee_rejected=adapter_stats["rejected"],
            committee_deferred=adapter_stats["deferred"],
            filter_rate=filter_rate,
            per_expert_accuracy=per_expert_accuracy,
            weight_evolution=weight_snapshots,
        )

        logger.info(
            "committee_backtest_complete",
            extra={
                "baseline_pnl": f"{baseline_pnl:.2f}",
                "committee_pnl": f"{committee_pnl:.2f}",
                "pnl_delta": f"{result.pnl_delta:.2f}",
                "filter_rate": f"{filter_rate:.2%}",
                "baseline_trades": baseline_trades,
                "committee_trades": result.committee_trades,
            },
        )

        return result

    def _create_default_committee(
        self,
    ) -> tuple[InvestmentCommittee, list[BaseExpert]]:
        """Create committee with 4 default experts."""
        experts: list[BaseExpert] = [
            SMCExpert(),
            TrendExpert(),
            RiskExpert(),
            ContrarianExpert(),
        ]
        committee = InvestmentCommittee(experts=experts)
        return committee, experts

    def get_stats(self) -> dict[str, Any]:
        """Return adapter stats if available."""
        if self._adapter is not None:
            return self._adapter.get_stats()
        return {
            "total_evaluated": 0,
            "approved": 0,
            "rejected": 0,
            "deferred": 0,
            "filter_rate": 0.0,
        }
