"""Predictive Transition Model — Markov chain for regime and event prediction.

Builds transition probability matrices from historical events, then predicts:
- Next likely regime with probability
- Expected time to regime transition
- Most probable event sequence

Inspired by VentureOS fund's eventBacktest.js transition matrix engine.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bot.utils.logger import get_logger

if TYPE_CHECKING:
    from bot.core.event_bus import DomainEvent, EventBus

logger = get_logger(__name__)


@dataclass
class Prediction:
    """A single prediction with confidence."""

    predicted_state: str
    probability: float
    alternatives: list[dict[str, float]] = field(
        default_factory=list
    )  # [{state, probability}, ...]
    horizon_seconds: float | None = None  # estimated time until transition
    basis: str = ""  # what data the prediction is based on
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "predicted_state": self.predicted_state,
            "probability": round(self.probability, 4),
            "alternatives": [
                {"state": a["state"], "probability": round(a["probability"], 4)}
                for a in self.alternatives
            ],
            "horizon_seconds": self.horizon_seconds,
            "basis": self.basis,
            "ts": self.ts,
        }


class MarkovTransitionModel:
    """Markov chain transition model for event and regime prediction.

    Maintains:
    - First-order transition matrix: P(next_state | current_state)
    - Second-order: P(next_state | prev_state, current_state) for higher accuracy
    - Transition time distribution: avg time between state changes

    Can predict:
    - predict_next(current_state) -> Prediction
    - predict_sequence(current_state, steps=3) -> list[Prediction]
    - get_stationary_distribution() -> {state: probability}
    """

    def __init__(self, smoothing: float = 0.01):
        """
        Args:
            smoothing: Laplace smoothing parameter to avoid zero probabilities.
        """
        self._smoothing = smoothing

        # First-order: counts[from_state][to_state] = count
        self._first_order: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        # Second-order: counts[(prev, current)][next] = count
        self._second_order: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )

        # Transition times: time_deltas[from_state][to_state] = [delta1, delta2, ...]
        self._transition_times: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # State set
        self._states: set[str] = set()

        # Last state per entity for sequence tracking
        self._entity_states: dict[str, list[tuple[str, float]]] = defaultdict(
            list
        )  # entity_key -> [(state, ts), ...]

        # Total observations
        self._total_transitions: int = 0

        # Prediction history
        self._predictions: list[Prediction] = []
        self._max_predictions: int = 200

    def observe(self, entity_key: str, state: str, ts: float | None = None) -> None:
        """Record a state observation for an entity.

        Args:
            entity_key: Unique entity identifier (e.g., "regime:ETH/USDT")
            state: The observed state (e.g., "bull_trend", "POSITION_OPENED")
            ts: Timestamp of observation
        """
        ts = ts or time.time()
        self._states.add(state)

        history = self._entity_states[entity_key]

        if history:
            prev_state, prev_ts = history[-1]

            if prev_state != state:  # only count actual transitions
                # First-order
                self._first_order[prev_state][state] += 1
                self._total_transitions += 1

                # Transition time
                delta = ts - prev_ts
                if delta > 0:
                    self._transition_times[prev_state][state].append(delta)

                # Second-order (need at least 2 prior states)
                if len(history) >= 2:
                    prev_prev_state = history[-2][0]
                    self._second_order[(prev_prev_state, prev_state)][state] += 1

        history.append((state, ts))

        # Keep history bounded
        if len(history) > 100:
            self._entity_states[entity_key] = history[-50:]

    def observe_from_events(
        self, events: list[DomainEvent], state_extractor: str = "event_type"
    ) -> None:
        """Bulk-observe from a list of DomainEvents.

        Args:
            events: List of events sorted by time
            state_extractor: Which field to use as state. Options:
                - "event_type": use event_type directly
                - "regime": use data["regime"] field
                - "status": use data["status"] field
        """
        sorted_events = sorted(events, key=lambda e: e.ts)

        for evt in sorted_events:
            entity_key = f"{evt.entity_type}:{evt.entity_id}"

            if state_extractor == "event_type":
                state = evt.event_type
            elif state_extractor == "regime":
                state = evt.data.get("regime", evt.event_type)
            elif state_extractor == "status":
                state = evt.data.get("status", evt.event_type)
            else:
                state = evt.event_type

            self.observe(entity_key, state, evt.ts)

    def predict_next(self, current_state: str, prev_state: str | None = None) -> Prediction:
        """Predict the most likely next state.

        Uses second-order model if prev_state is provided, falls back to first-order.
        """
        # Try second-order first
        if prev_state and (prev_state, current_state) in self._second_order:
            counts = self._second_order[(prev_state, current_state)]
            total = sum(counts.values()) + self._smoothing * len(self._states)
            probs = {s: (counts.get(s, 0) + self._smoothing) / total for s in self._states}
            basis = f"second-order: ({prev_state}, {current_state})"
        elif current_state in self._first_order:
            counts = self._first_order[current_state]
            total = sum(counts.values()) + self._smoothing * len(self._states)
            probs = {s: (counts.get(s, 0) + self._smoothing) / total for s in self._states}
            basis = f"first-order: {current_state}"
        else:
            # No data — uniform distribution
            n = len(self._states) if self._states else 1
            probs = dict.fromkeys(self._states, 1.0 / n)
            basis = "uniform (no data)"

        # Sort by probability
        sorted_probs = sorted(probs.items(), key=lambda x: -x[1])

        if not sorted_probs:
            return Prediction(predicted_state="unknown", probability=0.0, basis=basis)

        best_state, best_prob = sorted_probs[0]
        alternatives = [{"state": s, "probability": p} for s, p in sorted_probs[1:5]]

        # Estimate time to transition
        horizon = self._estimate_transition_time(current_state, best_state)

        prediction = Prediction(
            predicted_state=best_state,
            probability=best_prob,
            alternatives=alternatives,  # type: ignore[arg-type]
            horizon_seconds=horizon,
            basis=basis,
        )

        self._predictions.insert(0, prediction)
        if len(self._predictions) > self._max_predictions:
            self._predictions = self._predictions[: self._max_predictions]

        return prediction

    def predict_sequence(self, current_state: str, steps: int = 3) -> list[Prediction]:
        """Predict a sequence of future states."""
        predictions = []
        state = current_state
        prev_state = None

        for _ in range(steps):
            pred = self.predict_next(state, prev_state)
            predictions.append(pred)
            prev_state = state
            state = pred.predicted_state

        return predictions

    def get_transition_matrix(self) -> dict[str, dict[str, float]]:
        """Get the full first-order transition probability matrix."""
        matrix = {}
        for from_state in self._states:
            counts = self._first_order.get(from_state, {})
            total = sum(counts.values()) + self._smoothing * len(self._states)
            matrix[from_state] = {
                to_state: (counts.get(to_state, 0) + self._smoothing) / total
                for to_state in self._states
            }
        return matrix

    def get_stationary_distribution(self, iterations: int = 100) -> dict[str, float]:
        """Compute the stationary distribution via power iteration.

        The stationary distribution represents the long-run proportion of time
        the system spends in each state.
        """
        states = sorted(self._states)
        n = len(states)
        if n == 0:
            return {}

        matrix = self.get_transition_matrix()

        # Initialize uniform distribution
        dist = [1.0 / n] * n

        # Power iteration
        for _ in range(iterations):
            new_dist = [0.0] * n
            for i, s_from in enumerate(states):
                for j, s_to in enumerate(states):
                    prob = matrix.get(s_from, {}).get(s_to, 0)
                    new_dist[j] += dist[i] * prob

            # Normalize
            total = sum(new_dist)
            if total > 0:
                dist = [d / total for d in new_dist]
            else:
                break

        return {states[i]: round(dist[i], 6) for i in range(n)}

    def get_entropy(self, state: str) -> float:
        """Calculate Shannon entropy of transitions from a state.

        Higher entropy = more unpredictable transitions.
        Lower entropy = more deterministic transitions.
        """
        matrix = self.get_transition_matrix()
        probs = matrix.get(state, {})

        if not probs:
            return 0.0

        entropy = 0.0
        for p in probs.values():
            if p > 0:
                entropy -= p * math.log2(p)

        return round(entropy, 4)

    def _estimate_transition_time(self, from_state: str, to_state: str) -> float | None:
        """Estimate average transition time between states."""
        times = self._transition_times.get(from_state, {}).get(to_state, [])
        if not times:
            return None
        return sum(times) / len(times)

    def get_stats(self) -> dict[str, Any]:
        """Get model statistics."""
        return {
            "total_states": len(self._states),
            "total_transitions": self._total_transitions,
            "states": sorted(self._states),
            "entities_tracked": len(self._entity_states),
            "predictions_made": len(self._predictions),
            "last_prediction": self._predictions[0].to_dict() if self._predictions else None,
        }

    async def subscribe_to_bus(
        self, event_bus: EventBus, entity_types: list[str] | None = None
    ) -> None:
        """Auto-observe events from EventBus.

        Args:
            event_bus: The EventBus to subscribe to
            entity_types: If provided, only observe events for these entity types.
                          If None, observe all events.
        """

        def on_event(event: DomainEvent) -> None:
            if entity_types and event.entity_type not in entity_types:
                return
            entity_key = f"{event.entity_type}:{event.entity_id}"

            # For regime events, use the regime value as state
            if event.entity_type == "regime" and "regime" in event.data:
                state = event.data["regime"]
            else:
                state = event.event_type

            self.observe(entity_key, state, event.ts)

        # Subscribe to all events using wildcard pattern
        # Since EventBus may not support wildcards, subscribe to common types
        common_types = [
            "REGIME_DETECTED",
            "TRANSITION_COMPLETE",
            "REGIME_CHANGE_CANDIDATE",
            "SIGNAL_GENERATED",
            "POSITION_OPENED",
            "POSITION_CLOSED",
            "STRATEGY_ACTIVATED",
            "STRATEGY_DEACTIVATED",
            "RISK_HALTED",
            "RISK_RESUMED",
            "DECISION_MADE",
        ]
        for event_type in common_types:
            event_bus.subscribe(event_type, on_event)

        logger.info("predictive_model_subscribed", event_types=len(common_types))
