"""Anomaly Detector — detects unusual market behavior using statistical methods."""

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from bot.core.event_bus import DomainEvent, EventBus, EventPriority
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class AnomalyType(str, Enum):
    VOLUME_SPIKE = "volume_spike"
    PRICE_SPIKE = "price_spike"
    SPREAD_ANOMALY = "spread_anomaly"
    VOLATILITY_EXPLOSION = "volatility_explosion"
    LIQUIDITY_GAP = "liquidity_gap"
    CORRELATION_BREAK = "correlation_break"


# Mapping from metric name to anomaly type
_METRIC_TO_ANOMALY: dict[str, AnomalyType] = {
    "volume": AnomalyType.VOLUME_SPIKE,
    "price_change_pct": AnomalyType.PRICE_SPIKE,
    "spread": AnomalyType.SPREAD_ANOMALY,
    "volatility": AnomalyType.VOLATILITY_EXPLOSION,
    "bid_ask_spread": AnomalyType.LIQUIDITY_GAP,
    "correlation": AnomalyType.CORRELATION_BREAK,
}


@dataclass
class Anomaly:
    anomaly_type: AnomalyType
    severity: float  # 0.0-1.0 (0.0=mild, 1.0=extreme)
    z_score: float  # how many std devs from mean
    description: str
    metric_name: str  # e.g., "volume", "price_change_pct"
    metric_value: float
    threshold: float  # the z_score threshold that triggered
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "anomaly_type": self.anomaly_type.value,
            "severity": self.severity,
            "z_score": self.z_score,
            "description": self.description,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "threshold": self.threshold,
            "ts": self.ts,
        }


class AnomalyDetector:
    """Detects market anomalies using rolling z-score analysis.

    Maintains rolling windows of market metrics and flags when values
    exceed configurable z-score thresholds.

    Metrics tracked:
    - volume: trading volume per bar
    - price_change_pct: |close - open| / open per bar
    - spread: high - low per bar
    - volatility: rolling std of returns

    When anomaly detected:
    - Publishes ANOMALY_DETECTED event to EventBus
    - If severity >= halt_threshold: publishes RISK_HALTED event
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        window_size: int = 100,
        z_threshold: float = 3.0,
        halt_threshold: float = 0.8,
        cooldown_seconds: float = 300,
    ) -> None:
        self._event_bus = event_bus
        self._window_size = window_size
        self._z_threshold = z_threshold
        self._halt_threshold = halt_threshold
        self._cooldown_seconds = cooldown_seconds

        self._metrics: dict[str, list[float]] = {}
        self._anomalies: list[Anomaly] = []
        self._max_anomalies: int = 500
        self._halted: bool = False
        self._last_halt_ts: float = 0

    def observe(self, metrics: dict[str, float]) -> list[Anomaly]:
        """Observe a new set of market metrics and check for anomalies.

        Args:
            metrics: dict with keys like:
                - "volume": current bar volume
                - "price_change_pct": abs price change as fraction
                - "spread": high - low
                - "volatility": current realized volatility
                - "bid_ask_spread": bid-ask spread (optional)

        Returns list of detected anomalies (empty if none).

        For each metric:
            1. Append to rolling window
            2. If window has >= 20 values, compute mean and std
            3. Calculate z_score = (value - mean) / std
            4. If |z_score| > z_threshold: create Anomaly
            5. severity = min(1.0, |z_score| / (z_threshold * 2))
        """
        detected: list[Anomaly] = []

        for metric_name, value in metrics.items():
            # Append to rolling window
            if metric_name not in self._metrics:
                self._metrics[metric_name] = []
            window = self._metrics[metric_name]
            window.append(value)

            # Trim to window_size
            if len(window) > self._window_size:
                self._metrics[metric_name] = window[-self._window_size :]
                window = self._metrics[metric_name]

            # Need at least 20 values
            if len(window) < 20:
                continue

            # Compute stats
            mean = sum(window) / len(window)
            variance = sum((x - mean) ** 2 for x in window) / len(window)
            std = math.sqrt(variance)

            if std == 0:
                continue

            z_score = (value - mean) / std

            if abs(z_score) > self._z_threshold:
                severity = min(1.0, abs(z_score) / (self._z_threshold * 2))
                anomaly_type = _METRIC_TO_ANOMALY.get(
                    metric_name, AnomalyType.PRICE_SPIKE
                )
                anomaly = Anomaly(
                    anomaly_type=anomaly_type,
                    severity=severity,
                    z_score=z_score,
                    description=(
                        f"{metric_name} z-score {z_score:.2f} exceeds "
                        f"threshold {self._z_threshold:.1f}"
                    ),
                    metric_name=metric_name,
                    metric_value=value,
                    threshold=self._z_threshold,
                )
                detected.append(anomaly)
                self._anomalies.append(anomaly)

                # Trim anomaly history
                if len(self._anomalies) > self._max_anomalies:
                    self._anomalies = self._anomalies[-self._max_anomalies :]

                logger.warning(
                    "anomaly_detected",
                    anomaly_type=anomaly_type.value,
                    metric=metric_name,
                    z_score=round(z_score, 2),
                    severity=round(severity, 2),
                )

        return detected

    def observe_candle(
        self,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> list[Anomaly]:
        """Convenience: extract metrics from OHLCV candle and observe.

        Extracts:
        - volume: volume
        - price_change_pct: abs(close - open) / open
        - spread: (high - low) / open
        - volatility: price_change_pct (simplified)
        """
        if open == 0:
            return []

        price_change_pct = abs(close - open) / open
        spread = (high - low) / open

        return self.observe(
            {
                "volume": volume,
                "price_change_pct": price_change_pct,
                "spread": spread,
                "volatility": price_change_pct,
            }
        )

    def check_halt(self, anomalies: list[Anomaly]) -> bool:
        """Check if any anomaly should trigger trading halt.

        Returns True if halt should be triggered.
        Also publishes RISK_HALTED event if EventBus available.
        """
        now = time.time()

        for anomaly in anomalies:
            if anomaly.severity >= self._halt_threshold:
                # Check cooldown
                if now - self._last_halt_ts < self._cooldown_seconds:
                    return False

                self._halted = True
                self._last_halt_ts = now

                logger.warning(
                    "trading_halt_triggered",
                    anomaly_type=anomaly.anomaly_type.value,
                    severity=round(anomaly.severity, 2),
                    z_score=round(anomaly.z_score, 2),
                )

                return True

        return False

    def reset_halt(self) -> None:
        """Clear halt state."""
        self._halted = False
        logger.info("trading_halt_cleared")

    def get_recent_anomalies(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent anomalies as dicts."""
        return [a.to_dict() for a in self._anomalies[-limit:]]

    def get_stats(self) -> dict[str, Any]:
        """Stats: total_anomalies, anomalies_by_type, is_halted, metrics_tracked, window_size."""
        by_type: dict[str, int] = {}
        for a in self._anomalies:
            key = a.anomaly_type.value
            by_type[key] = by_type.get(key, 0) + 1

        return {
            "total_anomalies": len(self._anomalies),
            "anomalies_by_type": by_type,
            "is_halted": self._halted,
            "metrics_tracked": list(self._metrics.keys()),
            "window_size": self._window_size,
        }

    async def publish_anomaly(self, anomaly: Anomaly) -> None:
        """Publish ANOMALY_DETECTED event to EventBus."""
        if self._event_bus is None:
            return

        event = DomainEvent(
            entity_type="market",
            entity_id="anomaly_detector",
            event_type="ANOMALY_DETECTED",
            data=anomaly.to_dict(),
            priority=EventPriority.HIGH,
        )
        await self._event_bus.publish(event)

        # If severity warrants halt, also publish RISK_HALTED
        if anomaly.severity >= self._halt_threshold:
            halt_event = DomainEvent(
                entity_type="risk",
                entity_id="anomaly_detector",
                event_type="RISK_HALTED",
                data={
                    "reason": f"Anomaly: {anomaly.anomaly_type.value}",
                    "severity": anomaly.severity,
                    "z_score": anomaly.z_score,
                    "metric": anomaly.metric_name,
                },
                priority=EventPriority.CRITICAL,
            )
            await self._event_bus.publish(halt_event)
