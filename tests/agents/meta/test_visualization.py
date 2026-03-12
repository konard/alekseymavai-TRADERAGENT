from __future__ import annotations

import pytest

from bot.agents.meta.visualization import (
    AgentCollaborationGraph,
    CollaborationEdge,
    ExposureCell,
    PortfolioHeatMap,
)


# ======================================================================
# ExposureCell
# ======================================================================


class TestExposureCell:
    def test_to_dict(self):
        cell = ExposureCell(
            symbol="BTCUSDT",
            strategy="grid",
            exposure_usd=5000.0,
            direction="long",
            risk_pct=50.0,
            pnl=120.0,
            intensity=0.5,
        )
        d = cell.to_dict()
        assert d["symbol"] == "BTCUSDT"
        assert d["strategy"] == "grid"
        assert d["exposure_usd"] == 5000.0
        assert d["direction"] == "long"
        assert d["risk_pct"] == 50.0
        assert d["pnl"] == 120.0
        assert d["intensity"] == 0.5


# ======================================================================
# PortfolioHeatMap
# ======================================================================


class TestPortfolioHeatMap:
    def test_add_positions_and_heatmap_structure(self):
        hm = PortfolioHeatMap(portfolio_value=10000.0)
        hm.add_position("BTCUSDT", "grid", 3000.0, "long", pnl=50.0)
        hm.add_position("ETHUSDT", "dca", 2000.0, "long", pnl=-20.0)
        result = hm.get_heatmap()
        assert "cells" in result
        assert "symbols" in result
        assert "strategies" in result
        assert len(result["cells"]) == 2
        assert sorted(result["symbols"]) == ["BTCUSDT", "ETHUSDT"]
        assert sorted(result["strategies"]) == ["dca", "grid"]

    def test_total_exposure(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 3000.0, "long")
        hm.add_position("ETHUSDT", "smc", 2000.0, "short")
        result = hm.get_heatmap()
        assert result["total_exposure"] == 5000.0

    def test_total_risk_pct(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 5000.0, "long")
        result = hm.get_heatmap()
        assert result["total_risk_pct"] == pytest.approx(50.0)

    def test_net_direction_long_heavy(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 5000.0, "long")
        hm.add_position("ETHUSDT", "smc", 1000.0, "short")
        result = hm.get_heatmap()
        assert result["net_direction"] == "long"

    def test_net_direction_short_heavy(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 1000.0, "long")
        hm.add_position("ETHUSDT", "smc", 5000.0, "short")
        result = hm.get_heatmap()
        assert result["net_direction"] == "short"

    def test_net_direction_balanced(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 3000.0, "long")
        hm.add_position("ETHUSDT", "smc", 3000.0, "short")
        result = hm.get_heatmap()
        assert result["net_direction"] == "neutral"

    def test_intensity_clamped_to_one(self):
        hm = PortfolioHeatMap(1000.0)
        hm.add_position("BTCUSDT", "grid", 5000.0, "long")
        result = hm.get_heatmap()
        cell = result["cells"][0]
        assert cell["intensity"] == 1.0

    def test_intensity_normal(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 2500.0, "long")
        result = hm.get_heatmap()
        cell = result["cells"][0]
        assert cell["intensity"] == pytest.approx(0.25)

    def test_remove_position(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 3000.0, "long")
        hm.add_position("ETHUSDT", "dca", 2000.0, "long")
        hm.remove_position("BTCUSDT", "grid")
        result = hm.get_heatmap()
        assert len(result["cells"]) == 1
        assert result["cells"][0]["symbol"] == "ETHUSDT"

    def test_remove_nonexistent_position(self):
        hm = PortfolioHeatMap(10000.0)
        hm.remove_position("BTCUSDT", "grid")  # should not raise

    def test_risk_concentration(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 6000.0, "long")
        hm.add_position("ETHUSDT", "dca", 4000.0, "long")
        conc = hm.get_risk_concentration()
        assert conc["by_symbol"]["BTCUSDT"] == pytest.approx(0.6)
        assert conc["by_symbol"]["ETHUSDT"] == pytest.approx(0.4)
        assert conc["max_concentration_symbol"] == "BTCUSDT"

    def test_herfindahl_index_single_symbol(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 10000.0, "long")
        conc = hm.get_risk_concentration()
        # single symbol: proportion = 1.0, HHI = 1.0
        assert conc["herfindahl_index"] == pytest.approx(1.0)

    def test_herfindahl_index_equal_split(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 5000.0, "long")
        hm.add_position("ETHUSDT", "dca", 5000.0, "long")
        conc = hm.get_risk_concentration()
        # two equal: 0.5^2 + 0.5^2 = 0.5
        assert conc["herfindahl_index"] == pytest.approx(0.5)

    def test_pnl_heatmap_intensity(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 3000.0, "long", pnl=500.0)
        hm.add_position("ETHUSDT", "dca", 2000.0, "short", pnl=-200.0)
        result = hm.get_pnl_heatmap()
        cells_by_sym = {c["symbol"]: c for c in result["cells"]}
        assert cells_by_sym["BTCUSDT"]["intensity"] == pytest.approx(0.05)
        assert cells_by_sym["ETHUSDT"]["intensity"] == pytest.approx(0.02)

    def test_update_portfolio_value(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 5000.0, "long")
        hm.update_portfolio_value(20000.0)
        result = hm.get_heatmap()
        assert result["total_risk_pct"] == pytest.approx(25.0)

    def test_get_stats(self):
        hm = PortfolioHeatMap(10000.0)
        hm.add_position("BTCUSDT", "grid", 3000.0, "long", pnl=100.0)
        hm.add_position("ETHUSDT", "dca", 2000.0, "short", pnl=-50.0)
        stats = hm.get_stats()
        assert stats["portfolio_value"] == 10000.0
        assert stats["position_count"] == 2
        assert stats["total_exposure"] == 5000.0
        assert stats["total_pnl"] == pytest.approx(50.0)
        assert stats["exposure_pct"] == pytest.approx(50.0)

    def test_empty_heatmap(self):
        hm = PortfolioHeatMap(10000.0)
        result = hm.get_heatmap()
        assert result["cells"] == []
        assert result["total_exposure"] == 0.0
        assert result["net_direction"] == "neutral"


# ======================================================================
# CollaborationEdge
# ======================================================================


class TestCollaborationEdge:
    def test_to_dict(self):
        edge = CollaborationEdge(
            agent_a="smc",
            agent_b="trend",
            agreement_count=5,
            disagreement_count=2,
            agreement_rate=5 / 7,
            coalition_count=3,
        )
        d = edge.to_dict()
        assert d["agent_a"] == "smc"
        assert d["agent_b"] == "trend"
        assert d["agreement_count"] == 5
        assert d["coalition_count"] == 3


# ======================================================================
# AgentCollaborationGraph
# ======================================================================


class TestAgentCollaborationGraph:
    def test_record_votes_all_agree(self):
        g = AgentCollaborationGraph()
        g.record_vote("s1", {"smc": "APPROVE", "trend": "APPROVE", "risk": "APPROVE"})
        graph = g.get_graph()
        assert len(graph["edges"]) == 3  # C(3,2)
        for edge in graph["edges"]:
            assert edge["agreement_count"] == 1
            assert edge["disagreement_count"] == 0
            assert edge["agreement_rate"] == pytest.approx(1.0)

    def test_record_votes_split(self):
        g = AgentCollaborationGraph()
        g.record_vote(
            "s1",
            {"smc": "APPROVE", "trend": "APPROVE", "risk": "REJECT", "contrarian": "REJECT"},
        )
        graph = g.get_graph()
        # 6 edges total = C(4,2)
        assert len(graph["edges"]) == 6

    def test_agreement_rate_multi_session(self):
        g = AgentCollaborationGraph()
        g.record_vote("s1", {"a": "APPROVE", "b": "APPROVE"})
        g.record_vote("s2", {"a": "APPROVE", "b": "REJECT"})
        g.record_vote("s3", {"a": "REJECT", "b": "REJECT"})
        # 2 agree, 1 disagree => rate = 2/3
        graph = g.get_graph()
        edge = graph["edges"][0]
        assert edge["agreement_count"] == 2
        assert edge["disagreement_count"] == 1
        assert edge["agreement_rate"] == pytest.approx(2 / 3)

    def test_coalition_recording(self):
        g = AgentCollaborationGraph()
        g.record_coalition(["smc", "trend", "risk"])
        graph = g.get_graph()
        for edge in graph["edges"]:
            assert edge["coalition_count"] == 1

    def test_clusters_high_agreement(self):
        g = AgentCollaborationGraph()
        # a and b always agree, c always disagrees
        for i in range(10):
            g.record_vote(f"s{i}", {"a": "APPROVE", "b": "APPROVE", "c": "REJECT"})
        graph = g.get_graph()
        # a-b have 100% agreement, a-c and b-c have 0%
        # cluster: [a,b] and [c]
        clusters = graph["clusters"]
        assert len(clusters) == 2
        assert ["a", "b"] in clusters
        assert ["c"] in clusters

    def test_strongest_pair(self):
        g = AgentCollaborationGraph()
        g.record_vote("s1", {"a": "APPROVE", "b": "APPROVE", "c": "REJECT"})
        g.record_vote("s2", {"a": "APPROVE", "b": "APPROVE", "c": "APPROVE"})
        result = g.get_strongest_pair()
        assert result is not None
        assert result[0] == "a"
        assert result[1] == "b"
        assert result[2] == pytest.approx(1.0)

    def test_most_contrarian(self):
        g = AgentCollaborationGraph()
        for _ in range(5):
            g.record_vote("s", {"a": "APPROVE", "b": "REJECT", "c": "APPROVE"})
        # a-b: 0% agreement, a-c: 100%
        result = g.get_most_contrarian("a")
        assert result == "b"

    def test_affinity_matrix(self):
        g = AgentCollaborationGraph()
        g.record_vote("s1", {"x": "APPROVE", "y": "APPROVE", "z": "REJECT"})
        matrix = g.get_affinity_matrix()
        assert matrix["x"]["x"] == 1.0
        assert matrix["x"]["y"] == pytest.approx(1.0)
        assert matrix["x"]["z"] == pytest.approx(0.0)
        assert matrix["y"]["z"] == pytest.approx(0.0)

    def test_single_agent(self):
        g = AgentCollaborationGraph()
        g.record_vote("s1", {"solo": "APPROVE"})
        graph = g.get_graph()
        assert len(graph["nodes"]) == 1
        assert graph["nodes"][0]["agent"] == "solo"
        assert len(graph["edges"]) == 0
        assert graph["clusters"] == [["solo"]]

    def test_empty_graph(self):
        g = AgentCollaborationGraph()
        graph = g.get_graph()
        assert graph["nodes"] == []
        assert graph["edges"] == []
        assert graph["clusters"] == []
        assert g.get_strongest_pair() is None

    def test_get_most_contrarian_unknown_agent(self):
        g = AgentCollaborationGraph()
        assert g.get_most_contrarian("unknown") is None

    def test_get_stats(self):
        g = AgentCollaborationGraph()
        g.record_vote("s1", {"a": "APPROVE", "b": "APPROVE", "c": "REJECT"})
        g.record_coalition(["a", "b"])
        stats = g.get_stats()
        assert stats["agent_count"] == 3
        assert stats["edge_count"] == 3
        assert stats["total_agreements"] == 1  # a-b
        assert stats["total_disagreements"] == 2  # a-c, b-c
        assert stats["total_coalitions"] == 1
        assert stats["overall_agreement_rate"] == pytest.approx(1 / 3)
