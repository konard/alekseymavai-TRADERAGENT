from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Optional


@dataclass
class ExposureCell:
    symbol: str
    strategy: str
    exposure_usd: float
    direction: str  # "long" | "short" | "neutral"
    risk_pct: float  # of portfolio
    pnl: float
    intensity: float  # 0-1 normalized for heatmap coloring

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "exposure_usd": self.exposure_usd,
            "direction": self.direction,
            "risk_pct": self.risk_pct,
            "pnl": self.pnl,
            "intensity": self.intensity,
        }


class PortfolioHeatMap:
    def __init__(self, portfolio_value: float = 10000.0) -> None:
        self._portfolio_value = portfolio_value
        # keyed by (symbol, strategy)
        self._positions: dict[tuple[str, str], dict] = {}

    def update_portfolio_value(self, value: float) -> None:
        self._portfolio_value = value

    def add_position(
        self,
        symbol: str,
        strategy: str,
        exposure_usd: float,
        direction: str,
        pnl: float = 0.0,
    ) -> None:
        self._positions[(symbol, strategy)] = {
            "symbol": symbol,
            "strategy": strategy,
            "exposure_usd": exposure_usd,
            "direction": direction,
            "pnl": pnl,
        }

    def remove_position(self, symbol: str, strategy: str) -> None:
        self._positions.pop((symbol, strategy), None)

    # ------------------------------------------------------------------
    # Heatmap
    # ------------------------------------------------------------------

    def _build_cell(self, pos: dict) -> ExposureCell:
        pv = self._portfolio_value if self._portfolio_value != 0 else 1.0
        risk_pct = abs(pos["exposure_usd"]) / pv * 100.0
        intensity = min(abs(pos["exposure_usd"]) / pv, 1.0)
        return ExposureCell(
            symbol=pos["symbol"],
            strategy=pos["strategy"],
            exposure_usd=pos["exposure_usd"],
            direction=pos["direction"],
            risk_pct=risk_pct,
            pnl=pos["pnl"],
            intensity=intensity,
        )

    def get_heatmap(self) -> dict:
        cells = [self._build_cell(p) for p in self._positions.values()]
        symbols = sorted({c.symbol for c in cells})
        strategies = sorted({c.strategy for c in cells})
        total_exposure = sum(abs(c.exposure_usd) for c in cells)

        pv = self._portfolio_value if self._portfolio_value != 0 else 1.0
        total_risk_pct = total_exposure / pv * 100.0

        long_exp = sum(c.exposure_usd for c in cells if c.direction == "long")
        short_exp = sum(abs(c.exposure_usd) for c in cells if c.direction == "short")
        net = long_exp - short_exp
        if net > 0:
            net_direction = "long"
        elif net < 0:
            net_direction = "short"
        else:
            net_direction = "neutral"

        return {
            "cells": [c.to_dict() for c in cells],
            "symbols": symbols,
            "strategies": strategies,
            "total_exposure": total_exposure,
            "total_risk_pct": total_risk_pct,
            "net_direction": net_direction,
        }

    # ------------------------------------------------------------------
    # Risk concentration
    # ------------------------------------------------------------------

    def get_risk_concentration(self) -> dict:
        total = sum(abs(p["exposure_usd"]) for p in self._positions.values())

        by_symbol: dict[str, float] = {}
        by_strategy: dict[str, float] = {}
        for p in self._positions.values():
            by_symbol[p["symbol"]] = by_symbol.get(p["symbol"], 0.0) + abs(p["exposure_usd"])
            by_strategy[p["strategy"]] = by_strategy.get(p["strategy"], 0.0) + abs(
                p["exposure_usd"]
            )

        if total > 0:
            by_symbol_pct = {k: v / total for k, v in by_symbol.items()}
            by_strategy_pct = {k: v / total for k, v in by_strategy.items()}
        else:
            by_symbol_pct = {}
            by_strategy_pct = {}

        max_sym = max(by_symbol_pct, key=by_symbol_pct.get) if by_symbol_pct else ""
        max_strat = max(by_strategy_pct, key=by_strategy_pct.get) if by_strategy_pct else ""

        # Herfindahl index on symbol proportions
        hhi = sum(v**2 for v in by_symbol_pct.values())

        return {
            "by_symbol": by_symbol_pct,
            "by_strategy": by_strategy_pct,
            "max_concentration_symbol": max_sym,
            "max_concentration_strategy": max_strat,
            "herfindahl_index": hhi,
        }

    # ------------------------------------------------------------------
    # PnL heatmap
    # ------------------------------------------------------------------

    def get_pnl_heatmap(self) -> dict:
        pv = self._portfolio_value if self._portfolio_value != 0 else 1.0
        cells: list[dict] = []
        for p in self._positions.values():
            intensity = min(abs(p["pnl"]) / pv, 1.0)
            risk_pct = abs(p["exposure_usd"]) / pv * 100.0
            cell = ExposureCell(
                symbol=p["symbol"],
                strategy=p["strategy"],
                exposure_usd=p["exposure_usd"],
                direction=p["direction"],
                risk_pct=risk_pct,
                pnl=p["pnl"],
                intensity=intensity,
            )
            cells.append(cell.to_dict())
        symbols = sorted({c["symbol"] for c in cells})
        strategies = sorted({c["strategy"] for c in cells})
        total_exposure = sum(abs(c["exposure_usd"]) for c in cells)
        total_risk_pct = total_exposure / pv * 100.0

        long_exp = sum(c["exposure_usd"] for c in cells if c["direction"] == "long")
        short_exp = sum(abs(c["exposure_usd"]) for c in cells if c["direction"] == "short")
        net = long_exp - short_exp
        if net > 0:
            net_direction = "long"
        elif net < 0:
            net_direction = "short"
        else:
            net_direction = "neutral"

        return {
            "cells": cells,
            "symbols": symbols,
            "strategies": strategies,
            "total_exposure": total_exposure,
            "total_risk_pct": total_risk_pct,
            "net_direction": net_direction,
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        total_exposure = sum(abs(p["exposure_usd"]) for p in self._positions.values())
        total_pnl = sum(p["pnl"] for p in self._positions.values())
        pv = self._portfolio_value if self._portfolio_value != 0 else 1.0
        return {
            "portfolio_value": self._portfolio_value,
            "position_count": len(self._positions),
            "total_exposure": total_exposure,
            "total_pnl": total_pnl,
            "exposure_pct": total_exposure / pv * 100.0,
        }


# ======================================================================
# Agent Collaboration Graph
# ======================================================================


@dataclass
class CollaborationEdge:
    agent_a: str
    agent_b: str
    agreement_count: int = 0
    disagreement_count: int = 0
    agreement_rate: float = 0.0
    coalition_count: int = 0

    def to_dict(self) -> dict:
        return {
            "agent_a": self.agent_a,
            "agent_b": self.agent_b,
            "agreement_count": self.agreement_count,
            "disagreement_count": self.disagreement_count,
            "agreement_rate": self.agreement_rate,
            "coalition_count": self.coalition_count,
        }


class AgentCollaborationGraph:
    def __init__(self) -> None:
        # edges keyed by frozenset({a, b})
        self._edges: dict[frozenset, CollaborationEdge] = {}
        self._agents: set = set()
        self._agent_vote_count: dict[str, int] = {}

    def _get_edge(self, a: str, b: str) -> CollaborationEdge:
        key = frozenset({a, b})
        if key not in self._edges:
            sorted_pair = sorted([a, b])
            self._edges[key] = CollaborationEdge(agent_a=sorted_pair[0], agent_b=sorted_pair[1])
        return self._edges[key]

    def _update_rate(self, edge: CollaborationEdge) -> None:
        total = edge.agreement_count + edge.disagreement_count
        edge.agreement_rate = edge.agreement_count / total if total > 0 else 0.0

    def record_vote(self, session_id: str, votes: dict[str, str]) -> None:
        agents = list(votes.keys())
        for agent in agents:
            self._agents.add(agent)
            self._agent_vote_count[agent] = self._agent_vote_count.get(agent, 0) + 1

        for a, b in combinations(agents, 2):
            edge = self._get_edge(a, b)
            if votes[a] == votes[b]:
                edge.agreement_count += 1
            else:
                edge.disagreement_count += 1
            self._update_rate(edge)

    def record_coalition(self, agents: list[str]) -> None:
        for agent in agents:
            self._agents.add(agent)
        for a, b in combinations(agents, 2):
            edge = self._get_edge(a, b)
            edge.coalition_count += 1

    # ------------------------------------------------------------------
    # Graph output
    # ------------------------------------------------------------------

    def get_graph(self) -> dict:
        nodes = []
        for agent in sorted(self._agents):
            # avg agreement rate across all edges involving this agent
            rates = []
            for key, edge in self._edges.items():
                if agent in key:
                    total = edge.agreement_count + edge.disagreement_count
                    if total > 0:
                        rates.append(edge.agreement_rate)
            avg_rate = sum(rates) / len(rates) if rates else 0.0
            nodes.append(
                {
                    "agent": agent,
                    "total_votes": self._agent_vote_count.get(agent, 0),
                    "avg_agreement_rate": avg_rate,
                }
            )

        edges = [e.to_dict() for e in self._edges.values()]

        clusters = self._compute_clusters()

        return {
            "nodes": nodes,
            "edges": edges,
            "clusters": clusters,
        }

    def _compute_clusters(self) -> list[list[str]]:
        """Simple greedy clustering: agents with agreement_rate > 0.6."""
        if not self._agents:
            return []

        assigned: set = set()
        clusters: list[list[str]] = []

        for agent in sorted(self._agents):
            if agent in assigned:
                continue
            cluster = [agent]
            assigned.add(agent)
            for other in sorted(self._agents):
                if other in assigned:
                    continue
                # check if other has > 0.6 agreement with ALL current cluster members
                fits = True
                for member in cluster:
                    key = frozenset({member, other})
                    edge = self._edges.get(key)
                    if edge is None:
                        fits = False
                        break
                    total = edge.agreement_count + edge.disagreement_count
                    if total == 0 or edge.agreement_rate <= 0.6:
                        fits = False
                        break
                if fits:
                    cluster.append(other)
                    assigned.add(other)
            clusters.append(sorted(cluster))

        return clusters

    def get_strongest_pair(self) -> Optional[tuple[str, str, float]]:
        if not self._edges:
            return None
        best: Optional[CollaborationEdge] = None
        for edge in self._edges.values():
            total = edge.agreement_count + edge.disagreement_count
            if total == 0:
                continue
            if best is None or edge.agreement_rate > best.agreement_rate:
                best = edge
        if best is None:
            return None
        return (best.agent_a, best.agent_b, best.agreement_rate)

    def get_most_contrarian(self, agent_name: str) -> Optional[str]:
        if agent_name not in self._agents:
            return None
        worst_rate = None
        worst_agent = None
        for key, edge in self._edges.items():
            if agent_name not in key:
                continue
            total = edge.agreement_count + edge.disagreement_count
            if total == 0:
                continue
            if worst_rate is None or edge.agreement_rate < worst_rate:
                worst_rate = edge.agreement_rate
                other = next(iter(key - {agent_name}))
                worst_agent = other
        return worst_agent

    def get_affinity_matrix(self) -> dict[str, dict[str, float]]:
        agents = sorted(self._agents)
        matrix: dict[str, dict[str, float]] = {}
        for a in agents:
            matrix[a] = {}
            for b in agents:
                if a == b:
                    matrix[a][b] = 1.0
                else:
                    key = frozenset({a, b})
                    edge = self._edges.get(key)
                    if edge is not None:
                        matrix[a][b] = edge.agreement_rate
                    else:
                        matrix[a][b] = 0.0
        return matrix

    def get_stats(self) -> dict:
        total_agreements = sum(e.agreement_count for e in self._edges.values())
        total_disagreements = sum(e.disagreement_count for e in self._edges.values())
        total_coalitions = sum(e.coalition_count for e in self._edges.values())
        total_interactions = total_agreements + total_disagreements
        return {
            "agent_count": len(self._agents),
            "edge_count": len(self._edges),
            "total_agreements": total_agreements,
            "total_disagreements": total_disagreements,
            "total_coalitions": total_coalitions,
            "overall_agreement_rate": (
                total_agreements / total_interactions if total_interactions > 0 else 0.0
            ),
        }
