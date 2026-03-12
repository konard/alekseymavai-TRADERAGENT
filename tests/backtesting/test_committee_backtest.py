"""Tests for Committee Backtest Adapter and Engine."""

import pytest

from bot.tests.backtesting.committee_backtest_adapter import CommitteeBacktestAdapter
from bot.tests.backtesting.committee_backtest_engine import (
    CommitteeBacktestEngine,
    CommitteeBacktestResult,
)
from bot.agents.committee import InvestmentCommittee
from bot.agents.smc_expert import SMCExpert
from bot.agents.trend_expert import TrendExpert
from bot.agents.risk_expert import RiskExpert
from bot.agents.contrarian_expert import ContrarianExpert
from bot.agents.feedback_tracker import ExpertFeedbackTracker
from bot.agents.regime_profile import RegimeProfileStore
from bot.agents.base_expert import Verdict


def make_test_signals(n=20):
    """Generate N test signals with known outcomes."""
    signals = []
    for i in range(n):
        is_bull = i % 3 != 0  # 2/3 bullish market
        pnl = 100.0 if is_bull else -80.0
        signals.append(
            {
                "signal": {
                    "direction": "LONG",
                    "price": 3500,
                    "strategy": "trend_follower",
                    "symbol": "ETH/USDT",
                    "risk_pct": 2.0,
                    "stop_loss": 3400,
                    "take_profit": 3700,
                },
                "market_data": {
                    "regime": "bull_trend" if is_bull else "bear_trend",
                    "adx": 35.0 if is_bull else 15.0,
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
                },
                "regime": "bull_trend" if is_bull else "bear_trend",
                "pnl": pnl,
            }
        )
    return signals


def _make_committee_and_tracker():
    """Helper: create a committee with 4 experts and a feedback tracker."""
    experts = [SMCExpert(), TrendExpert(), RiskExpert(), ContrarianExpert()]
    committee = InvestmentCommittee(experts=experts)
    tracker = ExpertFeedbackTracker()
    return committee, tracker, experts


# ── Adapter Tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_approve_passes():
    """Committee APPROVE returns (True, decision)."""
    committee, tracker, _ = _make_committee_and_tracker()
    adapter = CommitteeBacktestAdapter(committee=committee, feedback_tracker=tracker)

    # Bull market signal — trend expert and risk expert should lean APPROVE
    signal_entry = make_test_signals(1)[0]
    should_exec, decision = await adapter.should_execute(
        signal=signal_entry["signal"],
        market_data=signal_entry["market_data"],
        current_regime="bull_trend",
    )

    assert isinstance(should_exec, bool)
    assert isinstance(decision, dict)
    assert "verdict" in decision
    assert "session_id" in decision
    assert "votes" in decision

    # If approved, should_exec must be True
    if decision["verdict"] == Verdict.APPROVE.value:
        assert should_exec is True


@pytest.mark.asyncio
async def test_adapter_reject_blocks():
    """Construct a high-risk scenario, verify rejection blocks signal."""
    committee, tracker, _ = _make_committee_and_tracker()
    adapter = CommitteeBacktestAdapter(committee=committee, feedback_tracker=tracker)

    # High-risk scenario: large drawdown, near daily limit, max positions
    signal = {
        "direction": "LONG",
        "price": 3500,
        "strategy": "trend_follower",
        "symbol": "ETH/USDT",
        "risk_pct": 2.0,
        "stop_loss": 3400,
        "take_profit": 3700,
    }
    market_data = {
        "regime": "bear_trend",
        "adx": 10.0,  # weak trend
        "trend_strength": 0.1,
        "ema_spread": -0.005,  # EMA bearish
        "balance": 10000,
        "initial_balance": 10000,
        "drawdown": 0.25,  # 25% drawdown — high
        "daily_loss": 400,  # 80% of daily limit used
        "max_daily_loss": 500,
        "open_positions": 10,  # at max
        "max_positions": 10,
        "exposure_pct": 90,  # very high exposure
    }

    should_exec, decision = await adapter.should_execute(
        signal=signal,
        market_data=market_data,
        current_regime="bear_trend",
    )

    # With this many red flags, risk expert should veto -> REJECT
    assert should_exec is False
    assert decision["verdict"] in (Verdict.REJECT.value, Verdict.DEFER.value)


@pytest.mark.asyncio
async def test_adapter_records_outcome():
    """After should_execute + record_outcome, tracker has scores."""
    committee, tracker, experts = _make_committee_and_tracker()
    adapter = CommitteeBacktestAdapter(committee=committee, feedback_tracker=tracker)

    signal_entry = make_test_signals(1)[0]
    _, decision = await adapter.should_execute(
        signal=signal_entry["signal"],
        market_data=signal_entry["market_data"],
        current_regime="bull_trend",
    )

    # Record a positive outcome
    position_id = f"bt_pos_0"
    # The adapter generates its own position_id, so find it
    # from the session_to_position mapping
    if adapter._session_to_position:
        position_id = list(adapter._session_to_position.values())[0]

    adapter.record_outcome(position_id=position_id, pnl=100.0, regime="bull_trend")

    # At least some experts should now have scores
    has_scores = False
    for expert in experts:
        sc = tracker.get_scorecard(expert.name)
        if sc.total_votes > 0:
            has_scores = True
            break

    assert has_scores, "Tracker should have scored at least one expert after record_outcome"


@pytest.mark.asyncio
async def test_adapter_stats():
    """get_stats() returns correct counts after multiple evaluations."""
    committee, tracker, _ = _make_committee_and_tracker()
    adapter = CommitteeBacktestAdapter(committee=committee, feedback_tracker=tracker)

    signals = make_test_signals(5)
    for entry in signals:
        await adapter.should_execute(
            signal=entry["signal"],
            market_data=entry["market_data"],
            current_regime=entry["regime"],
        )

    stats = adapter.get_stats()
    assert stats["total_evaluated"] == 5
    assert stats["approved"] + stats["rejected"] + stats["deferred"] == 5
    assert 0.0 <= stats["filter_rate"] <= 1.0


# ── Engine Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_run_paired():
    """Run 20 signals, get CommitteeBacktestResult with all fields."""
    engine = CommitteeBacktestEngine()
    signals = make_test_signals(20)

    result = await engine.run_paired(signals)

    assert isinstance(result, CommitteeBacktestResult)
    assert result.baseline_trades == 20
    assert result.committee_trades >= 0
    assert result.committee_approved >= 0
    assert result.committee_rejected >= 0
    assert result.committee_deferred >= 0
    assert (
        result.committee_approved + result.committee_rejected + result.committee_deferred
        == 20
    )

    # to_dict should produce a clean dict
    d = result.to_dict()
    assert "baseline_pnl" in d
    assert "committee_pnl" in d
    assert "pnl_delta" in d
    assert "filter_rate" in d


@pytest.mark.asyncio
async def test_engine_result_has_trades():
    """baseline_trades == total signals, committee_trades <= total."""
    engine = CommitteeBacktestEngine()
    signals = make_test_signals(15)

    result = await engine.run_paired(signals)

    assert result.baseline_trades == len(signals)
    assert result.committee_trades <= result.baseline_trades


@pytest.mark.asyncio
async def test_engine_filter_rate():
    """filter_rate between 0 and 1."""
    engine = CommitteeBacktestEngine()
    signals = make_test_signals(20)

    result = await engine.run_paired(signals)

    assert 0.0 <= result.filter_rate <= 1.0
    # filter_rate should match (rejected + deferred) / total
    total = result.committee_approved + result.committee_rejected + result.committee_deferred
    expected_filter = (result.committee_rejected + result.committee_deferred) / total if total else 0
    assert abs(result.filter_rate - expected_filter) < 1e-6


@pytest.mark.asyncio
async def test_engine_expert_accuracy():
    """per_expert_accuracy has 4 experts with float values."""
    engine = CommitteeBacktestEngine()
    signals = make_test_signals(20)

    result = await engine.run_paired(signals)

    assert len(result.per_expert_accuracy) == 4
    expected_names = {"smc_structuralist", "trend_analyst", "risk_manager", "contrarian"}
    assert set(result.per_expert_accuracy.keys()) == expected_names

    for name, accuracy in result.per_expert_accuracy.items():
        assert isinstance(accuracy, float), f"{name} accuracy should be float"
        assert 0.0 <= accuracy <= 1.0, f"{name} accuracy {accuracy} out of [0,1]"
