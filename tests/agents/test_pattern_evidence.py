"""Tests for PatternEvidenceProvider and PatternAwareCommittee."""

import pytest

from bot.agents.committee import InvestmentCommittee
from bot.agents.smc_expert import SMCExpert
from bot.agents.trend_expert import TrendExpert
from bot.agents.risk_expert import RiskExpert
from bot.agents.contrarian_expert import ContrarianExpert
from bot.agents.pattern_evidence_provider import PatternEvidenceProvider
from bot.agents.pattern_aware_committee import PatternAwareCommittee
from bot.core.pattern_learner import PatternLearner
from bot.core.event_bus import EventBus
from bot.agents.base_expert import CommitteeDecision, Verdict


@pytest.fixture
def event_bus():
    return EventBus(bot_name="test_pattern")


@pytest.fixture
def trained_learner():
    """PatternLearner with enough observations to produce matches."""
    learner = PatternLearner(min_occurrences=3)
    ctx = {"regime": "bull_trend", "strategy": "trend_follower", "direction": "LONG", "adx": 30}
    for i in range(5):
        learner.observe_open(f"p{i}", ctx)
        learner.observe_close(f"p{i}", pnl=100.0)
    return learner


@pytest.fixture
def empty_learner():
    """PatternLearner with no observations."""
    return PatternLearner(min_occurrences=3)


@pytest.fixture
def signal():
    return {
        "direction": "LONG",
        "strategy": "trend_follower",
        "price": 3500.0,
        "symbol": "ETH/USDT",
        "risk_pct": 2.0,
        "stop_loss": 3400.0,
        "take_profit": 3700.0,
    }


@pytest.fixture
def market_data():
    return {
        "regime": "bull_trend",
        "adx": 30.0,
        "trend_strength": 0.7,
        "ema_spread": 0.003,
        "balance": 10000,
        "initial_balance": 10000,
        "drawdown": 0.02,
        "daily_loss": 50,
        "max_daily_loss": 500,
        "open_positions": 2,
        "max_positions": 10,
        "exposure_pct": 30,
    }


class TestPatternEvidenceProvider:
    def test_enrich_adds_pattern_matches(self, trained_learner, signal, market_data):
        """With a trained PatternLearner, enrichment adds pattern_matches list."""
        provider = PatternEvidenceProvider(trained_learner)
        enriched = provider.enrich_market_data(signal, market_data)

        assert "pattern_matches" in enriched
        assert isinstance(enriched["pattern_matches"], list)
        assert len(enriched["pattern_matches"]) > 0

        # Each match has the expected keys
        match = enriched["pattern_matches"][0]
        assert "pattern_name" in match
        assert "match_score" in match
        assert "win_rate" in match
        assert "recommendation" in match
        assert "occurrences" in match
        assert "confidence" in match

        # Recommendation should be derived from pattern data
        assert enriched["pattern_recommendation"] != "no_data"
        assert enriched["pattern_confidence"] > 0.0
        assert enriched["pattern_win_rate"] is not None

    def test_enrich_with_no_patterns(self, empty_learner, signal, market_data):
        """Empty PatternLearner returns pattern_recommendation='no_data'."""
        provider = PatternEvidenceProvider(empty_learner)
        enriched = provider.enrich_market_data(signal, market_data)

        assert enriched["pattern_matches"] == []
        assert enriched["pattern_recommendation"] == "no_data"
        assert enriched["pattern_confidence"] == 0.0
        assert enriched["pattern_win_rate"] is None

    def test_enrich_does_not_mutate_original(self, trained_learner, signal, market_data):
        """Original market_data dict is not changed after enrichment."""
        original_keys = set(market_data.keys())
        original_copy = dict(market_data)

        provider = PatternEvidenceProvider(trained_learner)
        enriched = provider.enrich_market_data(signal, market_data)

        # Original unchanged
        assert set(market_data.keys()) == original_keys
        assert market_data == original_copy
        assert "pattern_matches" not in market_data

        # Enriched has extra keys
        assert "pattern_matches" in enriched

    def test_pattern_recommendation_from_strongest_match(self, signal, market_data):
        """The highest match_score pattern's recommendation is used."""
        learner = PatternLearner(min_occurrences=3)

        # Create one pattern with all wins (should produce strong_buy)
        ctx_win = {"regime": "bull_trend", "strategy": "trend_follower", "direction": "LONG", "adx": 30}
        for i in range(6):
            learner.observe_open(f"win_{i}", ctx_win)
            learner.observe_close(f"win_{i}", pnl=200.0)

        provider = PatternEvidenceProvider(learner)
        enriched = provider.enrich_market_data(signal, market_data)

        # The best match should have a positive recommendation
        assert len(enriched["pattern_matches"]) > 0
        best = enriched["pattern_matches"][0]
        assert best["recommendation"] in ("strong_buy", "buy", "neutral")
        assert enriched["pattern_recommendation"] == best["recommendation"]


class TestPatternAwareCommittee:
    @pytest.mark.asyncio
    async def test_pattern_aware_committee_evaluate(self, event_bus, trained_learner, signal, market_data):
        """Full evaluate() flows enrichment to experts, decision is valid."""
        committee = PatternAwareCommittee(
            experts=[SMCExpert(), TrendExpert(), RiskExpert(), ContrarianExpert()],
            event_bus=event_bus,
            pattern_learner=trained_learner,
        )

        decision = await committee.evaluate(signal, market_data)

        assert isinstance(decision, CommitteeDecision)
        assert decision.verdict in (Verdict.APPROVE, Verdict.REJECT, Verdict.DEFER)
        assert 0.0 <= decision.score <= 1.0
        assert len(decision.votes) > 0
        assert decision.session_id.startswith("session_")

    @pytest.mark.asyncio
    async def test_pattern_aware_committee_status(self, event_bus, trained_learner, signal, market_data):
        """get_status() includes pattern_learner stats."""
        committee = PatternAwareCommittee(
            experts=[TrendExpert(), RiskExpert()],
            event_bus=event_bus,
            pattern_learner=trained_learner,
        )

        # Run one evaluation so there's history
        await committee.evaluate(signal, market_data)

        status = committee.get_status()
        assert "pattern_learner" in status
        assert "total_patterns" in status["pattern_learner"]
        assert "total_observations" in status["pattern_learner"]
        assert status["pattern_learner"]["total_observations"] == 5
        assert status["total_sessions"] == 1
