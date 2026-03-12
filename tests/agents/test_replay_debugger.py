"""Tests for EventReplayDebugger — counterfactual replay of historical signals."""

import pytest

from bot.agents.replay_debugger import (
    EventReplayDebugger,
    ReplayResult,
    ReplayTradeResult,
)
from bot.agents.committee import InvestmentCommittee
from bot.agents.smc_expert import SMCExpert
from bot.agents.trend_expert import TrendExpert
from bot.agents.risk_expert import RiskExpert
from bot.agents.contrarian_expert import ContrarianExpert
from bot.core.event_bus import EventBus


def make_historical_trades(n=30):
    trades = []
    for i in range(n):
        is_winner = i % 3 != 0  # 2/3 winners
        regime = "bull_trend" if i % 2 == 0 else "bear_trend"
        trades.append(
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
                    "regime": regime,
                    "adx": 35.0 if "bull" in regime else 15.0,
                    "trend_strength": 0.7,
                    "ema_spread": 0.003,
                    "balance": 10000,
                    "initial_balance": 10000,
                    "drawdown": 0.02 if is_winner else 0.15,
                    "daily_loss": 50,
                    "max_daily_loss": 500,
                    "open_positions": 2,
                    "max_positions": 10,
                    "exposure_pct": 30,
                },
                "regime": regime,
                "pnl": 150.0 if is_winner else -100.0,
                "position_id": f"pos_{i}",
            }
        )
    return trades


def _make_committee():
    committee = InvestmentCommittee()
    committee.add_expert(SMCExpert())
    committee.add_expert(TrendExpert())
    committee.add_expert(RiskExpert())
    committee.add_expert(ContrarianExpert())
    return committee


@pytest.mark.asyncio
async def test_replay_basic():
    """Replay 30 trades, ReplayResult has all required fields."""
    debugger = EventReplayDebugger(committee=_make_committee())
    trades = make_historical_trades(30)
    result = await debugger.replay(trades)

    assert isinstance(result, ReplayResult)
    assert result.total_signals == 30
    assert result.approved + result.rejected + result.deferred == 30
    assert result.actual_winners + result.actual_losers == 30
    assert len(result.trades) == 30
    assert result.actual_win_rate >= 0.0
    assert result.filter_rate >= 0.0


@pytest.mark.asyncio
async def test_replay_filters_some_trades():
    """Committee should filter at least some trades (not approve all)."""
    debugger = EventReplayDebugger(committee=_make_committee())
    trades = make_historical_trades(30)
    result = await debugger.replay(trades)

    # With mixed bull/bear regimes and varying drawdown,
    # the committee should not approve every single trade
    approved_trades = sum(1 for t in result.trades if t.would_have_traded)
    assert approved_trades < result.total_signals or result.rejected > 0 or result.deferred > 0


@pytest.mark.asyncio
async def test_replay_pnl_delta():
    """Filtered PnL should differ from actual PnL when some trades are filtered."""
    debugger = EventReplayDebugger(committee=_make_committee())
    trades = make_historical_trades(30)
    result = await debugger.replay(trades)

    # If any trades were rejected/deferred, filtered_pnl != actual_pnl
    if result.rejected > 0 or result.deferred > 0:
        assert result.filtered_total_pnl != result.actual_total_pnl
    # pnl_delta should equal filtered - actual
    assert abs(result.pnl_delta - (result.filtered_total_pnl - result.actual_total_pnl)) < 0.01


@pytest.mark.asyncio
async def test_replay_avoided_losses():
    """Should correctly compute avoided_losses from rejected losing trades."""
    debugger = EventReplayDebugger(committee=_make_committee())
    trades = make_historical_trades(30)
    result = await debugger.replay(trades)

    # Manually check: avoided_losses = abs(sum of negative PnL on rejected trades)
    rejected = [t for t in result.trades if not t.would_have_traded]
    expected_avoided = abs(sum(t.actual_pnl for t in rejected if t.actual_pnl < 0))
    assert abs(result.avoided_losses - expected_avoided) < 0.01

    # avoided_losses should be non-negative
    assert result.avoided_losses >= 0.0


@pytest.mark.asyncio
async def test_replay_expert_accuracy():
    """Per-expert accuracy dict should have entries for all 4 experts."""
    debugger = EventReplayDebugger(committee=_make_committee())
    trades = make_historical_trades(30)
    result = await debugger.replay(trades)

    assert len(result.expert_accuracy) == 4
    expected_names = {"smc_structuralist", "trend_analyst", "risk_manager", "contrarian"}
    assert set(result.expert_accuracy.keys()) == expected_names

    for name, acc in result.expert_accuracy.items():
        assert 0.0 <= acc <= 1.0, f"Expert {name} accuracy {acc} out of range"


@pytest.mark.asyncio
async def test_replay_win_rate():
    """Filtered win_rate should be correctly computed from approved trades only."""
    debugger = EventReplayDebugger(committee=_make_committee())
    trades = make_historical_trades(30)
    result = await debugger.replay(trades)

    approved_trades = [t for t in result.trades if t.would_have_traded]
    if approved_trades:
        expected_winners = sum(1 for t in approved_trades if t.filtered_pnl > 0)
        expected_rate = expected_winners / len(approved_trades)
        assert abs(result.filtered_win_rate - expected_rate) < 0.01
    else:
        assert result.filtered_win_rate == 0.0


@pytest.mark.asyncio
async def test_replay_with_weight_sweep():
    """Weight sweep should return list of (config, result) sorted by PnL descending."""
    debugger = EventReplayDebugger(committee=_make_committee())
    trades = make_historical_trades(15)  # fewer for speed
    results = await debugger.replay_with_weight_sweep(trades)

    assert isinstance(results, list)
    assert len(results) == 5  # default generates 5 configs

    for config, result in results:
        assert isinstance(config, dict)
        assert isinstance(result, ReplayResult)

    # Check sorted descending by filtered_total_pnl
    pnls = [r.filtered_total_pnl for _, r in results]
    assert pnls == sorted(pnls, reverse=True)


@pytest.mark.asyncio
async def test_replay_result_to_dict():
    """ReplayResult and ReplayTradeResult serialize to dict correctly."""
    debugger = EventReplayDebugger(committee=_make_committee())
    trades = make_historical_trades(5)
    result = await debugger.replay(trades)

    d = result.to_dict()
    assert isinstance(d, dict)
    assert "total_signals" in d
    assert "approved" in d
    assert "rejected" in d
    assert "deferred" in d
    assert "actual_total_pnl" in d
    assert "filtered_total_pnl" in d
    assert "pnl_delta" in d
    assert "avoided_losses" in d
    assert "missed_profits" in d
    assert "filter_rate" in d
    assert "expert_accuracy" in d
    assert "trades" in d
    assert isinstance(d["trades"], list)

    if d["trades"]:
        trade_d = d["trades"][0]
        assert "position_id" in trade_d
        assert "committee_verdict" in trade_d
        assert "would_have_traded" in trade_d
        assert "filtered_pnl" in trade_d


@pytest.mark.asyncio
async def test_replay_progress_callback():
    """Progress callback should be called for each trade."""
    debugger = EventReplayDebugger(committee=_make_committee())
    trades = make_historical_trades(10)

    callback_calls = []

    def on_progress(pct, done, total):
        callback_calls.append((pct, done, total))

    await debugger.replay(trades, progress_callback=on_progress)

    assert len(callback_calls) == 10
    # Last call should be 100%
    assert callback_calls[-1][0] == pytest.approx(1.0)
    assert callback_calls[-1][1] == 10
    assert callback_calls[-1][2] == 10


@pytest.mark.asyncio
async def test_replay_empty_trades():
    """Empty trade list should return zeros across all metrics."""
    debugger = EventReplayDebugger(committee=_make_committee())
    result = await debugger.replay([])

    assert result.total_signals == 0
    assert result.approved == 0
    assert result.rejected == 0
    assert result.deferred == 0
    assert result.actual_total_pnl == 0.0
    assert result.actual_winners == 0
    assert result.actual_losers == 0
    assert result.actual_win_rate == 0.0
    assert result.filtered_total_pnl == 0.0
    assert result.filtered_winners == 0
    assert result.filtered_losers == 0
    assert result.filtered_win_rate == 0.0
    assert result.pnl_delta == 0.0
    assert result.avoided_losses == 0.0
    assert result.missed_profits == 0.0
    assert result.filter_rate == 0.0
    assert result.expert_accuracy == {}
    assert result.trades == []
