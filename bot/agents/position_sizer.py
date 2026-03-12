"""Dynamic Position Sizer — adjusts position size based on confidence and Kelly criterion."""

import time
from dataclasses import dataclass, field

from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class SizingResult:
    base_risk_pct: float  # original risk % (e.g., 2.0)
    adjusted_risk_pct: float  # after confidence adjustment
    kelly_fraction: float  # Kelly optimal fraction
    confidence_multiplier: float  # from committee score
    pattern_multiplier: float  # from pattern confidence
    final_position_pct: float  # final position size as % of capital
    reasoning: str
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "base_risk_pct": self.base_risk_pct,
            "adjusted_risk_pct": self.adjusted_risk_pct,
            "kelly_fraction": self.kelly_fraction,
            "confidence_multiplier": self.confidence_multiplier,
            "pattern_multiplier": self.pattern_multiplier,
            "final_position_pct": self.final_position_pct,
            "reasoning": self.reasoning,
            "ts": self.ts,
        }


class DynamicPositionSizer:
    """Calculates optimal position size using:

    1. Committee score -> confidence multiplier (0.25 to 1.0)
    2. Pattern win_rate + confidence -> pattern multiplier (0.5 to 1.2)
    3. Kelly criterion -> optimal fraction capped at half-Kelly
    4. Anomaly penalty -> reduce size during anomalies

    Formula:
      confidence_mult = 0.25 + 0.75 * committee_score  (score 0->0.25, score 1->1.0)
      pattern_mult = 0.5 + 0.5 * pattern_win_rate * pattern_confidence  (no data->0.5, strong->1.0)

      kelly_fraction = win_rate - (1 - win_rate) / avg_win_loss_ratio
      half_kelly = max(0, kelly_fraction / 2)  # conservative half-Kelly

      adjusted_risk = base_risk * confidence_mult * pattern_mult
      final = min(adjusted_risk, half_kelly * 100, max_risk_pct)
    """

    def __init__(
        self,
        base_risk_pct: float = 2.0,
        max_risk_pct: float = 5.0,
        min_risk_pct: float = 0.25,
        use_kelly: bool = True,
        kelly_cap: float = 0.5,
        anomaly_reduction: float = 0.5,
    ) -> None:
        self._base_risk_pct = base_risk_pct
        self._max_risk_pct = max_risk_pct
        self._min_risk_pct = min_risk_pct
        self._use_kelly = use_kelly
        self._kelly_cap = kelly_cap
        self._anomaly_reduction = anomaly_reduction
        self._history: list[SizingResult] = []
        self._max_history: int = 500

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate(
        self,
        committee_score: float = 0.5,
        pattern_win_rate: float | None = None,
        pattern_confidence: float = 0.0,
        avg_win_loss_ratio: float = 1.5,
        has_anomaly: bool = False,
        balance: float = 10000,
    ) -> SizingResult:
        """Calculate optimal position size."""

        # 1. Confidence multiplier from committee score (0.25 .. 1.0)
        committee_score = max(0.0, min(1.0, committee_score))
        confidence_mult = 0.25 + 0.75 * committee_score

        # 2. Pattern multiplier (0.5 .. 1.2)
        if pattern_win_rate is None:
            pattern_mult = 0.5
        else:
            pattern_win_rate = max(0.0, min(1.0, pattern_win_rate))
            pattern_confidence = max(0.0, min(1.0, pattern_confidence))
            # 0.5 base + up to 0.7 bonus (max 1.2 with perfect win_rate and confidence)
            pattern_mult = 0.5 + 0.7 * pattern_win_rate * pattern_confidence
            pattern_mult = min(pattern_mult, 1.2)

        # 3. Kelly criterion
        effective_wr = pattern_win_rate if pattern_win_rate is not None else 0.5
        kelly_frac = self.kelly_criterion(effective_wr, avg_win_loss_ratio)
        half_kelly = max(0.0, kelly_frac * self._kelly_cap)

        # 4. Adjusted risk
        adjusted_risk = self._base_risk_pct * confidence_mult * pattern_mult

        # 5. Apply Kelly cap if enabled
        reasoning_parts: list[str] = []
        if self._use_kelly and half_kelly > 0:
            kelly_risk = half_kelly * 100  # fraction -> percentage
            if adjusted_risk > kelly_risk:
                reasoning_parts.append(
                    f"Kelly cap applied: {adjusted_risk:.2f}% -> {kelly_risk:.2f}%"
                )
                adjusted_risk = kelly_risk
        elif self._use_kelly and half_kelly <= 0:
            reasoning_parts.append(
                "Kelly suggests no edge; applying minimum risk"
            )
            adjusted_risk = self._min_risk_pct

        # 6. Anomaly penalty
        if has_anomaly:
            pre_anomaly = adjusted_risk
            adjusted_risk *= self._anomaly_reduction
            reasoning_parts.append(
                f"Anomaly penalty: {pre_anomaly:.2f}% -> {adjusted_risk:.2f}%"
            )

        # 7. Clamp to [min, max]
        final = max(self._min_risk_pct, min(adjusted_risk, self._max_risk_pct))

        if not reasoning_parts:
            reasoning_parts.append(
                f"conf_mult={confidence_mult:.2f}, pat_mult={pattern_mult:.2f}"
            )

        result = SizingResult(
            base_risk_pct=self._base_risk_pct,
            adjusted_risk_pct=round(adjusted_risk, 4),
            kelly_fraction=round(kelly_frac, 4),
            confidence_multiplier=round(confidence_mult, 4),
            pattern_multiplier=round(pattern_mult, 4),
            final_position_pct=round(final, 4),
            reasoning="; ".join(reasoning_parts),
        )

        self._history.append(result)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

        logger.debug(
            "position_size calculated: final=%.2f%% kelly=%.4f conf=%.2f pat=%.2f",
            final,
            kelly_frac,
            confidence_mult,
            pattern_mult,
        )

        return result

    def kelly_criterion(self, win_rate: float, win_loss_ratio: float) -> float:
        """Calculate Kelly fraction. Returns 0 if negative (no edge).

        f* = p - (1-p)/b  where p=win_rate, b=win_loss_ratio
        """
        if win_loss_ratio <= 0:
            return 0.0
        f = win_rate - (1 - win_rate) / win_loss_ratio
        return max(0.0, f)

    def get_history(self, limit: int = 20) -> list[dict]:
        """Return recent sizing history as list of dicts."""
        return [r.to_dict() for r in self._history[-limit:]]

    def get_stats(self) -> dict:
        """Return summary statistics over recorded history."""
        if not self._history:
            return {
                "avg_adjusted_risk": 0.0,
                "avg_kelly": 0.0,
                "history_count": 0,
            }
        avg_risk = sum(r.adjusted_risk_pct for r in self._history) / len(
            self._history
        )
        avg_kelly = sum(r.kelly_fraction for r in self._history) / len(
            self._history
        )
        return {
            "avg_adjusted_risk": round(avg_risk, 4),
            "avg_kelly": round(avg_kelly, 4),
            "history_count": len(self._history),
        }
