from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional


@dataclass
class TradeFeatures:
    trade_id: str
    regime: str
    strategy: str
    direction: str
    hour_bucket: str  # "asia" | "europe" | "us"
    volatility_bucket: str  # "low" | "normal" | "high"
    pnl: float
    win: bool
    holding_bars: int

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "regime": self.regime,
            "strategy": self.strategy,
            "direction": self.direction,
            "hour_bucket": self.hour_bucket,
            "volatility_bucket": self.volatility_bucket,
            "pnl": self.pnl,
            "win": self.win,
            "holding_bars": self.holding_bars,
        }


@dataclass
class TradeCluster:
    cluster_id: str
    label: str
    trades: list[TradeFeatures]
    size: int
    win_rate: float
    avg_pnl: float
    total_pnl: float
    dominant_regime: str
    dominant_strategy: str
    dominant_direction: str

    def to_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "label": self.label,
            "size": self.size,
            "win_rate": self.win_rate,
            "avg_pnl": self.avg_pnl,
            "total_pnl": self.total_pnl,
            "dominant_regime": self.dominant_regime,
            "dominant_strategy": self.dominant_strategy,
            "dominant_direction": self.dominant_direction,
        }


def _most_common(values: list[str]) -> str:
    """Return the most common value in a list."""
    if not values:
        return ""
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda k: counts[k])


class TradeClustering:

    def __init__(self) -> None:
        self._trades: list[TradeFeatures] = []

    def add_trade(self, trade: TradeFeatures) -> None:
        self._trades.append(trade)

    def cluster(self, method: str = "feature") -> list[TradeCluster]:
        if method != "feature":
            raise ValueError(f"Unknown clustering method: {method}")

        groups: dict[tuple[str, str, str], list[TradeFeatures]] = defaultdict(list)
        for t in self._trades:
            key = (t.regime, t.strategy, t.direction)
            groups[key].append(t)

        clusters: list[TradeCluster] = []
        for idx, ((regime, strategy, direction), trades) in enumerate(
            sorted(groups.items())
        ):
            size = len(trades)
            wins = sum(1 for t in trades if t.win)
            win_rate = wins / size if size else 0.0
            total_pnl = sum(t.pnl for t in trades)
            avg_pnl = total_pnl / size if size else 0.0
            label = f"{strategy}_{direction}_{regime}"
            cluster_id = f"cluster_{idx}"

            clusters.append(
                TradeCluster(
                    cluster_id=cluster_id,
                    label=label,
                    trades=trades,
                    size=size,
                    win_rate=win_rate,
                    avg_pnl=avg_pnl,
                    total_pnl=total_pnl,
                    dominant_regime=regime,
                    dominant_strategy=strategy,
                    dominant_direction=direction,
                )
            )

        return clusters

    def find_biases(self) -> list[dict]:
        clusters = self.cluster()
        biases: list[dict] = []

        for c in clusters:
            if c.size >= 5 and c.win_rate < 0.35:
                biases.append(
                    {
                        "bias_type": "systematic_loser",
                        "cluster": c.label,
                        "details": {
                            "win_rate": c.win_rate,
                            "size": c.size,
                            "avg_pnl": c.avg_pnl,
                        },
                    }
                )
            if c.size >= 5 and c.win_rate > 0.70:
                biases.append(
                    {
                        "bias_type": "strong_edge",
                        "cluster": c.label,
                        "details": {
                            "win_rate": c.win_rate,
                            "size": c.size,
                            "avg_pnl": c.avg_pnl,
                        },
                    }
                )

        # Directional bias: check across ALL trades
        if self._trades:
            long_count = sum(1 for t in self._trades if t.direction == "long")
            total = len(self._trades)
            long_ratio = long_count / total
            if long_ratio > 0.80:
                biases.append(
                    {
                        "bias_type": "directional_bias",
                        "cluster": "all",
                        "details": {
                            "direction": "long",
                            "ratio": long_ratio,
                            "count": long_count,
                            "total": total,
                        },
                    }
                )
            elif (1 - long_ratio) > 0.80:
                biases.append(
                    {
                        "bias_type": "directional_bias",
                        "cluster": "all",
                        "details": {
                            "direction": "short",
                            "ratio": 1 - long_ratio,
                            "count": total - long_count,
                            "total": total,
                        },
                    }
                )

        return biases

    def get_regime_breakdown(self) -> dict[str, dict]:
        regime_trades: dict[str, list[TradeFeatures]] = defaultdict(list)
        for t in self._trades:
            regime_trades[t.regime].append(t)

        result: dict[str, dict] = {}
        for regime, trades in sorted(regime_trades.items()):
            size = len(trades)
            wins = sum(1 for t in trades if t.win)
            win_rate = wins / size if size else 0.0
            total_pnl = sum(t.pnl for t in trades)
            avg_pnl = total_pnl / size if size else 0.0

            # Strategy performance within regime
            strat_pnl: dict[str, float] = defaultdict(float)
            for t in trades:
                strat_pnl[t.strategy] += t.pnl

            best_strategy = max(strat_pnl, key=lambda k: strat_pnl[k]) if strat_pnl else ""
            worst_strategy = min(strat_pnl, key=lambda k: strat_pnl[k]) if strat_pnl else ""

            result[regime] = {
                "trade_count": size,
                "win_rate": win_rate,
                "avg_pnl": avg_pnl,
                "best_strategy": best_strategy,
                "worst_strategy": worst_strategy,
            }

        return result

    def get_time_breakdown(self) -> dict[str, dict]:
        bucket_trades: dict[str, list[TradeFeatures]] = defaultdict(list)
        for t in self._trades:
            bucket_trades[t.hour_bucket].append(t)

        result: dict[str, dict] = {}
        for bucket, trades in sorted(bucket_trades.items()):
            size = len(trades)
            wins = sum(1 for t in trades if t.win)
            win_rate = wins / size if size else 0.0
            total_pnl = sum(t.pnl for t in trades)
            avg_pnl = total_pnl / size if size else 0.0

            result[bucket] = {
                "trade_count": size,
                "win_rate": win_rate,
                "avg_pnl": avg_pnl,
            }

        return result

    def get_stats(self) -> dict:
        total = len(self._trades)
        if total == 0:
            return {
                "total_trades": 0,
                "clusters": 0,
                "biases": [],
            }

        clusters = self.cluster()
        biases = self.find_biases()
        wins = sum(1 for t in self._trades if t.win)

        return {
            "total_trades": total,
            "overall_win_rate": wins / total,
            "overall_avg_pnl": sum(t.pnl for t in self._trades) / total,
            "clusters": len(clusters),
            "biases": biases,
            "regime_breakdown": self.get_regime_breakdown(),
            "time_breakdown": self.get_time_breakdown(),
        }


class AdaptiveLearningRate:

    def __init__(
        self,
        initial_rate: float = 0.1,
        min_rate: float = 0.01,
        max_rate: float = 0.5,
        decay: float = 0.995,
        boost_on_regime_change: float = 2.0,
    ) -> None:
        self._initial_rate = initial_rate
        self._min_rate = min_rate
        self._max_rate = max_rate
        self._decay = decay
        self._boost_on_regime_change = boost_on_regime_change
        self._rates: dict[str, float] = {}
        self._steps: dict[str, int] = defaultdict(int)
        self._current_regime: Optional[str] = None

    def _clamp(self, rate: float) -> float:
        return max(self._min_rate, min(self._max_rate, rate))

    def _ensure_agent(self, agent_name: str) -> None:
        if agent_name not in self._rates:
            self._rates[agent_name] = self._initial_rate

    def get_rate(self, agent_name: str) -> float:
        self._ensure_agent(agent_name)
        return self._rates[agent_name]

    def step(self, agent_name: str, was_correct: bool) -> None:
        self._ensure_agent(agent_name)
        self._steps[agent_name] += 1
        if was_correct:
            self._rates[agent_name] *= self._decay
        else:
            self._rates[agent_name] *= 1.1
        self._rates[agent_name] = self._clamp(self._rates[agent_name])

    def on_regime_change(self, new_regime: str) -> None:
        self._current_regime = new_regime
        for agent_name in list(self._rates):
            self._rates[agent_name] *= self._boost_on_regime_change
            self._rates[agent_name] = self._clamp(self._rates[agent_name])

    def on_stable_regime(self) -> None:
        extra_decay = self._decay ** 5
        for agent_name in list(self._rates):
            self._rates[agent_name] *= extra_decay
            self._rates[agent_name] = self._clamp(self._rates[agent_name])

    def get_all_rates(self) -> dict[str, float]:
        return dict(self._rates)

    def reset(self, agent_name: str | None = None) -> None:
        if agent_name is not None:
            self._rates[agent_name] = self._initial_rate
            self._steps[agent_name] = 0
        else:
            for name in list(self._rates):
                self._rates[name] = self._initial_rate
                self._steps[name] = 0

    def get_stats(self) -> dict:
        return {
            "agents": {
                name: {
                    "rate": self._rates[name],
                    "steps": self._steps[name],
                }
                for name in self._rates
            },
            "current_regime": self._current_regime,
            "initial_rate": self._initial_rate,
            "min_rate": self._min_rate,
            "max_rate": self._max_rate,
            "decay": self._decay,
            "boost_on_regime_change": self._boost_on_regime_change,
        }
