"""Tests for MarkovTransitionModel and PatternLearner."""

import time

import pytest

from bot.core.predictive_model import MarkovTransitionModel, Prediction
from bot.core.pattern_learner import PatternLearner, Pattern, PatternMatch
from bot.core.event_bus import DomainEvent, EventBus


# ---------------------------------------------------------------------------
# MarkovTransitionModel
# ---------------------------------------------------------------------------


class TestMarkovTransitionModel:
    @pytest.fixture
    def model(self):
        return MarkovTransitionModel(smoothing=0.01)

    def test_observe_single(self, model):
        model.observe("r:1", "bull_trend", ts=1.0)
        model.observe("r:1", "bear_trend", ts=2.0)
        assert "bull_trend" in model._states
        assert "bear_trend" in model._states
        assert model._total_transitions == 1

    def test_no_self_transition(self, model):
        """Same state twice should NOT count as transition."""
        model.observe("r:1", "bull_trend", ts=1.0)
        model.observe("r:1", "bull_trend", ts=2.0)
        assert model._total_transitions == 0

    def test_predict_next_first_order(self, model):
        for _ in range(3):
            model.observe("r:1", "bull_trend", ts=1.0)
            model.observe("r:1", "bear_trend", ts=2.0)
        model.observe("r:1", "bull_trend", ts=3.0)
        model.observe("r:1", "tight_range", ts=4.0)

        pred = model.predict_next("bull_trend")
        assert isinstance(pred, Prediction)
        assert pred.predicted_state == "bear_trend"
        assert pred.probability > 0.5

    def test_predict_next_second_order(self, model):
        for i in range(5):
            model.observe("e:1", "A", ts=float(i * 3))
            model.observe("e:1", "B", ts=float(i * 3 + 1))
            model.observe("e:1", "C", ts=float(i * 3 + 2))

        pred = model.predict_next("B", prev_state="A")
        assert pred.predicted_state == "C"
        assert "second-order" in pred.basis

    def test_predict_unknown_state(self, model):
        model.observe("r:1", "A", ts=1.0)
        model.observe("r:1", "B", ts=2.0)
        pred = model.predict_next("X")
        assert pred.predicted_state in model._states or pred.predicted_state == "unknown"

    def test_predict_sequence(self, model):
        for i in range(10):
            model.observe("r:1", "A", ts=float(i * 2))
            model.observe("r:1", "B", ts=float(i * 2 + 1))

        seq = model.predict_sequence("A", steps=3)
        assert len(seq) == 3
        assert seq[0].predicted_state == "B"

    def test_transition_matrix(self, model):
        model.observe("r:1", "A", ts=1.0)
        model.observe("r:1", "B", ts=2.0)
        model.observe("r:1", "A", ts=3.0)

        matrix = model.get_transition_matrix()
        assert "A" in matrix
        assert "B" in matrix
        assert matrix["A"]["B"] > matrix["A"]["A"]

    def test_stationary_distribution(self, model):
        for i in range(20):
            model.observe("r:1", "A", ts=float(i * 2))
            model.observe("r:1", "B", ts=float(i * 2 + 1))

        dist = model.get_stationary_distribution()
        assert len(dist) == 2
        assert abs(dist["A"] - dist["B"]) < 0.1

    def test_entropy(self, model):
        for i in range(10):
            model.observe("r:1", "A", ts=float(i * 2))
            model.observe("r:1", "B", ts=float(i * 2 + 1))

        entropy_a = model.get_entropy("A")
        assert entropy_a < 1.5

    def test_transition_time_estimation(self, model):
        model.observe("r:1", "A", ts=100.0)
        model.observe("r:1", "B", ts=200.0)
        model.observe("r:1", "A", ts=300.0)
        model.observe("r:1", "B", ts=400.0)

        pred = model.predict_next("A")
        assert pred.horizon_seconds is not None
        assert pred.horizon_seconds == 100.0

    def test_observe_from_events(self, model):
        events = [
            DomainEvent(
                entity_type="regime", entity_id="r1",
                event_type="REGIME_DETECTED",
                data={"regime": "bull_trend"}, bot_name="t", ts=1.0,
            ),
            DomainEvent(
                entity_type="regime", entity_id="r1",
                event_type="TRANSITION_COMPLETE",
                data={"regime": "bear_trend"}, bot_name="t", ts=2.0,
            ),
        ]
        model.observe_from_events(events, state_extractor="regime")
        assert model._total_transitions == 1
        assert "bull_trend" in model._states

    def test_get_stats(self, model):
        model.observe("r:1", "A", ts=1.0)
        model.observe("r:1", "B", ts=2.0)
        stats = model.get_stats()
        assert stats["total_states"] == 2
        assert stats["total_transitions"] == 1

    def test_empty_model(self, model):
        pred = model.predict_next("X")
        assert pred.predicted_state == "unknown"
        assert pred.probability == 0.0
        assert model.get_stationary_distribution() == {}

    @pytest.mark.asyncio
    async def test_subscribe_to_bus(self, model):
        bus = EventBus(bot_name="test")
        await model.subscribe_to_bus(bus, entity_types=["regime"])

        for i, regime in enumerate(["bull_trend", "bear_trend", "bull_trend"]):
            evt = DomainEvent(
                entity_type="regime", entity_id="r1",
                event_type="REGIME_DETECTED",
                data={"regime": regime},
                bot_name="test", ts=float(i),
            )
            await bus.publish(evt)

        assert model._total_transitions == 2


# ---------------------------------------------------------------------------
# PatternLearner
# ---------------------------------------------------------------------------


class TestPatternLearner:
    @pytest.fixture
    def learner(self):
        return PatternLearner(min_occurrences=3)

    def test_observe_open_close(self, learner):
        learner.observe_open("p1", {"regime": "bull_trend", "strategy": "tf", "direction": "LONG", "adx": 30})
        learner.observe_close("p1", pnl=100.0)
        assert learner._total_observations == 1
        assert len(learner._patterns) == 1

    def test_pattern_accumulates(self, learner):
        ctx = {"regime": "bull_trend", "strategy": "tf", "direction": "LONG", "adx": 30}
        for i in range(5):
            learner.observe_open(f"p{i}", ctx)
            learner.observe_close(f"p{i}", pnl=100.0 if i % 2 == 0 else -50.0)

        assert learner._total_observations == 5
        assert len(learner._patterns) == 1
        p = list(learner._patterns.values())[0]
        assert p.occurrences == 5
        assert p.wins == 3
        assert p.losses == 2

    def test_close_without_open_ignored(self, learner):
        learner.observe_close("unknown_pos", pnl=100.0)
        assert learner._total_observations == 0

    def test_match_signal_exact(self, learner):
        ctx = {"regime": "bull_trend", "strategy": "tf", "direction": "LONG", "adx": 30}
        for i in range(5):
            learner.observe_open(f"p{i}", ctx)
            learner.observe_close(f"p{i}", pnl=200.0)

        matches = learner.match_signal(ctx)
        assert len(matches) >= 1
        assert matches[0].match_score == 1.0
        assert matches[0].expected_win_rate == 1.0

    def test_match_signal_no_data(self, learner):
        matches = learner.match_signal({"regime": "x", "strategy": "y", "direction": "Z"})
        assert len(matches) == 0

    def test_wilson_score(self, learner):
        p = Pattern(
            pattern_id="test", name="test", description="test", conditions={},
        )
        for _ in range(7):
            p.update_stats(100.0)
        for _ in range(3):
            p.update_stats(-50.0)

        assert p.win_rate == 0.7
        assert p.confidence > 0
        assert p.confidence < p.win_rate

    def test_get_recommendation(self, learner):
        p = Pattern(pattern_id="a", name="a", description="a", conditions={})
        for _ in range(10):
            p.update_stats(100.0)
        assert PatternLearner._get_recommendation(p) == "strong_buy"

        p2 = Pattern(pattern_id="b", name="b", description="b", conditions={})
        for _ in range(10):
            p2.update_stats(-100.0)
        assert PatternLearner._get_recommendation(p2) == "strong_avoid"

    def test_get_top_patterns(self, learner):
        for regime in ["bull_trend", "bear_trend"]:
            ctx = {"regime": regime, "strategy": "tf", "direction": "LONG", "adx": 30}
            for i in range(5):
                learner.observe_open(f"p_{regime}_{i}", ctx)
                learner.observe_close(f"p_{regime}_{i}", pnl=100.0 if regime == "bull_trend" else -50.0)

        top = learner.get_top_patterns(n=5)
        assert len(top) >= 1

    def test_get_stats(self, learner):
        stats = learner.get_stats()
        assert "total_patterns" in stats
        assert "total_observations" in stats

    def test_bucketize(self):
        assert PatternLearner._bucketize(10, [15, 25, 40]) == "<15"
        assert PatternLearner._bucketize(20, [15, 25, 40]) == "<25"
        assert PatternLearner._bucketize(50, [15, 25, 40]) == ">=40"
        assert PatternLearner._bucketize(None, [15, 25, 40]) == "unknown"

    def test_hour_bucket(self):
        assert PatternLearner._hour_bucket(3) == "asia"
        assert PatternLearner._hour_bucket(10) == "europe"
        assert PatternLearner._hour_bucket(20) == "us"
        assert PatternLearner._hour_bucket(None) == "unknown"

    @pytest.mark.asyncio
    async def test_subscribe_to_bus(self, learner):
        bus = EventBus(bot_name="test")
        learner._bus = bus
        await learner.start()

        open_evt = DomainEvent(
            entity_type="position", entity_id="pos_1",
            event_type="POSITION_OPENED",
            data={"regime": "bull_trend", "strategy": "tf", "direction": "LONG", "adx": 30},
            bot_name="test",
        )
        await bus.publish(open_evt)

        close_evt = DomainEvent(
            entity_type="position", entity_id="pos_1",
            event_type="POSITION_CLOSED",
            data={"pnl": 250.0},
            bot_name="test",
        )
        await bus.publish(close_evt)

        assert learner._total_observations == 1

        await learner.stop()
