from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone
from math import comb
from typing import Any

from bot.agents.tools.market_data import BaseTool, ToolResult


def _binomial_p_value(wins: int, n: int, p0: float = 0.5) -> float:
    """One-sided binomial test: P(X >= wins) under H0: p = p0."""
    p_val = sum(comb(n, k) * p0**k * (1 - p0) ** (n - k) for k in range(wins, n + 1))
    return p_val


# ---------------------------------------------------------------------------
# HypothesisSkill
# ---------------------------------------------------------------------------

class HypothesisSkill(BaseTool):
    """Expert formulates and validates testable hypotheses about market behavior."""

    name = "hypothesis"
    description = "Create, validate and query market hypotheses"

    def __init__(self) -> None:
        self._hypotheses: dict[str, dict[str, Any]] = {}

    # -- public API ---------------------------------------------------------

    def execute(self, params: dict[str, Any]) -> ToolResult:
        """Create a new hypothesis.

        Parameters
        ----------
        params : dict
            expert_name : str
            hypothesis : str
            test_conditions : dict
            expected_outcome : str
            confidence : float  (0-1)
        """
        required = ("expert_name", "hypothesis", "test_conditions", "expected_outcome", "confidence")
        missing = [k for k in required if k not in params]
        if missing:
            return ToolResult(success=False, error=f"Missing required fields: {missing}")

        hyp_id = str(uuid.uuid4())
        entry: dict[str, Any] = {
            "id": hyp_id,
            "expert_name": params["expert_name"],
            "hypothesis": params["hypothesis"],
            "test_conditions": params["test_conditions"],
            "expected_outcome": params["expected_outcome"],
            "confidence": float(params["confidence"]),
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "validation_result": None,
        }
        self._hypotheses[hyp_id] = entry
        return ToolResult(success=True, data={"hypothesis_id": hyp_id, **entry})

    def validate_hypothesis(self, hypothesis_id: str, actual_outcomes: list[dict]) -> dict:
        """Validate a hypothesis against observed outcomes.

        Each outcome dict: ``{"matched_conditions": bool, "result": "win"|"loss", "pnl": float}``

        Returns validation summary dict.
        """
        if hypothesis_id not in self._hypotheses:
            return {"error": f"Hypothesis {hypothesis_id} not found"}

        hyp = self._hypotheses[hypothesis_id]
        matched = [o for o in actual_outcomes if o.get("matched_conditions")]
        sample_size = len(matched)
        wins = sum(1 for o in matched if o.get("result") == "win")
        win_rate = wins / sample_size if sample_size > 0 else 0.0
        avg_pnl = (
            sum(o.get("pnl", 0.0) for o in matched) / sample_size
            if sample_size > 0
            else 0.0
        )
        p_value = _binomial_p_value(wins, sample_size) if sample_size > 0 else 1.0

        # Determine status
        if sample_size >= 10 and win_rate > 0.55 and p_value < 0.1:
            status = "confirmed"
        elif sample_size >= 10:
            status = "rejected"
        else:
            status = "testing"

        hyp["status"] = status
        validation = {
            "sample_size": sample_size,
            "wins": wins,
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "p_value": p_value,
            "status": status,
        }
        hyp["validation_result"] = validation
        return validation

    def get_hypotheses(
        self,
        expert_name: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Return hypotheses, optionally filtered by expert and/or status."""
        results = list(self._hypotheses.values())
        if expert_name is not None:
            results = [h for h in results if h["expert_name"] == expert_name]
        if status is not None:
            results = [h for h in results if h["status"] == status]
        return results

    def get_stats(self) -> dict:
        statuses = Counter(h["status"] for h in self._hypotheses.values())
        return {
            "total": len(self._hypotheses),
            "confirmed": statuses.get("confirmed", 0),
            "rejected": statuses.get("rejected", 0),
            "pending": statuses.get("pending", 0),
            "testing": statuses.get("testing", 0),
        }


# ---------------------------------------------------------------------------
# JournalSkill
# ---------------------------------------------------------------------------

class JournalSkill(BaseTool):
    """Trading journal: records decisions, reasoning, and post-trade reflections."""

    name = "journal"
    description = "Record trade decisions, reflect on outcomes, query history"

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}  # keyed by trade_id

    # -- public API ---------------------------------------------------------

    def execute(self, params: dict[str, Any]) -> ToolResult:
        action = params.get("action")
        if action == "record":
            return self._record(params)
        elif action == "reflect":
            return self._reflect(params)
        elif action == "query":
            return self._query(params)
        return ToolResult(success=False, error=f"Unknown action: {action}")

    # -- internals ----------------------------------------------------------

    def _record(self, params: dict[str, Any]) -> ToolResult:
        required = ("trade_id", "expert_name", "decision", "reasoning", "market_context", "confidence")
        missing = [k for k in required if k not in params]
        if missing:
            return ToolResult(success=False, error=f"Missing required fields: {missing}")

        trade_id = params["trade_id"]
        entry: dict[str, Any] = {
            "trade_id": trade_id,
            "expert_name": params["expert_name"],
            "decision": params["decision"],
            "reasoning": params["reasoning"],
            "market_context": params["market_context"],
            "confidence": float(params["confidence"]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reflection": None,
        }
        self._entries[trade_id] = entry
        return ToolResult(success=True, data=entry)

    def _reflect(self, params: dict[str, Any]) -> ToolResult:
        required = ("trade_id", "outcome", "pnl", "lesson", "what_was_right", "what_was_wrong")
        missing = [k for k in required if k not in params]
        if missing:
            return ToolResult(success=False, error=f"Missing required fields: {missing}")

        trade_id = params["trade_id"]
        if trade_id not in self._entries:
            return ToolResult(success=False, error=f"No journal entry for trade_id={trade_id}")

        reflection: dict[str, Any] = {
            "outcome": params["outcome"],
            "pnl": float(params["pnl"]),
            "lesson": params["lesson"],
            "what_was_right": params["what_was_right"],
            "what_was_wrong": params["what_was_wrong"],
            "reflected_at": datetime.now(timezone.utc).isoformat(),
        }
        self._entries[trade_id]["reflection"] = reflection
        return ToolResult(success=True, data={"trade_id": trade_id, "reflection": reflection})

    def _query(self, params: dict[str, Any]) -> ToolResult:
        expert_name = params.get("expert_name")
        outcome = params.get("outcome")
        limit = params.get("limit", 50)

        results = list(self._entries.values())
        if expert_name is not None:
            results = [e for e in results if e["expert_name"] == expert_name]
        if outcome is not None:
            results = [
                e for e in results
                if e.get("reflection") and e["reflection"]["outcome"] == outcome
            ]
        results = results[:limit]
        return ToolResult(success=True, data={"entries": results, "count": len(results)})

    # -- analytics ----------------------------------------------------------

    def get_lessons(self, expert_name: str | None = None) -> list[str]:
        """Extract all lessons learned from reflections."""
        entries = list(self._entries.values())
        if expert_name is not None:
            entries = [e for e in entries if e["expert_name"] == expert_name]
        return [
            e["reflection"]["lesson"]
            for e in entries
            if e.get("reflection") and e["reflection"].get("lesson")
        ]

    def get_decision_quality(self, expert_name: str | None = None) -> dict:
        """Compute decision quality metrics."""
        entries = list(self._entries.values())
        if expert_name is not None:
            entries = [e for e in entries if e["expert_name"] == expert_name]

        total_decisions = len(entries)
        reflected = [e for e in entries if e.get("reflection")]
        reflected_pct = len(reflected) / total_decisions if total_decisions > 0 else 0.0

        wins = [e for e in reflected if e["reflection"]["outcome"] == "win"]
        losses = [e for e in reflected if e["reflection"]["outcome"] == "loss"]

        avg_conf_wins = (
            sum(e["confidence"] for e in wins) / len(wins) if wins else 0.0
        )
        avg_conf_losses = (
            sum(e["confidence"] for e in losses) / len(losses) if losses else 0.0
        )

        # Top lessons by frequency
        lesson_counts: Counter[str] = Counter()
        for e in reflected:
            lesson = e["reflection"].get("lesson")
            if lesson:
                lesson_counts[lesson] += 1
        top_lessons = [lesson for lesson, _ in lesson_counts.most_common(5)]

        return {
            "total_decisions": total_decisions,
            "reflected_pct": reflected_pct,
            "avg_confidence_on_wins": avg_conf_wins,
            "avg_confidence_on_losses": avg_conf_losses,
            "top_lessons": top_lessons,
        }

    def get_stats(self) -> dict:
        reflected = sum(1 for e in self._entries.values() if e.get("reflection"))
        return {
            "total_entries": len(self._entries),
            "reflected": reflected,
            "unreflected": len(self._entries) - reflected,
        }


# ---------------------------------------------------------------------------
# LearningAggregator
# ---------------------------------------------------------------------------

class LearningAggregator:
    """Combines HypothesisSkill + JournalSkill to produce actionable insights."""

    def __init__(self, hypothesis_skill: HypothesisSkill, journal_skill: JournalSkill) -> None:
        self.hypothesis_skill = hypothesis_skill
        self.journal_skill = journal_skill

    def generate_insights(self, expert_name: str | None = None) -> dict:
        # Confirmed hypotheses
        confirmed = self.hypothesis_skill.get_hypotheses(expert_name=expert_name, status="confirmed")

        # Journal analytics
        entries = list(self.journal_skill._entries.values())
        if expert_name is not None:
            entries = [e for e in entries if e["expert_name"] == expert_name]

        reflected = [e for e in entries if e.get("reflection")]

        # Common mistakes
        wrong_counts: Counter[str] = Counter()
        right_counts: Counter[str] = Counter()
        for e in reflected:
            wrong = e["reflection"].get("what_was_wrong", "")
            right = e["reflection"].get("what_was_right", "")
            if wrong:
                wrong_counts[wrong] += 1
            if right:
                right_counts[right] += 1

        common_mistakes = [item for item, _ in wrong_counts.most_common(5)]
        strengths = [item for item, _ in right_counts.most_common(5)]

        # Confidence calibration
        wins = [e for e in reflected if e["reflection"]["outcome"] == "win"]
        losses = [e for e in reflected if e["reflection"]["outcome"] == "loss"]
        avg_conf_wins = sum(e["confidence"] for e in wins) / len(wins) if wins else 0.0
        avg_conf_losses = sum(e["confidence"] for e in losses) / len(losses) if losses else 0.0
        calibration = {
            "avg_confidence_on_wins": avg_conf_wins,
            "avg_confidence_on_losses": avg_conf_losses,
            "spread": avg_conf_wins - avg_conf_losses,
            "total_reflected": len(reflected),
        }

        # Recommendations
        recommendations: list[str] = []
        if confirmed:
            recommendations.append(
                f"Leverage {len(confirmed)} confirmed hypothesis(es) in strategy selection."
            )
        if calibration["spread"] < 0.05 and len(reflected) > 0:
            recommendations.append(
                "Confidence calibration is poor: high-confidence trades do not outperform "
                "low-confidence ones. Consider re-evaluating signal quality criteria."
            )
        if common_mistakes:
            recommendations.append(
                f"Top recurring mistake: '{common_mistakes[0]}'. Create a pre-trade checklist item."
            )
        if not reflected and entries:
            recommendations.append(
                "No trade reflections found. Start reflecting on completed trades to improve."
            )

        return {
            "confirmed_hypotheses": confirmed,
            "common_mistakes": common_mistakes,
            "strengths": strengths,
            "confidence_calibration": calibration,
            "recommendations": recommendations,
        }
