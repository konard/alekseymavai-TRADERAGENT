from __future__ import annotations

import time

import pytest

from bot.agents.analytics.timeframe_consensus import (
    RegimeTransitionDetector,
    TimeframeAnalyzer,
    TimeframeVerdict,
)


# ── TimeframeVerdict ──────────────────────────────────────────────


class TestTimeframeVerdict:
    def test_to_dict(self):
        v = TimeframeVerdict(
            timeframe="H1",
            direction="bullish",
            strength=0.8,
            signals={"ema_trend": "up", "rsi": 65},
            confidence=0.9,
            ts=1000.0,
        )
        d = v.to_dict()
        assert d["timeframe"] == "H1"
        assert d["direction"] == "bullish"
        assert d["strength"] == 0.8
        assert d["confidence"] == 0.9
        assert d["signals"]["rsi"] == 65
        assert d["ts"] == 1000.0

    def test_default_ts(self):
        before = time.time()
        v = TimeframeVerdict("M5", "neutral", 0.5, {}, 0.5)
        after = time.time()
        assert before <= v.ts <= after


# ── TimeframeAnalyzer ─────────────────────────────────────────────


def _make_verdict(tf: str, direction: str, strength: float = 0.8, confidence: float = 0.9) -> TimeframeVerdict:
    return TimeframeVerdict(tf, direction, strength, {}, confidence, ts=1000.0)


class TestTimeframeAnalyzerAllBullish:
    def test_all_bullish_alignment(self):
        analyzer = TimeframeAnalyzer()
        for tf in ["M5", "H1", "H4", "D1"]:
            analyzer.submit_verdict(_make_verdict(tf, "bullish"))

        result = analyzer.get_alignment_score()
        assert result["score"] > 0.5
        assert result["alignment"] in ("strong_bullish", "bullish")
        assert len(result["aligned_timeframes"]) == 4
        assert result["conflicting_timeframes"] == []

    def test_fully_aligned(self):
        analyzer = TimeframeAnalyzer()
        for tf in ["M5", "H1", "H4", "D1"]:
            analyzer.submit_verdict(_make_verdict(tf, "bullish"))
        assert analyzer.is_fully_aligned() is True

    def test_dominant_direction_bullish(self):
        analyzer = TimeframeAnalyzer()
        for tf in ["M5", "H1", "H4", "D1"]:
            analyzer.submit_verdict(_make_verdict(tf, "bullish"))
        assert analyzer.get_dominant_direction() == "bullish"


class TestTimeframeAnalyzerMixed:
    def test_mixed_directions(self):
        analyzer = TimeframeAnalyzer()
        analyzer.submit_verdict(_make_verdict("M5", "bearish"))
        analyzer.submit_verdict(_make_verdict("H1", "bearish"))
        analyzer.submit_verdict(_make_verdict("H4", "bullish"))
        analyzer.submit_verdict(_make_verdict("D1", "bullish"))

        result = analyzer.get_alignment_score()
        # H4+D1 weight (0.30+0.30=0.60) > M5+H1 (0.15+0.25=0.40)
        assert result["score"] > 0.0
        assert len(result["conflicting_timeframes"]) == 2

    def test_not_fully_aligned(self):
        analyzer = TimeframeAnalyzer()
        analyzer.submit_verdict(_make_verdict("M5", "bullish"))
        analyzer.submit_verdict(_make_verdict("H1", "bearish"))
        assert analyzer.is_fully_aligned() is False

    def test_dominant_direction_mixed(self):
        analyzer = TimeframeAnalyzer()
        analyzer.submit_verdict(_make_verdict("M5", "bullish"))
        analyzer.submit_verdict(_make_verdict("H1", "bullish"))
        analyzer.submit_verdict(_make_verdict("H4", "bearish"))
        assert analyzer.get_dominant_direction() == "bullish"


class TestTimeframeAnalyzerEdgeCases:
    def test_single_tf(self):
        analyzer = TimeframeAnalyzer(timeframes=["H4"])
        analyzer.submit_verdict(_make_verdict("H4", "bearish", strength=1.0, confidence=1.0))
        result = analyzer.get_alignment_score()
        assert result["score"] == pytest.approx(-1.0)
        assert result["alignment"] == "strong_bearish"
        assert analyzer.is_fully_aligned() is True

    def test_custom_weights(self):
        analyzer = TimeframeAnalyzer(
            timeframes=["M5", "D1"],
            weights={"M5": 0.9, "D1": 0.1},
        )
        analyzer.submit_verdict(_make_verdict("M5", "bearish", 1.0, 1.0))
        analyzer.submit_verdict(_make_verdict("D1", "bullish", 1.0, 1.0))
        result = analyzer.get_alignment_score()
        # M5 dominates with 0.9 weight → heavily bearish
        assert result["score"] < -0.5

    def test_empty_analyzer(self):
        analyzer = TimeframeAnalyzer()
        result = analyzer.get_alignment_score()
        assert result["score"] == 0.0
        assert result["alignment"] == "neutral"
        assert analyzer.get_dominant_direction() == "neutral"
        assert analyzer.is_fully_aligned() is False

    def test_missing_tfs(self):
        analyzer = TimeframeAnalyzer()
        # Only submit 2 of 4 TFs
        analyzer.submit_verdict(_make_verdict("M5", "bullish"))
        analyzer.submit_verdict(_make_verdict("H1", "bullish"))
        result = analyzer.get_alignment_score()
        assert result["score"] > 0.0
        assert len(result["details"]) == 2
        assert analyzer.is_fully_aligned() is True  # all submitted agree

    def test_get_stats(self):
        analyzer = TimeframeAnalyzer()
        analyzer.submit_verdict(_make_verdict("H1", "bullish"))
        stats = analyzer.get_stats()
        assert stats["verdicts_count"] == 1
        assert "H1" in stats["submitted_timeframes"]

    def test_score_clamped(self):
        """Score must be in [-1, 1]."""
        analyzer = TimeframeAnalyzer()
        for tf in ["M5", "H1", "H4", "D1"]:
            analyzer.submit_verdict(_make_verdict(tf, "bullish", 1.0, 1.0))
        result = analyzer.get_alignment_score()
        assert -1.0 <= result["score"] <= 1.0

    def test_all_bearish(self):
        analyzer = TimeframeAnalyzer()
        for tf in ["M5", "H1", "H4", "D1"]:
            analyzer.submit_verdict(_make_verdict(tf, "bearish", 1.0, 1.0))
        result = analyzer.get_alignment_score()
        assert result["score"] == pytest.approx(-1.0)
        assert result["alignment"] == "strong_bearish"

    def test_all_neutral(self):
        analyzer = TimeframeAnalyzer()
        for tf in ["M5", "H1", "H4", "D1"]:
            analyzer.submit_verdict(_make_verdict(tf, "neutral", 1.0, 1.0))
        result = analyzer.get_alignment_score()
        assert result["score"] == pytest.approx(0.0)
        assert result["alignment"] == "neutral"


# ── RegimeTransitionDetector ──────────────────────────────────────


class TestRegimeTransitionDetectorStable:
    def test_stable_regime_low_entropy(self):
        det = RegimeTransitionDetector(lookback=20)
        for i in range(20):
            det.observe("bull", ts=float(i))
        result = det.get_transition_probability()
        assert result["current_regime"] == "bull"
        assert result["stability"] == 1.0
        assert result["entropy"] == pytest.approx(0.0)

    def test_stable_no_warning(self):
        det = RegimeTransitionDetector(lookback=20)
        for i in range(20):
            det.observe("bull", ts=float(i))
        warning = det.detect_early_warning()
        assert warning["warning"] is False
        assert warning["level"] == "none"
        assert warning["regime_duration"] == 20

    def test_single_observation(self):
        det = RegimeTransitionDetector()
        det.observe("bear", ts=1.0)
        result = det.get_transition_probability()
        assert result["current_regime"] == "bear"
        assert result["stability"] == 1.0
        assert result["likely_next"] == "bear"
        assert result["transition_probability"] == 0.0


class TestRegimeTransitionDetectorUnstable:
    def test_frequent_transitions_high_entropy(self):
        det = RegimeTransitionDetector(lookback=20, entropy_threshold=0.3)
        regimes = ["bull", "bear", "range", "volatile"] * 5
        for i, r in enumerate(regimes):
            det.observe(r, ts=float(i))
        result = det.get_transition_probability()
        assert result["entropy"] > 1.0  # 4 regimes = log2(4) = 2.0 max

    def test_early_warning_high_entropy(self):
        det = RegimeTransitionDetector(lookback=20, entropy_threshold=0.3)
        regimes = ["bull", "bear", "range", "volatile"] * 5
        for i, r in enumerate(regimes):
            det.observe(r, ts=float(i))
        warning = det.detect_early_warning()
        assert warning["warning"] is True
        assert warning["level"] in ("low", "medium", "high")
        assert any("entropy" in r for r in warning["reasons"])

    def test_transition_probability_value(self):
        det = RegimeTransitionDetector(lookback=10)
        # bull -> bear -> bull -> bear ...
        for i in range(10):
            det.observe("bull" if i % 2 == 0 else "bear", ts=float(i))
        result = det.get_transition_probability()
        assert result["transition_probability"] > 0.0

    def test_short_regime_duration_warning(self):
        det = RegimeTransitionDetector(lookback=20, entropy_threshold=2.0)
        # Long bull run, then switch to bear for 1 bar
        for i in range(18):
            det.observe("bull", ts=float(i))
        det.observe("bear", ts=19.0)
        det.observe("range", ts=20.0)
        warning = det.detect_early_warning()
        # Current "range" duration=1, avg run is much longer
        assert warning["regime_duration"] == 1


class TestRegimeTransitionDetectorEdgeCases:
    def test_empty_window(self):
        det = RegimeTransitionDetector()
        result = det.get_transition_probability()
        assert result["current_regime"] == "unknown"
        assert result["stability"] == 0.0

        warning = det.detect_early_warning()
        assert warning["warning"] is False
        assert warning["regime_duration"] == 0

    def test_lookback_trim(self):
        det = RegimeTransitionDetector(lookback=5)
        for i in range(20):
            det.observe("bull", ts=float(i))
        stats = det.get_stats()
        assert stats["observations"] == 5

    def test_get_stats(self):
        det = RegimeTransitionDetector(entropy_threshold=0.5, lookback=10)
        det.observe("bull", ts=1.0)
        det.observe("bear", ts=2.0)
        stats = det.get_stats()
        assert stats["observations"] == 2
        assert stats["lookback"] == 10
        assert stats["entropy_threshold"] == 0.5
        assert stats["current_regime"] == "bear"
        assert stats["regime_counts"] == {"bull": 1, "bear": 1}

    def test_entropy_calculation(self):
        # Uniform distribution of 2 regimes → entropy = 1.0
        entropy = RegimeTransitionDetector._calculate_entropy({"a": 0.5, "b": 0.5})
        assert entropy == pytest.approx(1.0)

        # Single regime → entropy = 0.0
        entropy = RegimeTransitionDetector._calculate_entropy({"a": 1.0})
        assert entropy == pytest.approx(0.0)
