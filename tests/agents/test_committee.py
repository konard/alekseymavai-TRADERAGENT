"""Tests for Investment Committee and Expert Agents."""

import pytest

from bot.agents.base_expert import Argument, ArgumentType, Vote, Verdict, CommitteeDecision
from bot.agents.smc_expert import SMCExpert
from bot.agents.trend_expert import TrendExpert
from bot.agents.risk_expert import RiskExpert
from bot.agents.contrarian_expert import ContrarianExpert
from bot.agents.committee import InvestmentCommittee
from bot.core.event_bus import EventBus


@pytest.fixture
def event_bus():
    return EventBus(bot_name="test")


@pytest.fixture
def committee(event_bus):
    c = InvestmentCommittee(event_bus=event_bus)
    c.add_expert(SMCExpert())
    c.add_expert(TrendExpert())
    c.add_expert(RiskExpert())
    c.add_expert(ContrarianExpert())
    return c


@pytest.fixture
def bullish_signal():
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
def bullish_market():
    return {
        "regime": "bull_trend",
        "adx": 35.0,
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


class TestSMCExpert:
    def test_analyze_without_smc_context(self):
        expert = SMCExpert()
        arg = expert.analyze({"direction": "LONG", "price": 100}, {"regime": "bull_trend"})
        assert isinstance(arg, Argument)
        assert arg.expert_name == "smc_structuralist"
        assert 0 <= arg.confidence <= 1

    def test_analyze_returns_argument(self):
        expert = SMCExpert()
        arg = expert.analyze({"direction": "LONG", "price": 100}, {"smc_context": None})
        assert arg.argument_type == ArgumentType.RAISED


class TestTrendExpert:
    def test_bullish_trend_analysis(self):
        expert = TrendExpert()
        arg = expert.analyze(
            {"direction": "LONG", "price": 100},
            {"adx": 35, "regime": "bull_trend", "ema_spread": 0.003},
        )
        assert arg.confidence > 0.6  # should be bullish

    def test_counter_trend_analysis(self):
        expert = TrendExpert()
        arg = expert.analyze(
            {"direction": "LONG", "price": 100},
            {"adx": 35, "regime": "bear_trend", "ema_spread": -0.003},
        )
        assert arg.confidence < 0.5  # should be cautious


class TestRiskExpert:
    def test_healthy_risk(self):
        expert = RiskExpert()
        arg = expert.analyze(
            {
                "direction": "LONG",
                "price": 100,
                "risk_pct": 2.0,
                "stop_loss": 95,
                "take_profit": 110,
            },
            {
                "balance": 10000,
                "drawdown": 0.02,
                "daily_loss": 50,
                "max_daily_loss": 500,
                "open_positions": 2,
                "max_positions": 10,
            },
        )
        assert arg.confidence >= 0.5  # healthy state

    def test_high_risk_red_flags(self):
        expert = RiskExpert()
        arg = expert.analyze(
            {"direction": "LONG", "price": 100, "risk_pct": 2.0},
            {
                "balance": 100,
                "drawdown": 0.25,
                "daily_loss": 400,
                "max_daily_loss": 500,
                "open_positions": 10,
                "max_positions": 10,
                "exposure_pct": 90,
            },
        )
        assert arg.confidence < 0.3  # many red flags
        assert arg.evidence.get("red_flags", 0) >= 2


class TestContrarianExpert:
    def test_no_traps(self):
        expert = ContrarianExpert()
        arg = expert.analyze(
            {"direction": "LONG", "price": 3500},
            {
                "regime": "bull_trend",
                "adx": 30,
                "volume_trend": "rising",
                "regime_duration_seconds": 3600,
            },
        )
        assert arg.evidence.get("traps_detected", 0) == 0

    def test_detects_volume_divergence(self):
        expert = ContrarianExpert()
        arg = expert.analyze(
            {"direction": "LONG", "price": 3500},
            {"regime": "bull_trend", "adx": 30, "volume_trend": "falling"},
        )
        assert arg.evidence.get("volume_divergence") is True


class TestInvestmentCommittee:
    @pytest.mark.asyncio
    async def test_full_evaluation(self, committee, bullish_signal, bullish_market):
        decision = await committee.evaluate(bullish_signal, bullish_market)

        assert isinstance(decision, CommitteeDecision)
        assert decision.verdict in (Verdict.APPROVE, Verdict.REJECT, Verdict.DEFER)
        assert 0 <= decision.score <= 1
        assert len(decision.votes) == 4  # 4 experts
        assert len(decision.arguments) >= 4  # at least 4 initial + possible challenges
        assert decision.session_id.startswith("session_")

    @pytest.mark.asyncio
    async def test_decision_stored_in_history(
        self, committee, bullish_signal, bullish_market
    ):
        await committee.evaluate(bullish_signal, bullish_market)
        assert len(committee.history) == 1

    @pytest.mark.asyncio
    async def test_events_published(self, committee, bullish_signal, bullish_market):
        bus = committee._bus
        events = []
        bus.subscribe("SESSION_STARTED", lambda e: events.append(e))
        bus.subscribe("DECISION_MADE", lambda e: events.append(e))

        await committee.evaluate(bullish_signal, bullish_market)

        session_events = [e for e in events if e.event_type == "SESSION_STARTED"]
        decision_events = [e for e in events if e.event_type == "DECISION_MADE"]
        assert len(session_events) == 1
        assert len(decision_events) == 1

    @pytest.mark.asyncio
    async def test_to_dict(self, committee, bullish_signal, bullish_market):
        decision = await committee.evaluate(bullish_signal, bullish_market)
        d = decision.to_dict()
        assert "verdict" in d
        assert "score" in d
        assert "votes" in d
        assert "reasoning" in d

    @pytest.mark.asyncio
    async def test_status(self, committee, bullish_signal, bullish_market):
        await committee.evaluate(bullish_signal, bullish_market)
        status = committee.get_status()
        assert len(status["experts"]) == 4
        assert status["total_sessions"] == 1
        assert status["last_decision"] is not None

    @pytest.mark.asyncio
    async def test_high_risk_rejection(self, committee):
        """High risk scenario should produce REJECT."""
        signal = {"direction": "LONG", "price": 100, "risk_pct": 2.0}
        market = {
            "regime": "bear_trend",
            "adx": 55,
            "balance": 500,
            "drawdown": 0.30,
            "daily_loss": 450,
            "max_daily_loss": 500,
            "open_positions": 9,
            "max_positions": 10,
            "exposure_pct": 95,
            "volume_trend": "falling",
            "regime_duration_seconds": 120,
        }
        decision = await committee.evaluate(signal, market)
        # With bear_trend, high drawdown, near daily limit, near position limit,
        # high exposure, falling volume — should lean heavily REJECT
        assert decision.verdict in (Verdict.REJECT, Verdict.DEFER)
