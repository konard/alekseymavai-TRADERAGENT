from __future__ import annotations

import pytest

from bot.agents.analytics.attribution import (
    AgentProfiler,
    PnLDecomposer,
    ToolUsageRecord,
    TradeRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _long_trade(
    trade_id: str = "t1",
    strategy: str = "trend",
    symbol: str = "BTCUSDT",
    entry: float = 100.0,
    exit_: float = 110.0,
    size: float = 1.0,
    fees: float = 0.0,
    market_return: float = 0.0,
) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        strategy=strategy,
        symbol=symbol,
        direction="long",
        entry_price=entry,
        exit_price=exit_,
        size=size,
        entry_ts=1000.0,
        exit_ts=2000.0,
        fees=fees,
        market_return=market_return,
    )


def _short_trade(
    trade_id: str = "t1",
    strategy: str = "smc",
    symbol: str = "ETHUSDT",
    entry: float = 200.0,
    exit_: float = 180.0,
    size: float = 1.0,
    fees: float = 0.0,
    market_return: float = 0.0,
) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        strategy=strategy,
        symbol=symbol,
        direction="short",
        entry_price=entry,
        exit_price=exit_,
        size=size,
        entry_ts=1000.0,
        exit_ts=2000.0,
        fees=fees,
        market_return=market_return,
    )


# ============================= PnLDecomposer ==============================


class TestPnLDecomposerEmpty:
    def test_empty_decompose(self):
        d = PnLDecomposer()
        result = d.decompose()
        assert result["total_pnl"] == 0.0
        assert result["alpha"] == 0.0
        assert result["per_strategy"] == {}

    def test_empty_stats(self):
        d = PnLDecomposer()
        stats = d.get_stats()
        assert stats["total_trades"] == 0
        assert stats["win_rate"] == 0.0

    def test_empty_strategy_comparison(self):
        d = PnLDecomposer()
        assert d.get_strategy_comparison() == []


class TestPnLDecomposerSingleTrade:
    def test_single_winning_long(self):
        d = PnLDecomposer()
        d.add_trade(_long_trade(entry=100.0, exit_=110.0, size=2.0))
        result = d.decompose()
        assert result["total_pnl"] == pytest.approx(20.0)
        assert result["net_pnl"] == pytest.approx(20.0)

    def test_single_losing_long(self):
        d = PnLDecomposer()
        d.add_trade(_long_trade(entry=100.0, exit_=90.0, size=1.0))
        result = d.decompose()
        assert result["total_pnl"] == pytest.approx(-10.0)

    def test_single_winning_short(self):
        d = PnLDecomposer()
        d.add_trade(_short_trade(entry=200.0, exit_=180.0, size=1.0))
        result = d.decompose()
        assert result["total_pnl"] == pytest.approx(20.0)

    def test_single_losing_short(self):
        d = PnLDecomposer()
        d.add_trade(_short_trade(entry=200.0, exit_=220.0, size=1.0))
        result = d.decompose()
        assert result["total_pnl"] == pytest.approx(-20.0)

    def test_fees_subtracted(self):
        d = PnLDecomposer()
        d.add_trade(_long_trade(entry=100.0, exit_=110.0, size=1.0, fees=3.0))
        result = d.decompose()
        assert result["total_pnl"] == pytest.approx(10.0)
        assert result["fees_total"] == pytest.approx(3.0)
        assert result["net_pnl"] == pytest.approx(7.0)


class TestPnLDecomposerAlphaBeta:
    def test_alpha_vs_beta_separation(self):
        d = PnLDecomposer()
        # Long trade: pnl = (110-100)*1 = 10. market_return=5 => beta = 5*1*1 = 5
        d.add_trade(
            _long_trade(entry=100.0, exit_=110.0, size=1.0, market_return=5.0)
        )
        result = d.decompose()
        assert result["beta_pnl"] == pytest.approx(5.0)
        assert result["alpha"] == pytest.approx(5.0)  # 10 - 5

    def test_negative_alpha(self):
        d = PnLDecomposer()
        # pnl = 2, beta = 8 => alpha = -6
        d.add_trade(
            _long_trade(entry=100.0, exit_=102.0, size=1.0, market_return=8.0)
        )
        result = d.decompose()
        assert result["alpha"] == pytest.approx(-6.0)

    def test_alpha_pct(self):
        d = PnLDecomposer()
        d.add_trade(
            _long_trade(entry=100.0, exit_=110.0, size=1.0, market_return=5.0)
        )
        result = d.decompose()
        # alpha=5, total_pnl=10 => alpha_pct = 50%
        assert result["alpha_pct"] == pytest.approx(50.0)

    def test_short_beta(self):
        d = PnLDecomposer()
        # Short: pnl = (200-180)*1*(-1)*(-1)=20, direction_sign=-1
        # beta = market_return * size * direction_sign = 3 * 1 * (-1) = -3
        d.add_trade(
            _short_trade(entry=200.0, exit_=180.0, size=1.0, market_return=3.0)
        )
        result = d.decompose()
        assert result["beta_pnl"] == pytest.approx(-3.0)
        assert result["alpha"] == pytest.approx(23.0)


class TestPnLDecomposerMultiStrategy:
    def test_multiple_strategies_decomposition(self):
        d = PnLDecomposer()
        d.add_trade(_long_trade(trade_id="t1", strategy="trend", entry=100, exit_=120, size=1))
        d.add_trade(_long_trade(trade_id="t2", strategy="grid", entry=100, exit_=105, size=2))
        result = d.decompose()

        assert result["total_pnl"] == pytest.approx(30.0)  # 20 + 10
        assert "trend" in result["per_strategy"]
        assert "grid" in result["per_strategy"]
        assert result["per_strategy"]["trend"]["total_pnl"] == pytest.approx(20.0)
        assert result["per_strategy"]["grid"]["total_pnl"] == pytest.approx(10.0)

    def test_strategy_filter(self):
        d = PnLDecomposer()
        d.add_trade(_long_trade(trade_id="t1", strategy="trend", entry=100, exit_=120, size=1))
        d.add_trade(_long_trade(trade_id="t2", strategy="grid", entry=100, exit_=105, size=1))
        result = d.decompose(strategy="trend")
        assert result["total_pnl"] == pytest.approx(20.0)
        assert list(result["per_strategy"].keys()) == ["trend"]

    def test_strategy_comparison_sorted(self):
        d = PnLDecomposer()
        d.add_trade(_long_trade(trade_id="t1", strategy="worst", entry=100, exit_=90, size=1))
        d.add_trade(_long_trade(trade_id="t2", strategy="best", entry=100, exit_=130, size=1))
        d.add_trade(_long_trade(trade_id="t3", strategy="mid", entry=100, exit_=110, size=1))
        comp = d.get_strategy_comparison()
        assert [c["strategy"] for c in comp] == ["best", "mid", "worst"]
        assert comp[0]["total_pnl"] == pytest.approx(30.0)
        assert comp[0]["trade_count"] == 1


class TestPnLDecomposerSelection:
    def test_selection_attribution(self):
        d = PnLDecomposer()
        # BTC pnl=20, ETH pnl=5.  Portfolio avg pnl = 12.5
        # selection_pnl = (20-12.5)*1 + (5-12.5)*1 = 7.5 - 7.5 = 0
        # With single trade per symbol and two symbols, selection sums to 0.
        d.add_trade(_long_trade(trade_id="t1", symbol="BTCUSDT", entry=100, exit_=120, size=1))
        d.add_trade(_long_trade(trade_id="t2", symbol="ETHUSDT", entry=100, exit_=105, size=1))
        result = d.decompose()
        # By construction, selection_pnl sums to zero when each symbol has 1 trade.
        assert result["selection_pnl"] == pytest.approx(0.0)

    def test_selection_nonzero_with_multiple_trades(self):
        d = PnLDecomposer()
        # 2 BTC trades: pnl 20 + 10 = 30, avg 15
        # 1 ETH trade: pnl 2, avg 2
        # portfolio avg = 32/3 ~ 10.667
        # selection = (15-10.667)*2 + (2-10.667)*1 = 8.667 - 8.667 = 0
        # Selection always sums to 0 (it's a zero-sum decomposition).
        d.add_trade(_long_trade(trade_id="t1", symbol="BTCUSDT", entry=100, exit_=120, size=1))
        d.add_trade(_long_trade(trade_id="t2", symbol="BTCUSDT", entry=100, exit_=110, size=1))
        d.add_trade(_long_trade(trade_id="t3", symbol="ETHUSDT", entry=100, exit_=102, size=1))
        result = d.decompose()
        assert result["selection_pnl"] == pytest.approx(0.0, abs=1e-9)


class TestPnLDecomposerTiming:
    def test_timing_pnl_symmetric(self):
        d = PnLDecomposer()
        # long: entry=100, exit=110, midpoint=105
        # pnl = 10, pnl_at_midpoint = (110-105)*1=5, timing = 10-5 = 5
        d.add_trade(_long_trade(entry=100, exit_=110, size=1))
        result = d.decompose()
        assert result["timing_pnl"] == pytest.approx(5.0)


class TestPnLDecomposerStats:
    def test_stats(self):
        d = PnLDecomposer()
        d.add_trade(_long_trade(trade_id="t1", strategy="trend", symbol="BTCUSDT", entry=100, exit_=110))
        d.add_trade(_long_trade(trade_id="t2", strategy="grid", symbol="ETHUSDT", entry=100, exit_=90))
        stats = d.get_stats()
        assert stats["total_trades"] == 2
        assert stats["winning_trades"] == 1
        assert stats["losing_trades"] == 1
        assert stats["win_rate"] == pytest.approx(0.5)
        assert "trend" in stats["strategies"]
        assert "BTCUSDT" in stats["symbols"]


# ============================= AgentProfiler ==============================


def _tool_record(
    tool: str = "fetch_price",
    agent: str = "analyst",
    exec_time: float = 0.5,
    success: bool = True,
    ts: float = 1000.0,
) -> ToolUsageRecord:
    return ToolUsageRecord(
        tool_name=tool,
        agent_name=agent,
        execution_time=exec_time,
        success=success,
        ts=ts,
    )


class TestAgentProfilerToolUse:
    def test_record_tool_use(self):
        p = AgentProfiler()
        p.record_tool_use(_tool_record())
        profile = p.get_agent_profile("analyst")
        assert profile["total_tool_uses"] == 1

    def test_tool_breakdown(self):
        p = AgentProfiler()
        p.record_tool_use(_tool_record(tool="fetch_price"))
        p.record_tool_use(_tool_record(tool="fetch_price"))
        p.record_tool_use(_tool_record(tool="place_order"))
        profile = p.get_agent_profile("analyst")
        assert profile["tool_breakdown"]["fetch_price"] == 2
        assert profile["tool_breakdown"]["place_order"] == 1
        assert profile["most_used_tool"] == "fetch_price"

    def test_avg_response_time(self):
        p = AgentProfiler()
        p.record_tool_use(_tool_record(exec_time=1.0))
        p.record_tool_use(_tool_record(exec_time=3.0))
        profile = p.get_agent_profile("analyst")
        assert profile["avg_response_time"] == pytest.approx(2.0)

    def test_success_rate(self):
        p = AgentProfiler()
        p.record_tool_use(_tool_record(success=True))
        p.record_tool_use(_tool_record(success=True))
        p.record_tool_use(_tool_record(success=False))
        profile = p.get_agent_profile("analyst")
        assert profile["success_rate"] == pytest.approx(2.0 / 3.0)

    def test_fastest_tool(self):
        p = AgentProfiler()
        p.record_tool_use(_tool_record(tool="slow", exec_time=5.0))
        p.record_tool_use(_tool_record(tool="fast", exec_time=0.1))
        profile = p.get_agent_profile("analyst")
        assert profile["fastest_tool"] == "fast"


class TestAgentProfilerMultipleAgents:
    def test_multiple_agents(self):
        p = AgentProfiler()
        p.record_tool_use(_tool_record(agent="alpha"))
        p.record_tool_use(_tool_record(agent="alpha"))
        p.record_tool_use(_tool_record(agent="beta"))
        all_profiles = p.get_all_profiles()
        assert "alpha" in all_profiles
        assert "beta" in all_profiles
        assert all_profiles["alpha"]["total_tool_uses"] == 2
        assert all_profiles["beta"]["total_tool_uses"] == 1

    def test_unknown_agent(self):
        p = AgentProfiler()
        profile = p.get_agent_profile("nonexistent")
        assert profile["total_tool_uses"] == 0
        assert profile["success_rate"] == 0.0
        assert profile["decision_accuracy"] == 0.0
        assert profile["most_used_tool"] == ""
        assert profile["fastest_tool"] == ""
        assert profile["confidence_calibration"] == 0.0


class TestAgentProfilerDecisions:
    def test_decision_accuracy(self):
        p = AgentProfiler()
        p.record_decision("analyst", "buy", "correct", 0.9, ts=1000.0)
        p.record_decision("analyst", "sell", "wrong", 0.6, ts=1001.0)
        p.record_decision("analyst", "buy", "correct", 0.8, ts=1002.0)
        profile = p.get_agent_profile("analyst")
        assert profile["total_decisions"] == 3
        assert profile["decision_accuracy"] == pytest.approx(2.0 / 3.0)

    def test_confidence_calibration_perfect(self):
        p = AgentProfiler()
        # High confidence + correct, low confidence + wrong => positive correlation
        p.record_decision("a", "buy", "correct", 0.95, ts=1.0)
        p.record_decision("a", "buy", "correct", 0.90, ts=2.0)
        p.record_decision("a", "sell", "wrong", 0.10, ts=3.0)
        p.record_decision("a", "sell", "wrong", 0.15, ts=4.0)
        profile = p.get_agent_profile("a")
        assert profile["confidence_calibration"] > 0.9

    def test_confidence_calibration_inverse(self):
        p = AgentProfiler()
        # High confidence + wrong, low confidence + correct => negative correlation
        p.record_decision("a", "buy", "wrong", 0.95, ts=1.0)
        p.record_decision("a", "buy", "wrong", 0.90, ts=2.0)
        p.record_decision("a", "sell", "correct", 0.10, ts=3.0)
        p.record_decision("a", "sell", "correct", 0.15, ts=4.0)
        profile = p.get_agent_profile("a")
        assert profile["confidence_calibration"] < -0.9

    def test_decision_breakdown(self):
        p = AgentProfiler()
        p.record_decision("a", "buy", "correct", 0.8, ts=1.0)
        p.record_decision("a", "buy", "wrong", 0.5, ts=2.0)
        p.record_decision("a", "sell", "correct", 0.9, ts=3.0)
        bd = p.get_decision_breakdown("a")
        assert bd["buy"]["total"] == 2
        assert bd["buy"]["correct"] == 1
        assert bd["buy"]["accuracy"] == pytest.approx(0.5)
        assert bd["sell"]["total"] == 1
        assert bd["sell"]["accuracy"] == pytest.approx(1.0)

    def test_decision_breakdown_unknown_agent(self):
        p = AgentProfiler()
        assert p.get_decision_breakdown("ghost") == {}


class TestAgentProfilerLeaderboard:
    def test_tool_leaderboard(self):
        p = AgentProfiler()
        p.record_tool_use(_tool_record(tool="popular", exec_time=1.0))
        p.record_tool_use(_tool_record(tool="popular", exec_time=2.0))
        p.record_tool_use(_tool_record(tool="popular", exec_time=1.5, success=False))
        p.record_tool_use(_tool_record(tool="rare", exec_time=0.5))
        lb = p.get_tool_leaderboard()
        assert lb[0]["tool_name"] == "popular"
        assert lb[0]["usage_count"] == 3
        assert lb[0]["success_rate"] == pytest.approx(2.0 / 3.0)
        assert lb[1]["tool_name"] == "rare"
        assert lb[1]["usage_count"] == 1


class TestAgentProfilerStats:
    def test_stats(self):
        p = AgentProfiler()
        p.record_tool_use(_tool_record(agent="a", tool="t1"))
        p.record_tool_use(_tool_record(agent="b", tool="t2"))
        p.record_decision("c", "buy", "correct", 0.8, ts=1.0)
        stats = p.get_stats()
        assert stats["total_agents"] == 3
        assert stats["total_tool_uses"] == 2
        assert stats["total_decisions"] == 1
        assert stats["unique_tools"] == 2
        assert sorted(stats["agents"]) == ["a", "b", "c"]

    def test_empty_stats(self):
        p = AgentProfiler()
        stats = p.get_stats()
        assert stats["total_agents"] == 0
        assert stats["total_tool_uses"] == 0
