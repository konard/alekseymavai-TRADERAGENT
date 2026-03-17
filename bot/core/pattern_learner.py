"""Pattern Learner — discovers and tracks recurring patterns in trade outcomes.

Observes sequences of events leading to profitable/unprofitable trades,
builds a pattern library, and scores new signals against known patterns.

Inspired by VentureOS fund's EXPERT_PATTERN_LEARNED / EXPERT_PATTERN_FIRED events.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bot.utils.logger import get_logger

if TYPE_CHECKING:
    from bot.core.event_bus import DomainEvent, EventBus

logger = get_logger(__name__)


@dataclass
class Pattern:
    """A discovered trading pattern."""

    pattern_id: str
    name: str
    description: str

    # Pattern fingerprint: conditions that define this pattern
    conditions: dict[
        str, Any
    ]  # e.g. {"regime": "bull_trend", "adx_above": 25, "near_demand_zone": True}

    # Outcome statistics
    occurrences: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    win_rate: float = 0.0

    # Confidence metrics
    confidence: float = 0.0  # based on sample size + win rate
    last_seen: float = 0.0
    first_seen: float = 0.0

    # Tags
    tags: list[str] = field(default_factory=list)

    def update_stats(self, pnl: float, ts: float | None = None) -> None:
        """Update pattern statistics with a new outcome."""
        ts = ts or time.time()
        self.occurrences += 1
        self.total_pnl += pnl
        if pnl > 0:
            self.wins += 1
        elif pnl < 0:
            self.losses += 1

        self.win_rate = self.wins / self.occurrences if self.occurrences > 0 else 0
        self.avg_pnl = self.total_pnl / self.occurrences if self.occurrences > 0 else 0
        self.last_seen = ts
        if self.first_seen == 0:
            self.first_seen = ts

        # Confidence = f(sample_size, win_rate consistency)
        # Uses Wilson score interval lower bound
        if self.occurrences >= 5:
            n = self.occurrences
            p = self.win_rate
            z = 1.96  # 95% confidence
            denominator = 1 + z**2 / n
            center = (p + z**2 / (2 * n)) / denominator
            margin = z * (p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5 / denominator
            self.confidence = max(0, center - margin)
        else:
            self.confidence = self.win_rate * (self.occurrences / 10)  # low-sample penalty

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "name": self.name,
            "description": self.description,
            "conditions": self.conditions,
            "occurrences": self.occurrences,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "avg_pnl": round(self.avg_pnl, 4),
            "total_pnl": round(self.total_pnl, 4),
            "confidence": round(self.confidence, 4),
            "last_seen": self.last_seen,
            "tags": self.tags,
        }


@dataclass
class PatternMatch:
    """Result of matching a signal against the pattern library."""

    pattern: Pattern
    match_score: float  # 0.0-1.0 how well the signal matches pattern conditions
    expected_win_rate: float
    expected_pnl: float
    recommendation: str  # "strong_buy", "buy", "neutral", "avoid", "strong_avoid"


class PatternLearner:
    """Discovers and manages trading patterns from event history.

    The learner:
    1. Observes POSITION_CLOSED events with context (regime, strategy, etc.)
    2. Extracts a fingerprint from the context
    3. Updates or creates patterns based on the fingerprint
    4. Scores new signals against known patterns

    Usage:
        learner = PatternLearner(event_bus)
        await learner.start()  # subscribes to events

        # Later, when evaluating a new signal:
        matches = learner.match_signal(signal_context)
    """

    def __init__(self, event_bus: EventBus | None = None, min_occurrences: int = 5):
        self._bus = event_bus
        self._patterns: dict[str, Pattern] = {}
        self._min_occurrences = min_occurrences
        self._unsubscribers: list = []

        # Context buffer: stores context when position opens, used when it closes
        self._open_contexts: dict[str, dict[str, Any]] = {}  # position_id -> context

        # Stats
        self._total_observations: int = 0
        self._patterns_fired: int = 0

    @property
    def patterns(self) -> list[Pattern]:
        return sorted(self._patterns.values(), key=lambda p: -p.confidence)

    def _make_fingerprint(self, context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Create a unique fingerprint from trading context."""
        # Key features that define a pattern
        features: dict[str, Any] = {
            "regime": context.get("regime", "unknown"),
            "strategy": context.get("strategy", "unknown"),
            "direction": context.get("direction", "unknown"),
            "adx_range": self._bucketize(context.get("adx"), [15, 25, 40, 60]),
            "drawdown_range": self._bucketize(context.get("drawdown"), [0.05, 0.10, 0.20]),
            "hour_bucket": self._hour_bucket(context.get("hour")),
        }

        # Add optional features
        if context.get("near_demand_zone"):
            features["near_demand"] = True
        if context.get("near_supply_zone"):
            features["near_supply"] = True
        if context.get("bos_detected"):
            features["bos"] = True
        if context.get("choch_detected"):
            features["choch"] = True
        if context.get("volume_trend"):
            features["volume"] = context["volume_trend"]

        # Hash the sorted features
        feature_str = str(sorted(features.items()))
        fingerprint = hashlib.md5(feature_str.encode()).hexdigest()[:16]

        return fingerprint, features

    @staticmethod
    def _bucketize(value: float | None, thresholds: list[float]) -> str:
        """Convert a continuous value into a categorical bucket."""
        if value is None:
            return "unknown"
        for t in thresholds:
            if float(value) < t:
                return f"<{t}"
        return f">={thresholds[-1]}"

    @staticmethod
    def _hour_bucket(hour: int | None) -> str:
        """Convert hour to session bucket."""
        if hour is None:
            return "unknown"
        if 0 <= hour < 8:
            return "asia"
        elif 8 <= hour < 16:
            return "europe"
        else:
            return "us"

    def observe_open(self, position_id: str, context: dict[str, Any]) -> None:
        """Record context when a position opens."""
        self._open_contexts[position_id] = {
            **context,
            "open_ts": time.time(),
        }

    def observe_close(
        self, position_id: str, pnl: float, close_context: dict[str, Any] | None = None
    ) -> None:
        """Record outcome when a position closes."""
        open_ctx = self._open_contexts.pop(position_id, {})
        if not open_ctx:
            return

        # Merge open and close context
        ctx = {**open_ctx}
        if close_context:
            ctx.update(close_context)

        fingerprint, features = self._make_fingerprint(ctx)
        self._total_observations += 1

        if fingerprint in self._patterns:
            pattern = self._patterns[fingerprint]
            pattern.update_stats(pnl)
        else:
            # Create new pattern
            direction = ctx.get("direction", "")
            regime = ctx.get("regime", "")
            strategy = ctx.get("strategy", "")

            pattern = Pattern(
                pattern_id=fingerprint,
                name=f"{strategy}_{direction}_{regime}",
                description=f"{strategy} {direction} in {regime}",
                conditions=features,
            )
            pattern.update_stats(pnl)
            self._patterns[fingerprint] = pattern

        logger.debug(
            "pattern_observed",
            pattern_id=fingerprint,
            pnl=round(pnl, 2),
            occurrences=pattern.occurrences,
            win_rate=f"{pattern.win_rate:.1%}",
        )

    def match_signal(self, context: dict[str, Any]) -> list[PatternMatch]:
        """Match a trading signal context against known patterns.

        Returns patterns sorted by match_score * confidence (best first).
        """
        fingerprint, features = self._make_fingerprint(context)
        matches = []

        # Exact match
        if fingerprint in self._patterns:
            pattern = self._patterns[fingerprint]
            if pattern.occurrences >= self._min_occurrences:
                matches.append(
                    PatternMatch(
                        pattern=pattern,
                        match_score=1.0,
                        expected_win_rate=pattern.win_rate,
                        expected_pnl=pattern.avg_pnl,
                        recommendation=self._get_recommendation(pattern),
                    )
                )
                self._patterns_fired += 1

        # Partial matches (at least 50% conditions matching)
        for pid, pattern in self._patterns.items():
            if pid == fingerprint:
                continue
            if pattern.occurrences < self._min_occurrences:
                continue

            matching_keys = 0
            total_keys = 0
            for key, value in features.items():
                if key in pattern.conditions:
                    total_keys += 1
                    if pattern.conditions[key] == value:
                        matching_keys += 1

            if total_keys > 0:
                match_score = matching_keys / total_keys
            else:
                match_score = 0

            if match_score >= 0.5:  # at least 50% conditions match
                matches.append(
                    PatternMatch(
                        pattern=pattern,
                        match_score=match_score,
                        expected_win_rate=pattern.win_rate,
                        expected_pnl=pattern.avg_pnl * match_score,  # discount by match quality
                        recommendation=self._get_recommendation(pattern, match_score),
                    )
                )

        # Sort by combined score
        matches.sort(key=lambda m: -(m.match_score * m.pattern.confidence))

        return matches[:10]  # top 10

    @staticmethod
    def _get_recommendation(pattern: Pattern, match_score: float = 1.0) -> str:
        """Generate recommendation based on pattern stats."""
        effective_wr = pattern.win_rate * match_score

        if effective_wr >= 0.7 and pattern.avg_pnl > 0:
            return "strong_buy"
        elif effective_wr >= 0.55 and pattern.avg_pnl > 0:
            return "buy"
        elif effective_wr <= 0.3 or pattern.avg_pnl < 0:
            return "strong_avoid"
        elif effective_wr <= 0.45:
            return "avoid"
        return "neutral"

    async def start(self) -> None:
        """Subscribe to EventBus for automatic learning."""
        if not self._bus:
            return

        def on_position_opened(event: DomainEvent) -> None:
            self.observe_open(
                position_id=event.entity_id,
                context=event.data,
            )

        def on_position_closed(event: DomainEvent) -> None:
            pnl = event.data.get("pnl", 0)
            if isinstance(pnl, str):
                try:
                    pnl = float(pnl)
                except ValueError:
                    pnl = 0
            self.observe_close(
                position_id=event.entity_id,
                pnl=pnl,
                close_context=event.data,
            )

        self._unsubscribers.append(self._bus.subscribe("POSITION_OPENED", on_position_opened))
        self._unsubscribers.append(self._bus.subscribe("POSITION_CLOSED", on_position_closed))

        logger.info("pattern_learner_started")

    async def stop(self) -> None:
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()

    def get_top_patterns(self, n: int = 20) -> list[dict]:
        """Get top N patterns by confidence."""
        return [p.to_dict() for p in self.patterns[:n]]

    def get_stats(self) -> dict[str, Any]:
        """Get learner statistics."""
        profitable = [
            p
            for p in self._patterns.values()
            if p.avg_pnl > 0 and p.occurrences >= self._min_occurrences
        ]
        unprofitable = [
            p
            for p in self._patterns.values()
            if p.avg_pnl <= 0 and p.occurrences >= self._min_occurrences
        ]

        return {
            "total_patterns": len(self._patterns),
            "qualified_patterns": len(profitable) + len(unprofitable),
            "profitable_patterns": len(profitable),
            "unprofitable_patterns": len(unprofitable),
            "total_observations": self._total_observations,
            "patterns_fired": self._patterns_fired,
            "open_positions_tracked": len(self._open_contexts),
            "top_patterns": self.get_top_patterns(5),
        }

    async def publish_pattern_fired(self, match: PatternMatch, session_id: str = "") -> None:
        """Publish EXPERT_PATTERN_FIRED event."""
        if self._bus:
            from bot.core.event_bus import DomainEvent

            evt = DomainEvent(
                entity_type="expert",
                entity_id=match.pattern.pattern_id,
                event_type="EXPERT_PATTERN_FIRED",
                data={
                    "pattern_name": match.pattern.name,
                    "match_score": round(match.match_score, 4),
                    "expected_win_rate": round(match.expected_win_rate, 4),
                    "expected_pnl": round(match.expected_pnl, 4),
                    "recommendation": match.recommendation,
                    "occurrences": match.pattern.occurrences,
                    "confidence": round(match.pattern.confidence, 4),
                    "session_id": session_id,
                },
                bot_name="pattern_learner",
            )
            await self._bus.publish(evt)
