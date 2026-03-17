from __future__ import annotations

import math
from dataclasses import dataclass, field

_DIRECTION_VALUES = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}
_ROLLING_WINDOW = 20


@dataclass
class PredictionSource:
    name: str
    prediction: str
    confidence: float
    weight: float
    accuracy_history: list[bool] = field(default_factory=list)

    def to_dict(self) -> dict:
        recent = self.accuracy_history[-_ROLLING_WINDOW:]
        rolling_accuracy = sum(recent) / len(recent) if recent else 0.0
        return {
            "name": self.name,
            "prediction": self.prediction,
            "confidence": self.confidence,
            "weight": self.weight,
            "rolling_accuracy": rolling_accuracy,
            "total_predictions": len(self.accuracy_history),
        }


@dataclass
class EnsemblePrediction:
    direction: str
    confidence: float
    agreement_ratio: float
    sources: list[dict]
    entropy: float

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "confidence": self.confidence,
            "agreement_ratio": self.agreement_ratio,
            "sources": self.sources,
            "entropy": self.entropy,
        }


class EnsemblePredictor:
    def __init__(
        self,
        learning_rate: float = 0.1,
        min_weight: float = 0.1,
        max_weight: float = 3.0,
    ) -> None:
        self.learning_rate = learning_rate
        self.min_weight = min_weight
        self.max_weight = max_weight
        self._sources: dict[str, PredictionSource] = {}
        self._latest_predictions: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------
    def register_source(self, name: str, initial_weight: float = 1.0) -> None:
        self._sources[name] = PredictionSource(
            name=name,
            prediction="neutral",
            confidence=0.0,
            weight=max(self.min_weight, min(initial_weight, self.max_weight)),
        )

    def submit_prediction(self, name: str, prediction: str, confidence: float) -> None:
        if name not in self._sources:
            raise KeyError(f"Source '{name}' is not registered")
        confidence = max(0.0, min(confidence, 1.0))
        src = self._sources[name]
        src.prediction = prediction
        src.confidence = confidence
        self._latest_predictions[name] = (prediction, confidence)

    # ------------------------------------------------------------------
    def predict(self) -> EnsemblePrediction:
        active = {n: self._sources[n] for n in self._latest_predictions if n in self._sources}
        if not active:
            return EnsemblePrediction(
                direction="neutral",
                confidence=0.0,
                agreement_ratio=0.0,
                sources=[],
                entropy=0.0,
            )

        weighted_score = 0.0
        total_weight = 0.0
        for name, src in active.items():
            pred, conf = self._latest_predictions[name]
            dv = _DIRECTION_VALUES.get(pred, 0.0)
            contribution = src.weight * conf * dv
            weighted_score += contribution
            total_weight += src.weight * conf if conf else 0.0

        normalised = weighted_score / total_weight if total_weight else 0.0

        if normalised > 0.1:
            direction = "bullish"
        elif normalised < -0.1:
            direction = "bearish"
        else:
            direction = "neutral"

        confidence = min(abs(normalised), 1.0)

        # agreement
        n_total = len(active)
        agreeing = sum(1 for n in active if self._latest_predictions[n][0] == direction)
        agreement_ratio = agreeing / n_total

        # entropy
        counts: dict[str, int] = {}
        for n in active:
            p = self._latest_predictions[n][0]
            counts[p] = counts.get(p, 0) + 1
        entropy = 0.0
        for c in counts.values():
            prob = c / n_total
            if prob > 0:
                entropy -= prob * math.log2(prob)

        sources_info = [src.to_dict() for src in active.values()]

        return EnsemblePrediction(
            direction=direction,
            confidence=confidence,
            agreement_ratio=agreement_ratio,
            sources=sources_info,
            entropy=entropy,
        )

    # ------------------------------------------------------------------
    def record_outcome(self, actual: str) -> None:
        for name, (pred, _conf) in list(self._latest_predictions.items()):
            src = self._sources.get(name)
            if src is None:
                continue
            correct = pred == actual
            src.accuracy_history.append(correct)
            if correct:
                src.weight *= 1 + self.learning_rate
            else:
                src.weight *= 1 - self.learning_rate
            src.weight = max(self.min_weight, min(src.weight, self.max_weight))

    # ------------------------------------------------------------------
    def get_source_rankings(self) -> list[dict]:
        ranked: list[dict] = []
        for src in self._sources.values():
            recent = src.accuracy_history[-_ROLLING_WINDOW:]
            accuracy = sum(recent) / len(recent) if recent else 0.0
            ranked.append(
                {
                    "name": src.name,
                    "rolling_accuracy": accuracy,
                    "weight": src.weight,
                    "total_predictions": len(src.accuracy_history),
                }
            )
        ranked.sort(key=lambda d: d["rolling_accuracy"], reverse=True)
        return ranked

    def get_stats(self) -> dict:
        return {
            "num_sources": len(self._sources),
            "num_active": len(self._latest_predictions),
            "learning_rate": self.learning_rate,
            "sources": {n: s.to_dict() for n, s in self._sources.items()},
        }


class RegimeAdaptiveParams:
    def __init__(self) -> None:
        self._overrides: dict[str, dict[str, float]] = {}

    def define_regime_overrides(self, regime: str, overrides: dict[str, float]) -> None:
        self._overrides[regime] = overrides

    def get_params(self, base_params: dict, current_regime: str) -> dict:
        result = dict(base_params)
        overrides = self._overrides.get(current_regime)
        if overrides is None:
            return result
        for key, value in overrides.items():
            if key in result:
                result[key] = result[key] * value
            else:
                result[key] = value
        return result

    @staticmethod
    def get_default_overrides() -> dict[str, dict[str, float]]:
        return {
            "bull_trend": {"tp_multiplier": 1.5, "sl_multiplier": 0.8, "position_size_mult": 1.2},
            "bear_trend": {"tp_multiplier": 0.8, "sl_multiplier": 1.2, "position_size_mult": 0.7},
            "sideways": {"tp_multiplier": 0.6, "sl_multiplier": 0.6, "position_size_mult": 0.8},
            "volatile": {"tp_multiplier": 2.0, "sl_multiplier": 1.5, "position_size_mult": 0.5},
            "accumulation": {"tp_multiplier": 1.0, "sl_multiplier": 1.0, "position_size_mult": 1.0},
        }

    def load_defaults(self) -> None:
        for regime, overrides in self.get_default_overrides().items():
            self._overrides[regime] = overrides

    def get_stats(self) -> dict:
        return {
            "num_regimes": len(self._overrides),
            "regimes": list(self._overrides.keys()),
            "overrides": dict(self._overrides),
        }
