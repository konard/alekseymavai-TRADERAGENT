"""Monte Carlo Risk Simulator — simulates future portfolio scenarios."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SimulationResult:
    num_simulations: int
    horizon_days: int
    initial_balance: float

    # Distribution stats
    mean_final_balance: float
    median_final_balance: float
    std_final_balance: float

    # Risk metrics
    var_95: float  # Value at Risk 95% — worst 5th percentile loss
    var_99: float  # Value at Risk 99%
    cvar_95: float  # Conditional VaR (expected shortfall)
    max_drawdown_median: float  # median max drawdown across simulations
    max_drawdown_95: float  # 95th percentile max drawdown
    prob_drawdown_10: float  # probability of >10% drawdown
    prob_drawdown_20: float  # probability of >20% drawdown
    prob_drawdown_50: float  # probability of >50% drawdown (ruin)

    # Return metrics
    mean_return_pct: float
    sharpe_estimate: float

    # Percentile paths (for fan chart)
    percentile_paths: dict[str, list[float]]  # "p5", "p25", "p50", "p75", "p95" -> daily balances

    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "num_simulations": self.num_simulations,
            "horizon_days": self.horizon_days,
            "initial_balance": self.initial_balance,
            "mean_final_balance": self.mean_final_balance,
            "median_final_balance": self.median_final_balance,
            "std_final_balance": self.std_final_balance,
            "var_95": self.var_95,
            "var_99": self.var_99,
            "cvar_95": self.cvar_95,
            "max_drawdown_median": self.max_drawdown_median,
            "max_drawdown_95": self.max_drawdown_95,
            "prob_drawdown_10": self.prob_drawdown_10,
            "prob_drawdown_20": self.prob_drawdown_20,
            "prob_drawdown_50": self.prob_drawdown_50,
            "mean_return_pct": self.mean_return_pct,
            "sharpe_estimate": self.sharpe_estimate,
            "percentile_paths": self.percentile_paths,
            "ts": self.ts,
        }


def _percentile(sorted_data: list[float], pct: float) -> float:
    """Compute percentile from already-sorted data. pct in [0, 100]."""
    if not sorted_data:
        return 0.0
    n = len(sorted_data)
    k = (pct / 100.0) * (n - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    d0 = sorted_data[f] * (c - k)
    d1 = sorted_data[c] * (k - f)
    return d0 + d1


def _median(data: list[float]) -> float:
    s = sorted(data)
    return _percentile(s, 50.0)


def _mean(data: list[float]) -> float:
    if not data:
        return 0.0
    return sum(data) / len(data)


def _std(data: list[float], mean_val: float | None = None) -> float:
    if len(data) < 2:
        return 0.0
    m = mean_val if mean_val is not None else _mean(data)
    variance = sum((x - m) ** 2 for x in data) / (len(data) - 1)
    return math.sqrt(variance)


class MonteCarloSimulator:
    """Simulates future portfolio paths using historical trade statistics.

    Method: Bootstrap from historical trade outcomes.
    Each simulation day:
      1. Sample N trades from historical distribution
      2. Apply PnL to balance
      3. Track max drawdown

    After all simulations: compute VaR, CVaR, drawdown probabilities, percentile paths.
    """

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)
        self._last_result: SimulationResult | None = None

    def simulate(
        self,
        initial_balance: float = 10000,
        daily_returns: list[float] | None = None,
        trade_pnls: list[float] | None = None,
        trades_per_day: float = 2.0,
        horizon_days: int = 30,
        num_simulations: int = 10000,
        daily_mean: float | None = None,
        daily_std: float | None = None,
    ) -> SimulationResult:
        """Run Monte Carlo simulation.

        If daily_returns provided: bootstrap resample from them.
        Elif trade_pnls provided: sample trades_per_day trades per day, sum PnLs.
        Else: use parametric normal(daily_mean, daily_std).

        For each simulation path:
          1. Start with initial_balance
          2. For each day: sample daily return, apply to balance
          3. Track running max and drawdown
          4. Record final balance and max drawdown

        After all sims: compute statistics.
        """
        all_paths: list[list[float]] = []
        final_balances: list[float] = []
        max_drawdowns: list[float] = []

        for _ in range(num_simulations):
            path = [initial_balance]
            balance = initial_balance
            peak = initial_balance
            max_dd = 0.0

            for _ in range(horizon_days):
                if daily_returns is not None:
                    # Bootstrap: resample one daily return
                    daily_ret = self._rng.choice(daily_returns)
                elif trade_pnls is not None:
                    # Sample trades_per_day trades, sum their PnLs as fraction of balance
                    n_trades = int(trades_per_day)
                    frac = trades_per_day - n_trades
                    if self._rng.random() < frac:
                        n_trades += 1
                    day_pnl = 0.0
                    for _ in range(n_trades):
                        day_pnl += self._rng.choice(trade_pnls)
                    daily_ret = day_pnl / balance if balance > 0 else 0.0
                else:
                    # Parametric normal
                    dm = daily_mean if daily_mean is not None else 0.0
                    ds = daily_std if daily_std is not None else 0.0
                    daily_ret = self._rng.gauss(dm, ds)

                balance *= (1.0 + daily_ret)
                if balance < 0:
                    balance = 0.0

                path.append(balance)

                if balance > peak:
                    peak = balance
                if peak > 0:
                    dd = (peak - balance) / peak
                    if dd > max_dd:
                        max_dd = dd

            all_paths.append(path)
            final_balances.append(balance)
            max_drawdowns.append(max_dd)

        # Compute statistics
        mean_final = _mean(final_balances)
        median_final = _median(final_balances)
        std_final = _std(final_balances, mean_final)

        var_95 = self._compute_var(final_balances, initial_balance, 0.95)
        var_99 = self._compute_var(final_balances, initial_balance, 0.99)
        cvar_95 = self._compute_cvar(final_balances, initial_balance, 0.95)

        sorted_dd = sorted(max_drawdowns)
        max_dd_median = _percentile(sorted_dd, 50.0)
        max_dd_95 = _percentile(sorted_dd, 95.0)

        prob_dd_10 = sum(1 for dd in max_drawdowns if dd > 0.10) / num_simulations
        prob_dd_20 = sum(1 for dd in max_drawdowns if dd > 0.20) / num_simulations
        prob_dd_50 = sum(1 for dd in max_drawdowns if dd > 0.50) / num_simulations

        mean_return_pct = ((mean_final - initial_balance) / initial_balance) * 100.0 if initial_balance > 0 else 0.0

        # Sharpe estimate: annualized
        daily_rets_realized = []
        for path in all_paths:
            for i in range(1, len(path)):
                if path[i - 1] > 0:
                    daily_rets_realized.append((path[i] - path[i - 1]) / path[i - 1])
        mean_dr = _mean(daily_rets_realized)
        std_dr = _std(daily_rets_realized, mean_dr)
        sharpe = (mean_dr / std_dr * math.sqrt(252)) if std_dr > 0 else 0.0

        percentile_paths = self._compute_percentile_paths(all_paths, [5, 25, 50, 75, 95])

        result = SimulationResult(
            num_simulations=num_simulations,
            horizon_days=horizon_days,
            initial_balance=initial_balance,
            mean_final_balance=mean_final,
            median_final_balance=median_final,
            std_final_balance=std_final,
            var_95=var_95,
            var_99=var_99,
            cvar_95=cvar_95,
            max_drawdown_median=max_dd_median,
            max_drawdown_95=max_dd_95,
            prob_drawdown_10=prob_dd_10,
            prob_drawdown_20=prob_dd_20,
            prob_drawdown_50=prob_dd_50,
            mean_return_pct=mean_return_pct,
            sharpe_estimate=sharpe,
            percentile_paths=percentile_paths,
        )
        self._last_result = result
        return result

    def _compute_var(
        self, final_balances: list[float], initial: float, confidence: float
    ) -> float:
        """VaR = initial_balance - percentile(final_balances, 1-confidence)"""
        sorted_fb = sorted(final_balances)
        pct = (1.0 - confidence) * 100.0
        threshold = _percentile(sorted_fb, pct)
        return initial - threshold

    def _compute_cvar(
        self, final_balances: list[float], initial: float, confidence: float
    ) -> float:
        """CVaR = mean of losses worse than VaR."""
        sorted_fb = sorted(final_balances)
        pct = (1.0 - confidence) * 100.0
        threshold = _percentile(sorted_fb, pct)
        tail = [b for b in sorted_fb if b <= threshold]
        if not tail:
            return self._compute_var(final_balances, initial, confidence)
        return initial - _mean(tail)

    def _compute_percentile_paths(
        self, all_paths: list[list[float]], percentiles: list[int]
    ) -> dict[str, list[float]]:
        """Extract percentile paths for fan chart visualization."""
        if not all_paths:
            return {}
        path_len = len(all_paths[0])
        result: dict[str, list[float]] = {}
        for p in percentiles:
            key = f"p{p}"
            result[key] = []
        for day_idx in range(path_len):
            day_values = sorted(all_paths[sim][day_idx] for sim in range(len(all_paths)))
            for p in percentiles:
                key = f"p{p}"
                result[key].append(_percentile(day_values, float(p)))
        return result

    def estimate_from_pattern_learner(
        self,
        pattern_learner: Any,
        initial_balance: float = 10000,
        horizon_days: int = 30,
    ) -> SimulationResult:
        """Convenience: extract trade stats from PatternLearner and simulate.

        Gets win_rate, avg_win, avg_loss from learner stats and top patterns,
        then generates synthetic trade PnLs for simulation.
        """
        stats = pattern_learner.get_stats()
        top_patterns = stats.get("top_patterns", [])

        # Aggregate win_rate and avg_pnl from qualified patterns
        if top_patterns:
            win_rates = [p.get("win_rate", 0.5) for p in top_patterns]
            avg_pnls = [p.get("avg_pnl", 0.0) for p in top_patterns]
            overall_wr = _mean(win_rates)
            overall_avg_pnl = _mean(avg_pnls)
        else:
            overall_wr = 0.5
            overall_avg_pnl = 0.0

        # Estimate avg_win and avg_loss from win_rate and avg_pnl
        # avg_pnl = win_rate * avg_win + (1 - win_rate) * avg_loss
        # Assume avg_win = -avg_loss * 1.5 (typical risk-reward)
        if overall_wr > 0 and overall_wr < 1:
            # Solve: avg_pnl = wr * avg_win - (1-wr) * abs(avg_loss)
            # Assume avg_win = R * abs(avg_loss), solve for abs(avg_loss):
            # avg_pnl = wr * R * L - (1-wr) * L  => L = avg_pnl / (wr*R - (1-wr))
            rr = 1.5
            denom = overall_wr * rr - (1.0 - overall_wr)
            if abs(denom) > 1e-9:
                avg_loss_abs = abs(overall_avg_pnl / denom) if overall_avg_pnl != 0 else initial_balance * 0.01
                avg_win = rr * avg_loss_abs
                avg_loss = -avg_loss_abs
            else:
                avg_win = initial_balance * 0.015
                avg_loss = -initial_balance * 0.01
        else:
            avg_win = initial_balance * 0.015
            avg_loss = -initial_balance * 0.01

        # Generate synthetic trade PnLs
        synthetic_pnls: list[float] = []
        for _ in range(200):
            if self._rng.random() < overall_wr:
                synthetic_pnls.append(avg_win * self._rng.uniform(0.5, 1.5))
            else:
                synthetic_pnls.append(avg_loss * self._rng.uniform(0.5, 1.5))

        return self.simulate(
            initial_balance=initial_balance,
            trade_pnls=synthetic_pnls,
            trades_per_day=2.0,
            horizon_days=horizon_days,
            num_simulations=10000,
        )

    def get_stats(self) -> dict:
        """Return stats about the last simulation run."""
        if self._last_result is None:
            return {"status": "no_simulation_run"}
        return {
            "status": "completed",
            "last_result": self._last_result.to_dict(),
        }
