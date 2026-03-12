from __future__ import annotations

import pytest

from bot.agents.meta.clustering import (
    AdaptiveLearningRate,
    TradeCluster,
    TradeClustering,
    TradeFeatures,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(
    trade_id: str = "t1",
    regime: str = "bull",
    strategy: str = "grid",
    direction: str = "long",
    hour_bucket: str = "asia",
    volatility_bucket: str = "normal",
    pnl: float = 10.0,
    win: bool = True,
    holding_bars: int = 5,
) -> TradeFeatures:
    return TradeFeatures(
        trade_id=trade_id,
        regime=regime,
        strategy=strategy,
        direction=direction,
        hour_bucket=hour_bucket,
        volatility_bucket=volatility_bucket,
        pnl=pnl,
        win=win,
        holding_bars=holding_bars,
    )


# ===========================================================================
# TradeFeatures tests
# ===========================================================================

class TestTradeFeatures:
    def test_to_dict(self):
        t = _make_trade()
        d = t.to_dict()
        assert d["trade_id"] == "t1"
        assert d["regime"] == "bull"
        assert d["win"] is True
        assert set(d.keys()) == {
            "trade_id", "regime", "strategy", "direction",
            "hour_bucket", "volatility_bucket", "pnl", "win", "holding_bars",
        }


# ===========================================================================
# TradeCluster tests
# ===========================================================================

class TestTradeCluster:
    def test_to_dict(self):
        t = _make_trade()
        c = TradeCluster(
            cluster_id="c0",
            label="grid_long_bull",
            trades=[t],
            size=1,
            win_rate=1.0,
            avg_pnl=10.0,
            total_pnl=10.0,
            dominant_regime="bull",
            dominant_strategy="grid",
            dominant_direction="long",
        )
        d = c.to_dict()
        assert d["cluster_id"] == "c0"
        assert "trades" not in d  # trades excluded from dict


# ===========================================================================
# TradeClustering tests
# ===========================================================================

class TestTradeClustering:

    def test_empty_trades(self):
        tc = TradeClustering()
        clusters = tc.cluster()
        assert clusters == []
        assert tc.find_biases() == []
        stats = tc.get_stats()
        assert stats["total_trades"] == 0

    def test_single_trade(self):
        tc = TradeClustering()
        tc.add_trade(_make_trade())
        clusters = tc.cluster()
        assert len(clusters) == 1
        assert clusters[0].size == 1
        assert clusters[0].win_rate == 1.0
        assert clusters[0].label == "grid_long_bull"

    def test_cluster_by_features(self):
        tc = TradeClustering()
        # 3 trades in same group
        for i in range(3):
            tc.add_trade(_make_trade(trade_id=f"t{i}", pnl=10.0, win=True))
        # 2 trades in different group
        for i in range(2):
            tc.add_trade(_make_trade(
                trade_id=f"s{i}", regime="bear", strategy="smc",
                direction="short", pnl=-5.0, win=False,
            ))
        clusters = tc.cluster()
        assert len(clusters) == 2
        labels = {c.label for c in clusters}
        assert "grid_long_bull" in labels
        assert "smc_short_bear" in labels

    def test_cluster_stats(self):
        tc = TradeClustering()
        tc.add_trade(_make_trade(trade_id="t1", pnl=20.0, win=True))
        tc.add_trade(_make_trade(trade_id="t2", pnl=-10.0, win=False))
        clusters = tc.cluster()
        assert len(clusters) == 1
        c = clusters[0]
        assert c.size == 2
        assert c.win_rate == 0.5
        assert c.total_pnl == pytest.approx(10.0)
        assert c.avg_pnl == pytest.approx(5.0)

    def test_multiple_strategies(self):
        tc = TradeClustering()
        for s in ["grid", "smc", "tf"]:
            tc.add_trade(_make_trade(trade_id=f"{s}_1", strategy=s))
        clusters = tc.cluster()
        assert len(clusters) == 3

    def test_find_bias_systematic_loser(self):
        tc = TradeClustering()
        # 6 trades, 1 win (16.7% win rate)
        for i in range(6):
            tc.add_trade(_make_trade(
                trade_id=f"t{i}", pnl=-10.0, win=(i == 0),
            ))
        biases = tc.find_biases()
        loser_biases = [b for b in biases if b["bias_type"] == "systematic_loser"]
        assert len(loser_biases) == 1
        assert loser_biases[0]["details"]["win_rate"] == pytest.approx(1 / 6)

    def test_find_bias_strong_edge(self):
        tc = TradeClustering()
        # 5 trades, all wins (100% win rate)
        for i in range(5):
            tc.add_trade(_make_trade(trade_id=f"t{i}", pnl=15.0, win=True))
        biases = tc.find_biases()
        edge_biases = [b for b in biases if b["bias_type"] == "strong_edge"]
        assert len(edge_biases) == 1
        assert edge_biases[0]["details"]["win_rate"] == 1.0

    def test_find_bias_directional_long(self):
        tc = TradeClustering()
        # 9 long, 1 short = 90% long
        for i in range(9):
            tc.add_trade(_make_trade(trade_id=f"l{i}", direction="long"))
        tc.add_trade(_make_trade(trade_id="s0", direction="short"))
        biases = tc.find_biases()
        dir_biases = [b for b in biases if b["bias_type"] == "directional_bias"]
        assert len(dir_biases) == 1
        assert dir_biases[0]["details"]["direction"] == "long"

    def test_find_bias_directional_short(self):
        tc = TradeClustering()
        for i in range(9):
            tc.add_trade(_make_trade(trade_id=f"s{i}", direction="short"))
        tc.add_trade(_make_trade(trade_id="l0", direction="long"))
        biases = tc.find_biases()
        dir_biases = [b for b in biases if b["bias_type"] == "directional_bias"]
        assert len(dir_biases) == 1
        assert dir_biases[0]["details"]["direction"] == "short"

    def test_no_bias_when_small_cluster(self):
        tc = TradeClustering()
        # 4 losing trades (below size threshold of 5)
        for i in range(4):
            tc.add_trade(_make_trade(trade_id=f"t{i}", pnl=-10.0, win=False))
        biases = tc.find_biases()
        cluster_biases = [b for b in biases if b["bias_type"] in ("systematic_loser", "strong_edge")]
        assert len(cluster_biases) == 0

    def test_regime_breakdown(self):
        tc = TradeClustering()
        tc.add_trade(_make_trade(trade_id="t1", regime="bull", strategy="grid", pnl=20.0, win=True))
        tc.add_trade(_make_trade(trade_id="t2", regime="bull", strategy="smc", pnl=-5.0, win=False))
        tc.add_trade(_make_trade(trade_id="t3", regime="bear", strategy="grid", pnl=-15.0, win=False))
        breakdown = tc.get_regime_breakdown()
        assert "bull" in breakdown
        assert "bear" in breakdown
        assert breakdown["bull"]["trade_count"] == 2
        assert breakdown["bull"]["win_rate"] == 0.5
        assert breakdown["bull"]["best_strategy"] == "grid"
        assert breakdown["bull"]["worst_strategy"] == "smc"
        assert breakdown["bear"]["trade_count"] == 1

    def test_time_breakdown(self):
        tc = TradeClustering()
        tc.add_trade(_make_trade(trade_id="t1", hour_bucket="asia", pnl=10.0, win=True))
        tc.add_trade(_make_trade(trade_id="t2", hour_bucket="asia", pnl=-5.0, win=False))
        tc.add_trade(_make_trade(trade_id="t3", hour_bucket="us", pnl=20.0, win=True))
        breakdown = tc.get_time_breakdown()
        assert breakdown["asia"]["trade_count"] == 2
        assert breakdown["asia"]["win_rate"] == 0.5
        assert breakdown["us"]["trade_count"] == 1
        assert breakdown["us"]["win_rate"] == 1.0

    def test_get_stats(self):
        tc = TradeClustering()
        for i in range(3):
            tc.add_trade(_make_trade(trade_id=f"t{i}", pnl=10.0))
        stats = tc.get_stats()
        assert stats["total_trades"] == 3
        assert stats["overall_win_rate"] == 1.0
        assert "regime_breakdown" in stats
        assert "time_breakdown" in stats


# ===========================================================================
# AdaptiveLearningRate tests
# ===========================================================================

class TestAdaptiveLearningRate:

    def test_initial_rate(self):
        alr = AdaptiveLearningRate(initial_rate=0.1)
        assert alr.get_rate("agent_a") == pytest.approx(0.1)

    def test_decay_on_correct(self):
        alr = AdaptiveLearningRate(initial_rate=0.1, decay=0.9)
        alr.step("agent_a", was_correct=True)
        assert alr.get_rate("agent_a") == pytest.approx(0.1 * 0.9)

    def test_increase_on_wrong(self):
        alr = AdaptiveLearningRate(initial_rate=0.1)
        alr.step("agent_a", was_correct=False)
        assert alr.get_rate("agent_a") == pytest.approx(0.1 * 1.1)

    def test_clamp_min(self):
        alr = AdaptiveLearningRate(initial_rate=0.02, min_rate=0.01, decay=0.1)
        # Aggressive decay should hit min
        for _ in range(20):
            alr.step("agent_a", was_correct=True)
        assert alr.get_rate("agent_a") == pytest.approx(0.01)

    def test_clamp_max(self):
        alr = AdaptiveLearningRate(initial_rate=0.4, max_rate=0.5)
        # Repeated wrong should hit max
        for _ in range(20):
            alr.step("agent_a", was_correct=False)
        assert alr.get_rate("agent_a") == pytest.approx(0.5)

    def test_regime_change_boost(self):
        alr = AdaptiveLearningRate(initial_rate=0.1, boost_on_regime_change=2.0, max_rate=0.5)
        alr.get_rate("agent_a")  # init
        alr.on_regime_change("bear")
        assert alr.get_rate("agent_a") == pytest.approx(0.2)

    def test_regime_change_boost_clamped(self):
        alr = AdaptiveLearningRate(initial_rate=0.3, boost_on_regime_change=3.0, max_rate=0.5)
        alr.get_rate("agent_a")  # init
        alr.on_regime_change("bear")
        # 0.3 * 3.0 = 0.9 -> clamped to 0.5
        assert alr.get_rate("agent_a") == pytest.approx(0.5)

    def test_stable_regime_decay(self):
        alr = AdaptiveLearningRate(initial_rate=0.1, decay=0.9)
        alr.get_rate("agent_a")  # init
        alr.on_stable_regime()
        expected = 0.1 * (0.9 ** 5)
        assert alr.get_rate("agent_a") == pytest.approx(expected)

    def test_reset_single_agent(self):
        alr = AdaptiveLearningRate(initial_rate=0.1)
        alr.step("agent_a", was_correct=False)
        alr.step("agent_b", was_correct=False)
        alr.reset("agent_a")
        assert alr.get_rate("agent_a") == pytest.approx(0.1)
        assert alr.get_rate("agent_b") != pytest.approx(0.1)

    def test_reset_all_agents(self):
        alr = AdaptiveLearningRate(initial_rate=0.1)
        alr.step("agent_a", was_correct=False)
        alr.step("agent_b", was_correct=True)
        alr.reset()
        assert alr.get_rate("agent_a") == pytest.approx(0.1)
        assert alr.get_rate("agent_b") == pytest.approx(0.1)

    def test_multiple_agents_independent(self):
        alr = AdaptiveLearningRate(initial_rate=0.1, decay=0.9)
        alr.step("agent_a", was_correct=True)
        alr.step("agent_b", was_correct=False)
        assert alr.get_rate("agent_a") == pytest.approx(0.1 * 0.9)
        assert alr.get_rate("agent_b") == pytest.approx(0.1 * 1.1)

    def test_get_all_rates(self):
        alr = AdaptiveLearningRate(initial_rate=0.1)
        alr.get_rate("agent_a")
        alr.get_rate("agent_b")
        rates = alr.get_all_rates()
        assert set(rates.keys()) == {"agent_a", "agent_b"}
        assert all(v == pytest.approx(0.1) for v in rates.values())

    def test_get_stats(self):
        alr = AdaptiveLearningRate(initial_rate=0.1, decay=0.9)
        alr.step("agent_a", was_correct=True)
        alr.on_regime_change("bull")
        stats = alr.get_stats()
        assert stats["current_regime"] == "bull"
        assert stats["initial_rate"] == 0.1
        assert "agent_a" in stats["agents"]
        assert stats["agents"]["agent_a"]["steps"] == 1
