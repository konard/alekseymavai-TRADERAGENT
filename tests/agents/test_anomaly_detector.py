"""Tests for AnomalyDetector — market anomaly detection with z-score analysis."""

import asyncio
import random
import time

import pytest

from bot.agents.anomaly_detector import Anomaly, AnomalyDetector, AnomalyType
from bot.core.event_bus import EventBus


def test_no_anomaly_normal_data():
    """Feed 50 normal values, no anomalies detected."""
    random.seed(42)
    detector = AnomalyDetector(window_size=50, z_threshold=3.0)

    all_anomalies = []
    for _ in range(50):
        anomalies = detector.observe({
            "volume": random.gauss(1000, 100),
            "price_change_pct": random.gauss(0.005, 0.002),
        })
        all_anomalies.extend(anomalies)

    assert len(all_anomalies) == 0


def test_volume_spike_detected():
    """Feed 50 normal volumes then one 10x spike, anomaly detected."""
    random.seed(42)
    detector = AnomalyDetector(window_size=50, z_threshold=3.0)

    for _ in range(50):
        detector.observe({"volume": random.gauss(1000, 100)})

    anomalies = detector.observe({"volume": 10000})
    assert len(anomalies) > 0
    assert anomalies[0].anomaly_type == AnomalyType.VOLUME_SPIKE
    assert anomalies[0].metric_name == "volume"


def test_price_spike_detected():
    """Feed normal prices then a 5% jump, anomaly detected."""
    random.seed(42)
    detector = AnomalyDetector(window_size=50, z_threshold=3.0)

    for _ in range(50):
        detector.observe({"price_change_pct": random.gauss(0.005, 0.002)})

    anomalies = detector.observe({"price_change_pct": 0.05})
    assert len(anomalies) > 0
    assert anomalies[0].anomaly_type == AnomalyType.PRICE_SPIKE


def test_z_score_calculation():
    """Verify z_score is approximately correct."""
    detector = AnomalyDetector(window_size=100, z_threshold=2.0)

    # Feed 30 identical values to get std ~0, then add different values
    # Use known distribution: mean=100, std=10
    random.seed(123)
    for _ in range(30):
        detector.observe({"volume": random.gauss(100, 10)})

    # A value of ~200 should have z_score around (200 - 100) / 10 = 10
    anomalies = detector.observe({"volume": 200})
    assert len(anomalies) > 0
    # z_score should be large and positive
    assert anomalies[0].z_score > 5.0


def test_severity_calculation():
    """Higher z_score = higher severity."""
    random.seed(42)
    detector = AnomalyDetector(window_size=50, z_threshold=3.0)

    for _ in range(50):
        detector.observe({"volume": random.gauss(1000, 100)})

    # Moderate spike — just above threshold
    anomalies_moderate = detector.observe({"volume": 1400})

    # Reset and try extreme spike
    random.seed(42)
    detector2 = AnomalyDetector(window_size=50, z_threshold=3.0)
    for _ in range(50):
        detector2.observe({"volume": random.gauss(1000, 100)})

    anomalies_extreme = detector2.observe({"volume": 50000})

    assert len(anomalies_moderate) > 0
    assert len(anomalies_extreme) > 0
    assert anomalies_extreme[0].severity > anomalies_moderate[0].severity


def test_halt_triggered_on_extreme():
    """Severity >= halt_threshold triggers halt."""
    random.seed(42)
    detector = AnomalyDetector(
        window_size=50, z_threshold=3.0, halt_threshold=0.8
    )

    for _ in range(50):
        detector.observe({"volume": random.gauss(1000, 50)})

    # Massive spike should produce severity >= 0.8
    anomalies = detector.observe({"volume": 50000})
    assert len(anomalies) > 0

    halted = detector.check_halt(anomalies)
    assert halted is True
    assert detector._halted is True


def test_halt_cooldown():
    """Second extreme within cooldown_seconds does not re-halt."""
    random.seed(42)
    detector = AnomalyDetector(
        window_size=50, z_threshold=3.0, halt_threshold=0.5, cooldown_seconds=300
    )

    for _ in range(50):
        detector.observe({"volume": random.gauss(1000, 50)})

    # First halt
    anomalies1 = detector.observe({"volume": 50000})
    assert detector.check_halt(anomalies1) is True

    # Second halt attempt immediately — should be blocked by cooldown
    anomalies2 = detector.observe({"volume": 60000})
    if anomalies2:
        result = detector.check_halt(anomalies2)
        assert result is False  # within cooldown


def test_observe_candle():
    """Feed OHLCV data, extracts correct metrics."""
    random.seed(42)
    detector = AnomalyDetector(window_size=50, z_threshold=3.0)

    # Feed normal candles
    for _ in range(50):
        o = 100.0
        c = o + random.gauss(0, 0.5)
        h = max(o, c) + random.uniform(0, 0.3)
        l_ = min(o, c) - random.uniform(0, 0.3)
        v = random.gauss(1000, 100)
        detector.observe_candle(open=o, high=h, low=l_, close=c, volume=v)

    # Feed spike candle: huge volume & big move
    anomalies = detector.observe_candle(
        open=100.0, high=115.0, low=99.0, close=112.0, volume=10000
    )
    assert len(anomalies) > 0

    # Check that volume and/or price metrics were tracked
    stats = detector.get_stats()
    assert "volume" in stats["metrics_tracked"]
    assert "price_change_pct" in stats["metrics_tracked"]


def test_reset_halt():
    """Halted state can be cleared."""
    detector = AnomalyDetector()
    detector._halted = True
    assert detector._halted is True

    detector.reset_halt()
    assert detector._halted is False


def test_insufficient_window():
    """Fewer than 20 observations, no anomaly (insufficient data)."""
    detector = AnomalyDetector(window_size=50, z_threshold=3.0)

    all_anomalies = []
    for i in range(19):
        # Even extreme values shouldn't trigger — not enough data
        anomalies = detector.observe({"volume": 1000 if i < 18 else 999999})
        all_anomalies.extend(anomalies)

    assert len(all_anomalies) == 0


def test_get_stats():
    """Correct structure returned from get_stats."""
    random.seed(42)
    detector = AnomalyDetector(window_size=50, z_threshold=3.0)

    for _ in range(30):
        detector.observe({"volume": random.gauss(1000, 100)})

    stats = detector.get_stats()
    assert "total_anomalies" in stats
    assert "anomalies_by_type" in stats
    assert "is_halted" in stats
    assert "metrics_tracked" in stats
    assert "window_size" in stats
    assert stats["window_size"] == 50
    assert stats["is_halted"] is False
    assert isinstance(stats["anomalies_by_type"], dict)


def test_multiple_anomaly_types():
    """Different metrics can trigger independently."""
    random.seed(42)
    detector = AnomalyDetector(window_size=50, z_threshold=3.0)

    # Build separate baselines
    for _ in range(50):
        detector.observe({
            "volume": random.gauss(1000, 100),
            "price_change_pct": random.gauss(0.005, 0.002),
        })

    # Spike both metrics
    anomalies = detector.observe({
        "volume": 10000,
        "price_change_pct": 0.05,
    })

    assert len(anomalies) >= 2

    types_found = {a.anomaly_type for a in anomalies}
    assert AnomalyType.VOLUME_SPIKE in types_found
    assert AnomalyType.PRICE_SPIKE in types_found


def test_anomaly_to_dict():
    """Anomaly.to_dict returns correct structure."""
    anomaly = Anomaly(
        anomaly_type=AnomalyType.VOLUME_SPIKE,
        severity=0.75,
        z_score=4.5,
        description="volume z-score 4.50 exceeds threshold 3.0",
        metric_name="volume",
        metric_value=5000.0,
        threshold=3.0,
        ts=1000.0,
    )
    d = anomaly.to_dict()
    assert d["anomaly_type"] == "volume_spike"
    assert d["severity"] == 0.75
    assert d["z_score"] == 4.5
    assert d["metric_value"] == 5000.0
    assert d["ts"] == 1000.0


def test_get_recent_anomalies():
    """get_recent_anomalies returns dicts with limit."""
    random.seed(42)
    detector = AnomalyDetector(window_size=50, z_threshold=3.0)

    for _ in range(50):
        detector.observe({"volume": random.gauss(1000, 100)})

    # Create several anomalies
    for _ in range(5):
        detector.observe({"volume": 10000})

    recent = detector.get_recent_anomalies(limit=3)
    assert len(recent) == 3
    assert isinstance(recent[0], dict)


@pytest.mark.asyncio
async def test_publish_anomaly_with_event_bus():
    """publish_anomaly sends ANOMALY_DETECTED event to EventBus."""
    bus = EventBus(bot_name="test_bot")
    detector = AnomalyDetector(event_bus=bus)

    received_events = []
    bus.subscribe("ANOMALY_DETECTED", lambda e: received_events.append(e))

    anomaly = Anomaly(
        anomaly_type=AnomalyType.VOLUME_SPIKE,
        severity=0.5,
        z_score=4.0,
        description="test anomaly",
        metric_name="volume",
        metric_value=5000.0,
        threshold=3.0,
    )
    await detector.publish_anomaly(anomaly)

    assert len(received_events) == 1
    assert received_events[0].event_type == "ANOMALY_DETECTED"
    assert received_events[0].data["anomaly_type"] == "volume_spike"


@pytest.mark.asyncio
async def test_publish_anomaly_risk_halt_event():
    """High severity anomaly also publishes RISK_HALTED event."""
    bus = EventBus(bot_name="test_bot")
    detector = AnomalyDetector(event_bus=bus, halt_threshold=0.8)

    halt_events = []
    bus.subscribe("RISK_HALTED", lambda e: halt_events.append(e))

    anomaly = Anomaly(
        anomaly_type=AnomalyType.VOLATILITY_EXPLOSION,
        severity=0.9,
        z_score=6.0,
        description="extreme volatility",
        metric_name="volatility",
        metric_value=0.1,
        threshold=3.0,
    )
    await detector.publish_anomaly(anomaly)

    assert len(halt_events) == 1
    assert halt_events[0].event_type == "RISK_HALTED"
    assert halt_events[0].data["reason"] == "Anomaly: volatility_explosion"
