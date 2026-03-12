from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class TradeRecord:
    trade_id: str
    strategy: str
    symbol: str
    direction: str  # "long" | "short"
    entry_price: float
    exit_price: float
    size: float
    entry_ts: float
    exit_ts: float
    fees: float = 0.0
    market_return: float = 0.0  # benchmark return over same period

    @property
    def direction_sign(self) -> float:
        return 1.0 if self.direction == "long" else -1.0

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.size * self.direction_sign


class PnLDecomposer:

    def __init__(self) -> None:
        self._trades: list[TradeRecord] = []

    def add_trade(self, trade: TradeRecord) -> None:
        self._trades.append(trade)

    def _decompose_trades(self, trades: list[TradeRecord]) -> dict:
        if not trades:
            return {
                "total_pnl": 0.0,
                "alpha": 0.0,
                "beta_pnl": 0.0,
                "timing_pnl": 0.0,
                "selection_pnl": 0.0,
                "fees_total": 0.0,
                "net_pnl": 0.0,
                "alpha_pct": 0.0,
            }

        total_pnl = sum(t.pnl for t in trades)
        beta_pnl = sum(t.market_return * t.size * t.direction_sign for t in trades)
        alpha = total_pnl - beta_pnl
        fees_total = sum(t.fees for t in trades)
        net_pnl = total_pnl - fees_total

        # Timing: compare actual pnl vs pnl if entered at midpoint price.
        # midpoint = (entry + exit) / 2
        # pnl_at_midpoint for long = (exit - midpoint) * size = (exit - entry) / 2 * size
        # timing = actual_pnl - pnl_at_midpoint
        timing_pnl = 0.0
        for t in trades:
            midpoint = (t.entry_price + t.exit_price) / 2.0
            pnl_at_midpoint = (t.exit_price - midpoint) * t.size * t.direction_sign
            timing_pnl += t.pnl - pnl_at_midpoint

        # Selection: compare each symbol's average pnl vs portfolio average.
        symbol_pnls: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            symbol_pnls[t.symbol].append(t.pnl)

        portfolio_avg_pnl = total_pnl / len(trades)
        selection_pnl = 0.0
        for symbol, pnls in symbol_pnls.items():
            symbol_avg = sum(pnls) / len(pnls)
            selection_pnl += (symbol_avg - portfolio_avg_pnl) * len(pnls)

        alpha_pct = (alpha / abs(total_pnl) * 100.0) if total_pnl != 0.0 else 0.0

        return {
            "total_pnl": total_pnl,
            "alpha": alpha,
            "beta_pnl": beta_pnl,
            "timing_pnl": timing_pnl,
            "selection_pnl": selection_pnl,
            "fees_total": fees_total,
            "net_pnl": net_pnl,
            "alpha_pct": alpha_pct,
        }

    def decompose(self, strategy: str | None = None) -> dict:
        if strategy is not None:
            filtered = [t for t in self._trades if t.strategy == strategy]
        else:
            filtered = self._trades

        result = self._decompose_trades(filtered)

        # Build per-strategy breakdown.
        strategies: dict[str, list[TradeRecord]] = defaultdict(list)
        for t in filtered:
            strategies[t.strategy].append(t)

        per_strategy: dict[str, dict] = {}
        for strat_name, strat_trades in strategies.items():
            per_strategy[strat_name] = self._decompose_trades(strat_trades)

        result["per_strategy"] = per_strategy
        return result

    def get_strategy_comparison(self) -> list[dict]:
        strategies: dict[str, list[TradeRecord]] = defaultdict(list)
        for t in self._trades:
            strategies[t.strategy].append(t)

        comparisons: list[dict] = []
        for strat_name, strat_trades in strategies.items():
            decomp = self._decompose_trades(strat_trades)
            decomp["strategy"] = strat_name
            decomp["trade_count"] = len(strat_trades)
            comparisons.append(decomp)

        comparisons.sort(key=lambda x: x["total_pnl"], reverse=True)
        return comparisons

    def get_stats(self) -> dict:
        total = len(self._trades)
        if total == 0:
            return {
                "total_trades": 0,
                "strategies": [],
                "symbols": [],
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0.0,
            }
        winning = sum(1 for t in self._trades if t.pnl > 0)
        losing = sum(1 for t in self._trades if t.pnl < 0)
        strategies = sorted(set(t.strategy for t in self._trades))
        symbols = sorted(set(t.symbol for t in self._trades))
        return {
            "total_trades": total,
            "strategies": strategies,
            "symbols": symbols,
            "winning_trades": winning,
            "losing_trades": losing,
            "win_rate": winning / total if total else 0.0,
        }


@dataclass
class ToolUsageRecord:
    tool_name: str
    agent_name: str
    execution_time: float  # seconds
    success: bool
    ts: float


@dataclass
class _DecisionRecord:
    agent_name: str
    decision: str
    outcome: str
    confidence: float
    ts: float


class AgentProfiler:

    def __init__(self) -> None:
        self._tool_uses: list[ToolUsageRecord] = []
        self._decisions: list[_DecisionRecord] = []

    def record_tool_use(self, record: ToolUsageRecord) -> None:
        self._tool_uses.append(record)

    def record_decision(
        self,
        agent_name: str,
        decision: str,
        outcome: str,
        confidence: float,
        ts: float | None = None,
    ) -> None:
        self._decisions.append(
            _DecisionRecord(
                agent_name=agent_name,
                decision=decision,
                outcome=outcome,
                confidence=confidence,
                ts=ts if ts is not None else time.time(),
            )
        )

    def get_agent_profile(self, agent_name: str) -> dict:
        agent_tools = [r for r in self._tool_uses if r.agent_name == agent_name]
        agent_decisions = [d for d in self._decisions if d.agent_name == agent_name]

        total_tool_uses = len(agent_tools)

        # Tool breakdown
        tool_breakdown: dict[str, int] = defaultdict(int)
        for r in agent_tools:
            tool_breakdown[r.tool_name] += 1

        # Avg response time
        avg_response_time = (
            sum(r.execution_time for r in agent_tools) / total_tool_uses
            if total_tool_uses
            else 0.0
        )

        # Success rate
        success_count = sum(1 for r in agent_tools if r.success)
        success_rate = success_count / total_tool_uses if total_tool_uses else 0.0

        # Most used tool
        most_used_tool = (
            max(tool_breakdown, key=tool_breakdown.get) if tool_breakdown else ""
        )

        # Fastest tool (lowest avg execution time)
        tool_times: dict[str, list[float]] = defaultdict(list)
        for r in agent_tools:
            tool_times[r.tool_name].append(r.execution_time)
        fastest_tool = ""
        if tool_times:
            fastest_tool = min(
                tool_times,
                key=lambda name: sum(tool_times[name]) / len(tool_times[name]),
            )

        # Decision accuracy: "correct" outcomes / total
        total_decisions = len(agent_decisions)
        correct_decisions = sum(
            1 for d in agent_decisions if d.outcome == "correct"
        )
        decision_accuracy = (
            correct_decisions / total_decisions if total_decisions else 0.0
        )

        # Confidence calibration: Pearson correlation between confidence
        # and binary success (outcome == "correct" → 1, else 0).
        confidence_calibration = self._pearson_correlation(agent_decisions)

        return {
            "total_tool_uses": total_tool_uses,
            "tool_breakdown": dict(tool_breakdown),
            "avg_response_time": avg_response_time,
            "success_rate": success_rate,
            "total_decisions": total_decisions,
            "decision_accuracy": decision_accuracy,
            "confidence_calibration": confidence_calibration,
            "most_used_tool": most_used_tool,
            "fastest_tool": fastest_tool,
        }

    @staticmethod
    def _pearson_correlation(decisions: list[_DecisionRecord]) -> float:
        """Pearson correlation between confidence and binary success."""
        if len(decisions) < 2:
            return 0.0

        xs = [d.confidence for d in decisions]
        ys = [1.0 if d.outcome == "correct" else 0.0 for d in decisions]

        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n

        cov = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
        var_x = sum((x - mean_x) ** 2 for x in xs)
        var_y = sum((y - mean_y) ** 2 for y in ys)

        denom = math.sqrt(var_x * var_y)
        if denom == 0.0:
            return 0.0
        return cov / denom

    def get_all_profiles(self) -> dict[str, dict]:
        agents: set[str] = set()
        for r in self._tool_uses:
            agents.add(r.agent_name)
        for d in self._decisions:
            agents.add(d.agent_name)

        return {agent: self.get_agent_profile(agent) for agent in sorted(agents)}

    def get_tool_leaderboard(self) -> list[dict]:
        tool_records: dict[str, list[ToolUsageRecord]] = defaultdict(list)
        for r in self._tool_uses:
            tool_records[r.tool_name].append(r)

        leaderboard: list[dict] = []
        for tool_name, records in tool_records.items():
            total = len(records)
            successes = sum(1 for r in records if r.success)
            avg_time = sum(r.execution_time for r in records) / total
            leaderboard.append(
                {
                    "tool_name": tool_name,
                    "usage_count": total,
                    "success_rate": successes / total if total else 0.0,
                    "avg_execution_time": avg_time,
                }
            )

        leaderboard.sort(key=lambda x: x["usage_count"], reverse=True)
        return leaderboard

    def get_decision_breakdown(self, agent_name: str) -> dict:
        agent_decisions = [d for d in self._decisions if d.agent_name == agent_name]

        grouped: dict[str, list[_DecisionRecord]] = defaultdict(list)
        for d in agent_decisions:
            grouped[d.decision].append(d)

        breakdown: dict[str, dict] = {}
        for decision_type, decisions in grouped.items():
            total = len(decisions)
            correct = sum(1 for d in decisions if d.outcome == "correct")
            breakdown[decision_type] = {
                "total": total,
                "correct": correct,
                "accuracy": correct / total if total else 0.0,
                "avg_confidence": (
                    sum(d.confidence for d in decisions) / total if total else 0.0
                ),
            }

        return breakdown

    def get_stats(self) -> dict:
        agents: set[str] = set()
        for r in self._tool_uses:
            agents.add(r.agent_name)
        for d in self._decisions:
            agents.add(d.agent_name)

        tools: set[str] = set()
        for r in self._tool_uses:
            tools.add(r.tool_name)

        return {
            "total_agents": len(agents),
            "total_tool_uses": len(self._tool_uses),
            "total_decisions": len(self._decisions),
            "unique_tools": len(tools),
            "agents": sorted(agents),
        }
