from __future__ import annotations

import math

import pytest

from bot.agents.runtime.market_analysis import (
    LiquidityScore,
    LiquidityScorer,
    VolatilityRegimeClassifier,
    VolRegime,
)


# ======================================================================
# VolatilityRegimeClassifier tests
# ======================================================================


class TestVolatilityRegimeClassifier:
    """Tests for VolatilityRegimeClassifier."""

    def test_calm_regime_small_returns(self):
        """Very small returns should produce CALM regime."""
        vc = VolatilityRegimeClassifier(window_size=10)
        for i in range(10):
            vc.observe(0.00001 * (1 if i % 2 == 0 else -1), ts=float(i))
        result = vc.classify()
        assert result["regime"] == "calm"
        assert result["annualized_vol"] < 0.005

    def test_normal_regime(self):
        """Moderate returns should produce NORMAL regime."""
        vc = VolatilityRegimeClassifier(window_size=20)
        # Target annualized ~1%: daily vol ~ 0.01/sqrt(365) ~ 0.000523
        ret = 0.00055
        for i in range(20):
            vc.observe(ret * (1 if i % 2 == 0 else -1), ts=float(i))
        result = vc.classify()
        assert result["regime"] == "normal"

    def test_stressed_regime(self):
        """Larger returns should produce STRESSED regime."""
        vc = VolatilityRegimeClassifier(window_size=20)
        # Target annualized ~2%: daily vol ~0.02/sqrt(365) ~ 0.00105
        ret = 0.0012
        for i in range(20):
            vc.observe(ret * (1 if i % 2 == 0 else -1), ts=float(i))
        result = vc.classify()
        assert result["regime"] == "stressed"

    def test_crisis_regime_large_returns(self):
        """Very large returns should produce CRISIS regime."""
        vc = VolatilityRegimeClassifier(window_size=10)
        for i in range(10):
            vc.observe(0.05 * (1 if i % 2 == 0 else -1), ts=float(i))
        result = vc.classify()
        assert result["regime"] == "crisis"
        assert result["annualized_vol"] >= 0.03

    def test_trend_increasing(self):
        """Second half more volatile than first half -> increasing."""
        vc = VolatilityRegimeClassifier(window_size=10)
        # First half: small returns
        for i in range(5):
            vc.observe(0.0001, ts=float(i))
        # Second half: large returns
        for i in range(5, 10):
            vc.observe(0.01, ts=float(i))
        result = vc.classify()
        assert result["trend"] == "increasing"

    def test_trend_decreasing(self):
        """First half more volatile than second half -> decreasing."""
        vc = VolatilityRegimeClassifier(window_size=10)
        for i in range(5):
            vc.observe(0.01, ts=float(i))
        for i in range(5, 10):
            vc.observe(0.0001, ts=float(i))
        result = vc.classify()
        assert result["trend"] == "decreasing"

    def test_risk_multiplier_calm(self):
        vc = VolatilityRegimeClassifier(window_size=5)
        for i in range(5):
            vc.observe(0.000001, ts=float(i))
        vc.classify()
        assert vc.get_risk_multiplier() == 1.2

    def test_risk_multiplier_crisis(self):
        vc = VolatilityRegimeClassifier(window_size=5)
        for i in range(5):
            vc.observe(0.1, ts=float(i))
        vc.classify()
        assert vc.get_risk_multiplier() == 0.3

    def test_risk_multiplier_no_classification(self):
        """Before any classify() call, default multiplier is 1.0."""
        vc = VolatilityRegimeClassifier()
        assert vc.get_risk_multiplier() == 1.0

    def test_recommended_params_normal(self):
        vc = VolatilityRegimeClassifier(window_size=5)
        for i in range(5):
            vc.observe(0.00055 * (1 if i % 2 == 0 else -1), ts=float(i))
        vc.classify()
        params = vc.get_recommended_params()
        assert params["sl_width"] == 1.0
        assert params["tp_width"] == 1.0
        assert params["position_size"] == 1.0

    def test_recommended_params_stressed(self):
        vc = VolatilityRegimeClassifier(window_size=5)
        for i in range(5):
            vc.observe(0.0012 * (1 if i % 2 == 0 else -1), ts=float(i))
        vc.classify()
        params = vc.get_recommended_params()
        assert params["sl_width"] == 1.5
        assert params["position_size"] == 0.6

    def test_regime_history(self):
        vc = VolatilityRegimeClassifier(window_size=5)
        for i in range(5):
            vc.observe(0.000001, ts=float(i))
        vc.classify()
        for i in range(5):
            vc.observe(0.1, ts=float(i + 5))
        vc.classify()
        hist = vc.get_regime_history()
        assert len(hist) == 2
        assert hist[0]["regime"] == "calm"
        assert hist[1]["regime"] == "crisis"

    def test_empty_window(self):
        """classify() with no observations should return zeros safely."""
        vc = VolatilityRegimeClassifier()
        result = vc.classify()
        assert result["regime"] == "calm"
        assert result["volatility"] == 0.0
        assert result["annualized_vol"] == 0.0

    def test_percentile_calculation(self):
        """Percentile should rank current vol among historical vols."""
        vc = VolatilityRegimeClassifier(window_size=5)
        # First classification: low vol
        for i in range(5):
            vc.observe(0.000001, ts=float(i))
        vc.classify()

        # Second classification: high vol
        for i in range(5):
            vc.observe(0.1, ts=float(i + 5))
        result = vc.classify()
        # High vol should rank above the earlier low vol
        assert result["percentile"] == 100.0

    def test_classify_returns_all_keys(self):
        vc = VolatilityRegimeClassifier(window_size=5)
        for i in range(5):
            vc.observe(0.001, ts=float(i))
        result = vc.classify()
        expected_keys = {"regime", "volatility", "annualized_vol", "percentile", "trend", "ts"}
        assert set(result.keys()) == expected_keys

    def test_stats(self):
        vc = VolatilityRegimeClassifier(window_size=10)
        for i in range(7):
            vc.observe(0.001, ts=float(i))
        vc.classify()
        stats = vc.get_stats()
        assert stats["window_size"] == 10
        assert stats["observations"] == 7
        assert stats["history_len"] == 1


# ======================================================================
# LiquidityScorer tests
# ======================================================================


class TestLiquidityScorer:
    """Tests for LiquidityScorer."""

    def _observe_n(
        self,
        scorer: LiquidityScorer,
        symbol: str,
        spread: float,
        depth: float,
        volume: float,
        n: int = 5,
    ):
        for i in range(n):
            scorer.observe(symbol, spread, depth, volume, ts=float(i))

    def test_excellent_liquidity(self):
        """Tight spread + deep book + high volume -> excellent."""
        sc = LiquidityScorer()
        self._observe_n(sc, "BTCUSDT", spread=0.0001, depth=200_000, volume=5_000_000)
        result = sc.score("BTCUSDT")
        assert result.level == "excellent"
        assert result.score >= 0.8
        assert result.tradeable is True

    def test_poor_liquidity_wide_spread(self):
        """Wide spread should degrade the score substantially."""
        sc = LiquidityScorer()
        self._observe_n(sc, "SHITCOIN", spread=0.004, depth=5_000, volume=50_000)
        result = sc.score("SHITCOIN")
        assert result.score < 0.4
        assert result.level in ("poor", "dangerous")

    def test_tradeable_check(self):
        sc = LiquidityScorer(min_score_to_trade=0.5)
        self._observe_n(sc, "AAA", spread=0.004, depth=5_000, volume=10_000)
        assert sc.should_trade("AAA") is False

    def test_tradeable_true(self):
        sc = LiquidityScorer(min_score_to_trade=0.3)
        self._observe_n(sc, "BTCUSDT", spread=0.0001, depth=200_000, volume=5_000_000)
        assert sc.should_trade("BTCUSDT") is True

    def test_max_safe_order(self):
        """max_safe_order_usd = avg_depth * 0.1."""
        sc = LiquidityScorer()
        self._observe_n(sc, "ETHUSDT", spread=0.001, depth=100_000, volume=1_000_000)
        result = sc.score("ETHUSDT")
        assert result.max_safe_order_usd == pytest.approx(10_000.0)

    def test_score_components(self):
        """Individual component scores should be between 0 and 1."""
        sc = LiquidityScorer()
        self._observe_n(sc, "X", spread=0.001, depth=50_000, volume=500_000)
        result = sc.score("X")
        for key in ("spread_score", "depth_score", "volume_score", "stability_score"):
            assert 0.0 <= result.components[key] <= 1.0, f"{key} out of range"

    def test_multiple_symbols(self):
        sc = LiquidityScorer()
        self._observe_n(sc, "A", spread=0.0001, depth=200_000, volume=5_000_000)
        self._observe_n(sc, "B", spread=0.004, depth=5_000, volume=50_000)
        all_scores = sc.score_all()
        assert len(all_scores) == 2
        assert all_scores["A"].score > all_scores["B"].score

    def test_best_symbols_ranking(self):
        sc = LiquidityScorer()
        self._observe_n(sc, "A", spread=0.0001, depth=200_000, volume=5_000_000)
        self._observe_n(sc, "B", spread=0.001, depth=80_000, volume=800_000)
        self._observe_n(sc, "C", spread=0.004, depth=5_000, volume=50_000)
        best = sc.get_best_symbols(n=2)
        assert len(best) == 2
        assert best[0].symbol == "A"
        assert best[0].score >= best[1].score

    def test_insufficient_data(self):
        """Unknown symbol with no observations returns dangerous."""
        sc = LiquidityScorer()
        result = sc.score("UNKNOWN")
        assert result.level == "dangerous"
        assert result.score == 0.0
        assert result.tradeable is False
        assert result.max_safe_order_usd == 0.0

    def test_dangerous_level(self):
        """Very poor parameters should yield dangerous level."""
        sc = LiquidityScorer()
        self._observe_n(sc, "JUNK", spread=0.01, depth=500, volume=1_000)
        result = sc.score("JUNK")
        assert result.level == "dangerous"
        assert result.score < 0.2

    def test_stats(self):
        sc = LiquidityScorer()
        self._observe_n(sc, "A", spread=0.001, depth=100_000, volume=1_000_000, n=3)
        stats = sc.get_stats()
        assert stats["symbols_tracked"] == 1
        assert stats["observations_per_symbol"]["A"] == 3

    def test_to_dict(self):
        ls = LiquidityScore(
            symbol="BTC",
            score=0.85,
            level="excellent",
            components={
                "spread_score": 0.9,
                "depth_score": 0.8,
                "volume_score": 0.85,
                "stability_score": 0.7,
            },
            tradeable=True,
            max_safe_order_usd=10_000.0,
        )
        d = ls.to_dict()
        assert d["symbol"] == "BTC"
        assert d["score"] == 0.85
        assert d["tradeable"] is True
        assert "spread_score" in d["components"]
