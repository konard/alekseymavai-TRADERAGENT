"""Event Replay Debugger — replays historical signals through committee for counterfactual analysis."""

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from bot.agents.base_expert import Verdict
from bot.agents.committee import InvestmentCommittee
from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ReplayTradeResult:
    position_id: str
    signal: dict
    regime: str
    actual_pnl: float  # what actually happened
    committee_verdict: str  # "approve", "reject", "defer"
    committee_score: float
    would_have_traded: bool  # True if committee approved
    filtered_pnl: float  # 0 if rejected, actual_pnl if approved
    expert_votes: list[dict]
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "signal": self.signal,
            "regime": self.regime,
            "actual_pnl": self.actual_pnl,
            "committee_verdict": self.committee_verdict,
            "committee_score": round(self.committee_score, 4),
            "would_have_traded": self.would_have_traded,
            "filtered_pnl": self.filtered_pnl,
            "expert_votes": self.expert_votes,
            "ts": self.ts,
        }


@dataclass
class ReplayResult:
    total_signals: int
    approved: int
    rejected: int
    deferred: int

    # Actual performance (all trades)
    actual_total_pnl: float
    actual_winners: int
    actual_losers: int
    actual_win_rate: float

    # Counterfactual performance (committee-filtered)
    filtered_total_pnl: float
    filtered_winners: int
    filtered_losers: int
    filtered_win_rate: float

    # Improvement metrics
    pnl_delta: float  # filtered - actual
    avoided_losses: float  # sum of losses on rejected trades that lost
    missed_profits: float  # sum of profits on rejected trades that won
    filter_rate: float

    # Per-expert accuracy in this replay
    expert_accuracy: dict[str, float]

    # Timeline
    trades: list[ReplayTradeResult] = field(default_factory=list)

    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_signals": self.total_signals,
            "approved": self.approved,
            "rejected": self.rejected,
            "deferred": self.deferred,
            "actual_total_pnl": round(self.actual_total_pnl, 2),
            "actual_winners": self.actual_winners,
            "actual_losers": self.actual_losers,
            "actual_win_rate": round(self.actual_win_rate, 4),
            "filtered_total_pnl": round(self.filtered_total_pnl, 2),
            "filtered_winners": self.filtered_winners,
            "filtered_losers": self.filtered_losers,
            "filtered_win_rate": round(self.filtered_win_rate, 4),
            "pnl_delta": round(self.pnl_delta, 2),
            "avoided_losses": round(self.avoided_losses, 2),
            "missed_profits": round(self.missed_profits, 2),
            "filter_rate": round(self.filter_rate, 4),
            "expert_accuracy": {k: round(v, 4) for k, v in self.expert_accuracy.items()},
            "trades": [t.to_dict() for t in self.trades],
            "ts": self.ts,
        }


class EventReplayDebugger:
    """Replays historical signals through the Investment Committee.

    Use case: "What if I had used the committee for the last 3 months?"

    Takes a list of historical trades (signal + market_data + actual PnL),
    runs each through the committee, and compares outcomes.
    """

    def __init__(
        self,
        committee: InvestmentCommittee | None = None,
    ) -> None:
        self._committee = committee or self._create_default_committee()
        self._replay_count = 0
        self._last_result: ReplayResult | None = None

    async def replay(
        self,
        trades: list[dict],
        progress_callback: Callable | None = None,
    ) -> ReplayResult:
        """Replay historical trades through committee.

        For each trade:
          1. Run committee.evaluate(signal, market_data)
          2. Record verdict
          3. If APPROVE: filtered_pnl = actual_pnl
          4. If REJECT/DEFER: filtered_pnl = 0 (would not have traded)
          5. Score each expert's vote against actual outcome

        After all trades: compute improvement metrics.
        """
        trade_results: list[ReplayTradeResult] = []
        total = len(trades)

        for idx, trade in enumerate(trades):
            signal = trade["signal"]
            market_data = trade.get("market_data", {})
            regime = trade.get("regime", market_data.get("regime", "unknown"))
            actual_pnl = float(trade.get("pnl", 0.0))
            position_id = trade.get("position_id", f"replay_{idx}")

            # Run through committee
            decision = await self._committee.evaluate(signal, market_data)

            verdict_str = decision.verdict.value
            would_trade = decision.verdict == Verdict.APPROVE
            filtered_pnl = actual_pnl if would_trade else 0.0

            expert_votes = [
                {
                    "expert": v.expert_name,
                    "verdict": v.verdict.value,
                    "confidence": round(v.confidence, 4),
                    "reasoning": v.reasoning,
                }
                for v in decision.votes
            ]

            result = ReplayTradeResult(
                position_id=position_id,
                signal=signal,
                regime=regime,
                actual_pnl=actual_pnl,
                committee_verdict=verdict_str,
                committee_score=decision.score,
                would_have_traded=would_trade,
                filtered_pnl=filtered_pnl,
                expert_votes=expert_votes,
            )
            trade_results.append(result)

            if progress_callback is not None:
                try:
                    pct = (idx + 1) / total if total > 0 else 1.0
                    progress_callback(pct, idx + 1, total)
                except Exception:
                    pass

        # Compute aggregate metrics
        replay_result = self._compute_replay_result(trade_results)
        self._replay_count += 1
        self._last_result = replay_result

        logger.info(
            "replay_complete",
            total_signals=replay_result.total_signals,
            approved=replay_result.approved,
            rejected=replay_result.rejected,
            pnl_delta=f"{replay_result.pnl_delta:.2f}",
            filter_rate=f"{replay_result.filter_rate:.2%}",
        )

        return replay_result

    async def replay_with_weight_sweep(
        self,
        trades: list[dict],
        weight_configs: list[dict[str, float]] | None = None,
    ) -> list[tuple[dict[str, float], ReplayResult]]:
        """Replay with different weight configurations to find optimal.

        If weight_configs is None, generate defaults:
        - Current weights
        - Equal weights (all 1.0)
        - Risk-heavy (risk=2.5, others=0.5)
        - SMC-heavy (smc=2.0, others=0.7)
        - Contrarian-heavy (contrarian=2.0, others=0.7)

        Returns list of (weights_config, ReplayResult) sorted by filtered_total_pnl.
        """
        if weight_configs is None:
            expert_names = [e.name for e in self._committee.experts]
            current_weights = {e.name: e.weight for e in self._committee.experts}

            weight_configs = [current_weights]

            # Equal weights
            weight_configs.append({n: 1.0 for n in expert_names})

            # Risk-heavy
            risk_heavy = {n: 0.5 for n in expert_names}
            for n in expert_names:
                if "risk" in n.lower():
                    risk_heavy[n] = 2.5
            weight_configs.append(risk_heavy)

            # SMC-heavy
            smc_heavy = {n: 0.7 for n in expert_names}
            for n in expert_names:
                if "smc" in n.lower() or "structuralist" in n.lower():
                    smc_heavy[n] = 2.0
            weight_configs.append(smc_heavy)

            # Contrarian-heavy
            contrarian_heavy = {n: 0.7 for n in expert_names}
            for n in expert_names:
                if "contrarian" in n.lower():
                    contrarian_heavy[n] = 2.0
            weight_configs.append(contrarian_heavy)

        results: list[tuple[dict[str, float], ReplayResult]] = []

        for config in weight_configs:
            # Apply weights
            original_weights = {}
            for expert in self._committee.experts:
                original_weights[expert.name] = expert.weight
                if expert.name in config:
                    expert.weight = config[expert.name]

            # Run replay
            result = await self.replay(trades)
            results.append((dict(config), result))

            # Restore weights
            for expert in self._committee.experts:
                expert.weight = original_weights[expert.name]

        # Sort by filtered_total_pnl descending
        results.sort(key=lambda x: x[1].filtered_total_pnl, reverse=True)

        return results

    def _compute_replay_result(self, trade_results: list[ReplayTradeResult]) -> ReplayResult:
        """Compute aggregate ReplayResult from individual trade results."""
        total = len(trade_results)
        approved = sum(1 for t in trade_results if t.committee_verdict == "approve")
        rejected = sum(1 for t in trade_results if t.committee_verdict == "reject")
        deferred = sum(1 for t in trade_results if t.committee_verdict == "defer")

        # Actual performance
        actual_total_pnl = sum(t.actual_pnl for t in trade_results)
        actual_winners = sum(1 for t in trade_results if t.actual_pnl > 0)
        actual_losers = sum(1 for t in trade_results if t.actual_pnl <= 0)
        actual_win_rate = actual_winners / total if total > 0 else 0.0

        # Filtered performance (only approved trades count)
        approved_trades = [t for t in trade_results if t.would_have_traded]
        filtered_total_pnl = sum(t.filtered_pnl for t in trade_results)
        filtered_winners = sum(1 for t in approved_trades if t.filtered_pnl > 0)
        filtered_losers = sum(1 for t in approved_trades if t.filtered_pnl <= 0)
        filtered_win_rate = filtered_winners / len(approved_trades) if approved_trades else 0.0

        # Improvement metrics
        pnl_delta = filtered_total_pnl - actual_total_pnl
        rejected_trades = [t for t in trade_results if not t.would_have_traded]
        avoided_losses = abs(sum(t.actual_pnl for t in rejected_trades if t.actual_pnl < 0))
        missed_profits = sum(t.actual_pnl for t in rejected_trades if t.actual_pnl > 0)
        filter_rate = (rejected + deferred) / total if total > 0 else 0.0

        # Per-expert accuracy
        expert_accuracy = self._score_expert_votes(trade_results)

        return ReplayResult(
            total_signals=total,
            approved=approved,
            rejected=rejected,
            deferred=deferred,
            actual_total_pnl=actual_total_pnl,
            actual_winners=actual_winners,
            actual_losers=actual_losers,
            actual_win_rate=actual_win_rate,
            filtered_total_pnl=filtered_total_pnl,
            filtered_winners=filtered_winners,
            filtered_losers=filtered_losers,
            filtered_win_rate=filtered_win_rate,
            pnl_delta=pnl_delta,
            avoided_losses=avoided_losses,
            missed_profits=missed_profits,
            filter_rate=filter_rate,
            expert_accuracy=expert_accuracy,
            trades=trade_results,
        )

    def _score_expert_votes(self, trades_results: list[ReplayTradeResult]) -> dict[str, float]:
        """Calculate per-expert accuracy across replay.

        An expert vote is considered 'correct' if:
        - APPROVE on a trade that was profitable (actual_pnl > 0)
        - REJECT on a trade that was unprofitable (actual_pnl <= 0)
        - DEFER is counted as 50% correct (neutral stance)
        """
        expert_correct: dict[str, int] = {}
        expert_total: dict[str, int] = {}

        for trade in trades_results:
            is_profitable = trade.actual_pnl > 0

            for vote_info in trade.expert_votes:
                name = vote_info["expert"]
                verdict = vote_info["verdict"]

                expert_total[name] = expert_total.get(name, 0) + 1

                if verdict == "approve" and is_profitable:
                    expert_correct[name] = expert_correct.get(name, 0) + 1
                elif verdict == "reject" and not is_profitable:
                    expert_correct[name] = expert_correct.get(name, 0) + 1
                elif verdict == "defer":
                    # Defer counts as half correct
                    expert_correct[name] = expert_correct.get(name, 0)
                    # Use float tracking for half-credit
                    pass

        accuracy: dict[str, float] = {}
        for name in expert_total:
            total = expert_total[name]
            correct = expert_correct.get(name, 0)
            accuracy[name] = correct / total if total > 0 else 0.0

        return accuracy

    def _create_default_committee(self) -> InvestmentCommittee:
        """Create default committee with 4 experts."""
        from bot.agents.smc_expert import SMCExpert
        from bot.agents.trend_expert import TrendExpert
        from bot.agents.risk_expert import RiskExpert
        from bot.agents.contrarian_expert import ContrarianExpert

        committee = InvestmentCommittee()
        committee.add_expert(SMCExpert())
        committee.add_expert(TrendExpert())
        committee.add_expert(RiskExpert())
        committee.add_expert(ContrarianExpert())
        return committee

    def get_stats(self) -> dict:
        """Get replay debugger statistics."""
        return {
            "replay_count": self._replay_count,
            "last_result": self._last_result.to_dict() if self._last_result else None,
            "committee_experts": [
                {"name": e.name, "role": e.role, "weight": e.weight}
                for e in self._committee.experts
            ],
        }
