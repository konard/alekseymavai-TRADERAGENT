"""Tests for CorrelationGuard — cross-pair correlation detection and exposure management."""

import random

import pytest

from bot.agents.correlation_guard import CorrelationAlert, CorrelationGuard


def _make_guard_with_data(
    window_size: int = 30,
    correlation_threshold: float = 0.7,
    max_correlated_exposure: float = 3.0,
    n_bars: int = 50,
    seed: int = 42,
) -> CorrelationGuard:
    """Helper: create a guard with pre-loaded correlated return data."""
    random.seed(seed)
    guard = CorrelationGuard(
        window_size=window_size,
        correlation_threshold=correlation_threshold,
        max_correlated_exposure=max_correlated_exposure,
    )

    for _ in range(n_bars):
        btc_ret = random.gauss(0.001, 0.02)
        eth_ret = btc_ret * 0.9 + random.gauss(0, 0.005)  # highly correlated
        sol_ret = random.gauss(0.0, 0.025)  # uncorrelated
        guard.observe_returns_batch(
            {"BTC/USDT": btc_ret, "ETH/USDT": eth_ret, "SOL/USDT": sol_ret}
        )

    return guard


class TestCalculateCorrelation:
    def test_calculate_correlation_positive(self):
        """Two series that move together should have correlation near 1.0."""
        guard = _make_guard_with_data()
        corr = guard.calculate_correlation("BTC/USDT", "ETH/USDT")
        assert corr is not None
        assert corr > 0.85, f"Expected high positive correlation, got {corr}"

    def test_calculate_correlation_negative(self):
        """Inverse series should have correlation near -1.0."""
        random.seed(99)
        guard = CorrelationGuard(window_size=30)

        for _ in range(50):
            ret = random.gauss(0.001, 0.02)
            guard.observe_return("A", ret)
            guard.observe_return("B", -ret)  # perfectly inverse

        corr = guard.calculate_correlation("A", "B")
        assert corr is not None
        assert corr < -0.95, f"Expected strong negative correlation, got {corr}"

    def test_calculate_correlation_uncorrelated(self):
        """Independent random series should have correlation near 0."""
        guard = _make_guard_with_data()
        corr = guard.calculate_correlation("BTC/USDT", "SOL/USDT")
        assert corr is not None
        assert abs(corr) < 0.4, f"Expected low correlation, got {corr}"

    def test_insufficient_data(self):
        """Fewer than 5 returns should return None."""
        guard = CorrelationGuard(window_size=30)
        guard.observe_return("A", 0.01)
        guard.observe_return("A", 0.02)
        guard.observe_return("B", 0.01)
        guard.observe_return("B", 0.02)

        result = guard.calculate_correlation("A", "B")
        assert result is None


class TestExposureAlerts:
    def test_correlated_same_direction_alert(self):
        """Two LONG positions with high correlation should trigger an alert."""
        guard = _make_guard_with_data(max_correlated_exposure=2.0)

        guard.set_positions(
            {
                "BTC/USDT": {"direction": "LONG", "size_pct": 2.0},
                "ETH/USDT": {"direction": "LONG", "size_pct": 2.0},
            }
        )

        alerts = guard.check_exposure()
        assert len(alerts) >= 1
        alert = alerts[0]
        assert alert.pair_a in ("BTC/USDT", "ETH/USDT")
        assert alert.pair_b in ("BTC/USDT", "ETH/USDT")
        assert alert.correlation > 0.7
        assert alert.recommendation in ("reduce", "hedge")

    def test_uncorrelated_no_alert(self):
        """Two positions with low correlation should not trigger an alert."""
        guard = _make_guard_with_data(max_correlated_exposure=5.0)

        guard.set_positions(
            {
                "BTC/USDT": {"direction": "LONG", "size_pct": 1.0},
                "SOL/USDT": {"direction": "LONG", "size_pct": 1.0},
            }
        )

        alerts = guard.check_exposure()
        # BTC-SOL should not be flagged (low correlation)
        btc_sol_alerts = [
            a
            for a in alerts
            if {a.pair_a, a.pair_b} == {"BTC/USDT", "SOL/USDT"}
        ]
        assert len(btc_sol_alerts) == 0

    def test_opposite_direction_no_alert(self):
        """LONG + SHORT on positively correlated pairs should not alert (natural hedge)."""
        guard = _make_guard_with_data()

        guard.set_positions(
            {
                "BTC/USDT": {"direction": "LONG", "size_pct": 2.0},
                "ETH/USDT": {"direction": "SHORT", "size_pct": 2.0},
            }
        )

        alerts = guard.check_exposure()
        btc_eth_alerts = [
            a
            for a in alerts
            if {a.pair_a, a.pair_b} == {"BTC/USDT", "ETH/USDT"}
        ]
        assert len(btc_eth_alerts) == 0


class TestEffectiveExposure:
    def test_effective_exposure_calculation(self):
        """Three correlated positions should produce effective exposure > sum of sizes."""
        guard = _make_guard_with_data()

        guard.set_positions(
            {
                "BTC/USDT": {"direction": "LONG", "size_pct": 1.0},
                "ETH/USDT": {"direction": "LONG", "size_pct": 1.0},
                "SOL/USDT": {"direction": "LONG", "size_pct": 1.0},
            }
        )

        effective = guard.get_effective_exposure()
        # With 3 positions of size 1.0 each, effective should be > 0
        assert effective > 0.0
        # With some correlation, effective should differ from simple sum (3.0)
        # sqrt(3 + 3*2*rho) * 1.0 — with mixed correlations, hard to predict exact value
        # but it should be a reasonable number
        assert effective > 1.0

    def test_effective_exposure_no_positions(self):
        """No positions should give 0 exposure."""
        guard = CorrelationGuard()
        assert guard.get_effective_exposure() == 0.0

    def test_effective_exposure_single_position(self):
        """Single position should give exactly its size."""
        guard = _make_guard_with_data()
        guard.set_positions({"BTC/USDT": {"direction": "LONG", "size_pct": 2.5}})
        effective = guard.get_effective_exposure()
        assert abs(effective - 2.5) < 0.01


class TestShouldAllowPosition:
    def test_should_allow_position_within_limits(self):
        """Adding a position within limits should be allowed."""
        guard = _make_guard_with_data(max_correlated_exposure=10.0)
        guard.set_positions(
            {"BTC/USDT": {"direction": "LONG", "size_pct": 1.0}}
        )

        allowed, reason = guard.should_allow_new_position("SOL/USDT", "LONG", 1.0)
        assert allowed is True
        assert reason == "ok"

    def test_should_allow_position_exceeding_limits(self):
        """Adding a position that exceeds limits should be blocked."""
        guard = _make_guard_with_data(max_correlated_exposure=1.5)
        guard.set_positions(
            {
                "BTC/USDT": {"direction": "LONG", "size_pct": 2.0},
                "ETH/USDT": {"direction": "LONG", "size_pct": 2.0},
            }
        )

        allowed, reason = guard.should_allow_new_position("SOL/USDT", "LONG", 2.0)
        assert allowed is False
        assert "effective exposure" in reason


class TestCorrelationMatrix:
    def test_correlation_matrix(self):
        """Three symbols should produce a 3x3 matrix with 1.0 on diagonal."""
        guard = _make_guard_with_data()
        matrix = guard.get_correlation_matrix()

        assert len(matrix) == 3
        for sym in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
            assert sym in matrix
            assert matrix[sym][sym] == 1.0

        # BTC-ETH should be highly correlated
        assert matrix["BTC/USDT"]["ETH/USDT"] > 0.8

        # Matrix should be symmetric
        assert abs(
            matrix["BTC/USDT"]["ETH/USDT"] - matrix["ETH/USDT"]["BTC/USDT"]
        ) < 1e-10


class TestMisc:
    def test_get_stats(self):
        """Stats dict should have correct structure."""
        guard = _make_guard_with_data()
        guard.set_positions(
            {
                "BTC/USDT": {"direction": "LONG", "size_pct": 1.0},
                "ETH/USDT": {"direction": "LONG", "size_pct": 1.0},
            }
        )

        stats = guard.get_stats()
        assert stats["tracked_symbols"] == 3
        assert stats["open_positions"] == 2
        assert stats["total_pairs"] == 3  # 3 choose 2
        assert "high_correlation_pairs" in stats
        assert "effective_exposure" in stats
        assert "max_correlated_exposure" in stats
        assert "correlation_threshold" in stats
        assert "window_size" in stats
        assert "total_alerts" in stats

    def test_observe_returns_batch(self):
        """Batch observation should record returns for all symbols."""
        guard = CorrelationGuard(window_size=10)
        guard.observe_returns_batch(
            {"A": 0.01, "B": -0.02, "C": 0.005}
        )

        assert len(guard._returns) == 3
        assert guard._returns["A"] == [0.01]
        assert guard._returns["B"] == [-0.02]
        assert guard._returns["C"] == [0.005]

    def test_correlation_alert_to_dict(self):
        """CorrelationAlert.to_dict should return a valid dict."""
        alert = CorrelationAlert(
            pair_a="BTC/USDT",
            pair_b="ETH/USDT",
            correlation=0.92,
            direction_a="LONG",
            direction_b="LONG",
            effective_exposure=3.84,
            recommendation="reduce",
        )
        d = alert.to_dict()
        assert d["pair_a"] == "BTC/USDT"
        assert d["correlation"] == 0.92
        assert d["recommendation"] == "reduce"
        assert "ts" in d

    def test_recent_alerts_limit(self):
        """get_recent_alerts should respect the limit parameter."""
        guard = _make_guard_with_data(max_correlated_exposure=0.5)
        guard.set_positions(
            {
                "BTC/USDT": {"direction": "LONG", "size_pct": 2.0},
                "ETH/USDT": {"direction": "LONG", "size_pct": 2.0},
            }
        )
        guard.check_exposure()
        guard.check_exposure()  # generate more alerts

        recent = guard.get_recent_alerts(limit=1)
        assert len(recent) <= 1
