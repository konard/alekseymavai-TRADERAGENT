from __future__ import annotations

import math

import pytest

from bot.agents.meta.ensemble import (
    EnsemblePredictor,
    EnsemblePrediction,
    PredictionSource,
    RegimeAdaptiveParams,
)


# ── PredictionSource ────────────────────────────────────────────────

class TestPredictionSource:
    def test_to_dict_basic(self):
        src = PredictionSource("markov", "bullish", 0.9, 1.0, [True, False, True])
        d = src.to_dict()
        assert d["name"] == "markov"
        assert d["prediction"] == "bullish"
        assert d["confidence"] == 0.9
        assert d["weight"] == 1.0
        assert d["total_predictions"] == 3
        assert abs(d["rolling_accuracy"] - 2 / 3) < 1e-9


# ── EnsemblePrediction ──────────────────────────────────────────────

class TestEnsemblePrediction:
    def test_to_dict(self):
        ep = EnsemblePrediction("bullish", 0.8, 1.0, [{"name": "a"}], 0.0)
        d = ep.to_dict()
        assert d["direction"] == "bullish"
        assert d["confidence"] == 0.8
        assert d["agreement_ratio"] == 1.0


# ── EnsemblePredictor ───────────────────────────────────────────────

class TestEnsemblePredictor:
    def _make(self, **kw) -> EnsemblePredictor:
        return EnsemblePredictor(**kw)

    def test_register_sources(self):
        ep = self._make()
        ep.register_source("a")
        ep.register_source("b", initial_weight=2.0)
        stats = ep.get_stats()
        assert stats["num_sources"] == 2
        assert stats["sources"]["b"]["weight"] == 2.0

    def test_submit_unknown_source_raises(self):
        ep = self._make()
        with pytest.raises(KeyError):
            ep.submit_prediction("unknown", "bullish", 0.5)

    def test_no_predictions_returns_neutral(self):
        ep = self._make()
        ep.register_source("a")
        pred = ep.predict()
        assert pred.direction == "neutral"
        assert pred.confidence == 0.0

    def test_unanimous_bullish(self):
        ep = self._make()
        for name in ("a", "b", "c"):
            ep.register_source(name)
            ep.submit_prediction(name, "bullish", 0.9)
        pred = ep.predict()
        assert pred.direction == "bullish"
        assert pred.confidence > 0.5
        assert pred.agreement_ratio == 1.0
        assert pred.entropy == 0.0

    def test_unanimous_bearish(self):
        ep = self._make()
        for name in ("a", "b"):
            ep.register_source(name)
            ep.submit_prediction(name, "bearish", 0.8)
        pred = ep.predict()
        assert pred.direction == "bearish"
        assert pred.agreement_ratio == 1.0

    def test_split_vote_neutral(self):
        ep = self._make()
        ep.register_source("bull")
        ep.register_source("bear")
        ep.submit_prediction("bull", "bullish", 0.8)
        ep.submit_prediction("bear", "bearish", 0.8)
        pred = ep.predict()
        assert pred.direction == "neutral"
        assert pred.entropy == 1.0  # 2 equally distributed categories

    def test_weighted_by_accuracy(self):
        ep = self._make(learning_rate=0.5)
        ep.register_source("good", initial_weight=1.0)
        ep.register_source("bad", initial_weight=1.0)
        # Train: "good" is correct 3 times, "bad" is wrong 3 times
        for _ in range(3):
            ep.submit_prediction("good", "bullish", 0.9)
            ep.submit_prediction("bad", "bearish", 0.9)
            ep.record_outcome("bullish")
        # Now good has much higher weight
        rankings = ep.get_source_rankings()
        good_r = next(r for r in rankings if r["name"] == "good")
        bad_r = next(r for r in rankings if r["name"] == "bad")
        assert good_r["weight"] > bad_r["weight"]
        assert good_r["rolling_accuracy"] == 1.0
        assert bad_r["rolling_accuracy"] == 0.0
        # Predict again — "good" says bullish, "bad" says bearish, good should win
        ep.submit_prediction("good", "bullish", 0.9)
        ep.submit_prediction("bad", "bearish", 0.9)
        pred = ep.predict()
        assert pred.direction == "bullish"

    def test_outcome_recording_updates_weights(self):
        ep = self._make(learning_rate=0.1)
        ep.register_source("a", initial_weight=1.0)
        ep.submit_prediction("a", "bullish", 0.8)
        ep.record_outcome("bullish")
        assert ep._sources["a"].weight == pytest.approx(1.1)
        ep.submit_prediction("a", "bullish", 0.8)
        ep.record_outcome("bearish")
        assert ep._sources["a"].weight == pytest.approx(1.1 * 0.9)

    def test_weight_clamped_min(self):
        ep = self._make(learning_rate=0.99, min_weight=0.1)
        ep.register_source("a", initial_weight=0.2)
        ep.submit_prediction("a", "bullish", 1.0)
        ep.record_outcome("bearish")  # wrong → 0.2 * 0.01 = 0.002, clamped to 0.1
        assert ep._sources["a"].weight == 0.1

    def test_weight_clamped_max(self):
        ep = self._make(learning_rate=0.99, max_weight=3.0)
        ep.register_source("a", initial_weight=2.5)
        ep.submit_prediction("a", "bullish", 1.0)
        ep.record_outcome("bullish")  # correct → 2.5 * 1.99 = 4.975, clamped to 3.0
        assert ep._sources["a"].weight == 3.0

    def test_source_rankings_sorted(self):
        ep = self._make()
        ep.register_source("good")
        ep.register_source("bad")
        ep._sources["good"].accuracy_history = [True] * 10
        ep._sources["bad"].accuracy_history = [False] * 10
        rankings = ep.get_source_rankings()
        assert rankings[0]["name"] == "good"
        assert rankings[1]["name"] == "bad"

    def test_entropy_three_way_split(self):
        ep = self._make()
        ep.register_source("a")
        ep.register_source("b")
        ep.register_source("c")
        ep.submit_prediction("a", "bullish", 0.9)
        ep.submit_prediction("b", "bearish", 0.9)
        ep.submit_prediction("c", "neutral", 0.9)
        pred = ep.predict()
        expected = -3 * (1 / 3) * math.log2(1 / 3)
        assert abs(pred.entropy - expected) < 1e-9

    def test_single_source(self):
        ep = self._make()
        ep.register_source("solo")
        ep.submit_prediction("solo", "bearish", 0.7)
        pred = ep.predict()
        assert pred.direction == "bearish"
        assert pred.agreement_ratio == 1.0
        assert len(pred.sources) == 1

    def test_confidence_clamped_to_one(self):
        ep = self._make()
        ep.register_source("a", initial_weight=3.0)
        ep.submit_prediction("a", "bullish", 1.0)
        pred = ep.predict()
        assert pred.confidence <= 1.0

    def test_get_stats(self):
        ep = self._make()
        ep.register_source("x")
        ep.submit_prediction("x", "bullish", 0.5)
        stats = ep.get_stats()
        assert stats["num_sources"] == 1
        assert stats["num_active"] == 1
        assert "x" in stats["sources"]


# ── RegimeAdaptiveParams ────────────────────────────────────────────

class TestRegimeAdaptiveParams:
    def test_bull_overrides(self):
        rap = RegimeAdaptiveParams()
        rap.load_defaults()
        base = {"tp_multiplier": 1.0, "sl_multiplier": 1.0, "position_size_mult": 1.0}
        result = rap.get_params(base, "bull_trend")
        assert result["tp_multiplier"] == pytest.approx(1.5)
        assert result["sl_multiplier"] == pytest.approx(0.8)
        assert result["position_size_mult"] == pytest.approx(1.2)

    def test_bear_overrides(self):
        rap = RegimeAdaptiveParams()
        rap.load_defaults()
        base = {"tp_multiplier": 2.0, "sl_multiplier": 2.0, "position_size_mult": 1.0}
        result = rap.get_params(base, "bear_trend")
        assert result["tp_multiplier"] == pytest.approx(1.6)  # 2.0 * 0.8
        assert result["sl_multiplier"] == pytest.approx(2.4)  # 2.0 * 1.2
        assert result["position_size_mult"] == pytest.approx(0.7)

    def test_volatile_overrides(self):
        rap = RegimeAdaptiveParams()
        rap.load_defaults()
        base = {"tp_multiplier": 1.0, "sl_multiplier": 1.0, "position_size_mult": 1.0}
        result = rap.get_params(base, "volatile")
        assert result["tp_multiplier"] == pytest.approx(2.0)
        assert result["sl_multiplier"] == pytest.approx(1.5)
        assert result["position_size_mult"] == pytest.approx(0.5)

    def test_unknown_regime_no_change(self):
        rap = RegimeAdaptiveParams()
        rap.load_defaults()
        base = {"tp_multiplier": 1.0, "sl_multiplier": 1.0}
        result = rap.get_params(base, "unknown_regime")
        assert result == base

    def test_custom_overrides(self):
        rap = RegimeAdaptiveParams()
        rap.define_regime_overrides("my_regime", {"tp_multiplier": 3.0})
        base = {"tp_multiplier": 2.0, "sl_multiplier": 1.0}
        result = rap.get_params(base, "my_regime")
        assert result["tp_multiplier"] == pytest.approx(6.0)  # 2.0 * 3.0
        assert result["sl_multiplier"] == 1.0  # untouched

    def test_load_defaults(self):
        rap = RegimeAdaptiveParams()
        rap.load_defaults()
        stats = rap.get_stats()
        assert stats["num_regimes"] == 5
        assert set(stats["regimes"]) == {
            "bull_trend", "bear_trend", "sideways", "volatile", "accumulation",
        }

    def test_base_params_not_mutated(self):
        rap = RegimeAdaptiveParams()
        rap.load_defaults()
        base = {"tp_multiplier": 1.0, "sl_multiplier": 1.0}
        _ = rap.get_params(base, "bull_trend")
        assert base["tp_multiplier"] == 1.0  # original unchanged

    def test_override_adds_missing_key(self):
        rap = RegimeAdaptiveParams()
        rap.define_regime_overrides("r", {"new_key": 42.0})
        result = rap.get_params({}, "r")
        assert result["new_key"] == 42.0

    def test_get_stats(self):
        rap = RegimeAdaptiveParams()
        rap.define_regime_overrides("x", {"a": 1.0})
        stats = rap.get_stats()
        assert stats["num_regimes"] == 1
        assert "x" in stats["regimes"]
