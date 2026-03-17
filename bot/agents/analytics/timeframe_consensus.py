from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class TimeframeVerdict:
    """Single timeframe analysis verdict."""

    timeframe: str
    direction: str  # "bullish", "bearish", "neutral"
    strength: float  # 0.0 - 1.0
    signals: dict
    confidence: float  # 0.0 - 1.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "timeframe": self.timeframe,
            "direction": self.direction,
            "strength": self.strength,
            "signals": self.signals,
            "confidence": self.confidence,
            "ts": self.ts,
        }


_DIRECTION_VALUE = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}

_DEFAULT_WEIGHTS: dict[str, float] = {
    "M5": 0.15,
    "H1": 0.25,
    "H4": 0.30,
    "D1": 0.30,
}


class TimeframeAnalyzer:
    """Aggregates verdicts across multiple timeframes and computes alignment."""

    def __init__(
        self,
        timeframes: list[str] | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        self._timeframes = timeframes or ["M5", "H1", "H4", "D1"]
        self._weights = weights or {tf: _DEFAULT_WEIGHTS.get(tf, 0.25) for tf in self._timeframes}
        self._verdicts: dict[str, TimeframeVerdict] = {}

    # ------------------------------------------------------------------
    def submit_verdict(self, verdict: TimeframeVerdict) -> None:
        self._verdicts[verdict.timeframe] = verdict

    # ------------------------------------------------------------------
    def get_alignment_score(self) -> dict:
        if not self._verdicts:
            return {
                "score": 0.0,
                "alignment": "neutral",
                "aligned_timeframes": [],
                "conflicting_timeframes": [],
                "details": {},
            }

        # Weighted score
        total_weight = 0.0
        weighted_sum = 0.0
        details: dict[str, dict] = {}

        for tf, v in self._verdicts.items():
            w = self._weights.get(tf, 0.25)
            dir_val = _DIRECTION_VALUE.get(v.direction, 0.0)
            contribution = dir_val * v.strength * v.confidence * w
            weighted_sum += contribution
            total_weight += w
            details[tf] = {
                "direction": v.direction,
                "strength": v.strength,
                "confidence": v.confidence,
                "contribution": contribution,
            }

        score = weighted_sum / total_weight if total_weight > 0 else 0.0
        score = max(-1.0, min(1.0, score))

        # Alignment label
        if score >= 0.6:
            alignment = "strong_bullish"
        elif score >= 0.2:
            alignment = "bullish"
        elif score <= -0.6:
            alignment = "strong_bearish"
        elif score <= -0.2:
            alignment = "bearish"
        else:
            alignment = "neutral"

        dominant = self.get_dominant_direction()
        aligned = [tf for tf, v in self._verdicts.items() if v.direction == dominant]
        conflicting = [tf for tf, v in self._verdicts.items() if v.direction != dominant]

        return {
            "score": score,
            "alignment": alignment,
            "aligned_timeframes": aligned,
            "conflicting_timeframes": conflicting,
            "details": details,
        }

    # ------------------------------------------------------------------
    def get_dominant_direction(self) -> str:
        if not self._verdicts:
            return "neutral"
        counts = Counter(v.direction for v in self._verdicts.values())
        return counts.most_common(1)[0][0]

    def is_fully_aligned(self) -> bool:
        if not self._verdicts:
            return False
        directions = {v.direction for v in self._verdicts.values()}
        return len(directions) == 1

    def get_stats(self) -> dict:
        return {
            "timeframes": self._timeframes,
            "weights": self._weights,
            "verdicts_count": len(self._verdicts),
            "submitted_timeframes": list(self._verdicts.keys()),
        }


class RegimeTransitionDetector:
    """Detects regime instability and predicts transitions using entropy."""

    def __init__(self, entropy_threshold: float = 0.3, lookback: int = 20) -> None:
        self._entropy_threshold = entropy_threshold
        self._lookback = lookback
        self._observations: list[tuple[str, float]] = []

    # ------------------------------------------------------------------
    def observe(self, regime: str, ts: float | None = None) -> None:
        self._observations.append((regime, ts if ts is not None else time.time()))
        # Trim to lookback
        if len(self._observations) > self._lookback:
            self._observations = self._observations[-self._lookback :]

    # ------------------------------------------------------------------
    def get_transition_probability(self) -> dict:
        if not self._observations:
            return {
                "current_regime": "unknown",
                "stability": 0.0,
                "entropy": 0.0,
                "likely_next": "unknown",
                "transition_probability": 0.0,
            }

        current = self._observations[-1][0]
        stability = self._calculate_stability()

        # Build transition counts from current regime
        transition_counts: dict[str, int] = {}
        total_transitions = 0
        for i in range(len(self._observations) - 1):
            src = self._observations[i][0]
            dst = self._observations[i + 1][0]
            if src == current:
                transition_counts[dst] = transition_counts.get(dst, 0) + 1
                total_transitions += 1

        # Regime distribution for entropy
        regimes = [obs[0] for obs in self._observations]
        counts = Counter(regimes)
        total = len(regimes)
        distribution = {r: c / total for r, c in counts.items()}
        entropy = self._calculate_entropy(distribution)

        if total_transitions > 0:
            likely_next = max(transition_counts, key=transition_counts.get)  # type: ignore[arg-type]
            transition_prob = transition_counts[likely_next] / total_transitions
        else:
            likely_next = current
            transition_prob = 0.0

        return {
            "current_regime": current,
            "stability": stability,
            "entropy": entropy,
            "likely_next": likely_next,
            "transition_probability": transition_prob,
        }

    # ------------------------------------------------------------------
    def detect_early_warning(self) -> dict:
        if not self._observations:
            return {
                "warning": False,
                "level": "none",
                "reasons": [],
                "regime_duration": 0,
                "avg_duration": 0.0,
            }

        reasons: list[str] = []
        current = self._observations[-1][0]

        # Current regime duration (consecutive from end)
        regime_duration = 0
        for i in range(len(self._observations) - 1, -1, -1):
            if self._observations[i][0] == current:
                regime_duration += 1
            else:
                break

        # Average regime duration across all runs
        runs: list[int] = []
        run_len = 1
        for i in range(1, len(self._observations)):
            if self._observations[i][0] == self._observations[i - 1][0]:
                run_len += 1
            else:
                runs.append(run_len)
                run_len = 1
        runs.append(run_len)
        avg_duration = sum(runs) / len(runs) if runs else 0.0

        # Entropy check
        regimes = [obs[0] for obs in self._observations]
        counts = Counter(regimes)
        total = len(regimes)
        distribution = {r: c / total for r, c in counts.items()}
        entropy = self._calculate_entropy(distribution)

        if entropy > self._entropy_threshold:
            reasons.append(f"high entropy ({entropy:.3f} > {self._entropy_threshold})")

        # Short current run
        if avg_duration > 0 and regime_duration < avg_duration * 0.5:
            reasons.append(
                f"current regime duration ({regime_duration}) shorter than average ({avg_duration:.1f})"
            )

        # Rising entropy: compare first-half vs second-half entropy
        if len(self._observations) >= 6:
            mid = len(self._observations) // 2
            first_half = [obs[0] for obs in self._observations[:mid]]
            second_half = [obs[0] for obs in self._observations[mid:]]
            c1 = Counter(first_half)
            c2 = Counter(second_half)
            d1 = {r: c / len(first_half) for r, c in c1.items()}
            d2 = {r: c / len(second_half) for r, c in c2.items()}
            e1 = self._calculate_entropy(d1)
            e2 = self._calculate_entropy(d2)
            if e2 > e1 + 0.1:
                reasons.append(f"rising entropy ({e1:.3f} -> {e2:.3f})")

        # Determine level
        if len(reasons) >= 3:
            level = "high"
        elif len(reasons) == 2:
            level = "medium"
        elif len(reasons) == 1:
            level = "low"
        else:
            level = "none"

        return {
            "warning": len(reasons) > 0,
            "level": level,
            "reasons": reasons,
            "regime_duration": regime_duration,
            "avg_duration": avg_duration,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _calculate_entropy(distribution: dict[str, float]) -> float:
        entropy = 0.0
        for p in distribution.values():
            if p > 0:
                entropy -= p * math.log2(p)
        return entropy

    def _calculate_stability(self) -> float:
        if not self._observations:
            return 0.0
        current = self._observations[-1][0]
        duration = 0
        for i in range(len(self._observations) - 1, -1, -1):
            if self._observations[i][0] == current:
                duration += 1
            else:
                break
        return duration / len(self._observations)

    def get_stats(self) -> dict:
        regimes = [obs[0] for obs in self._observations]
        counts = Counter(regimes)
        return {
            "observations": len(self._observations),
            "lookback": self._lookback,
            "entropy_threshold": self._entropy_threshold,
            "regime_counts": dict(counts),
            "current_regime": self._observations[-1][0] if self._observations else "unknown",
        }
