"""Tests for Monte Carlo Risk Simulator."""

import pytest

from bot.agents.monte_carlo import MonteCarloSimulator, SimulationResult


SEED = 42
NUM_SIMS = 1000


class TestMonteCarloSimulator:

    def test_simulate_parametric(self):
        """Parametric simulation with daily_mean and daily_std produces all fields."""
        sim = MonteCarloSimulator(seed=SEED)
        result = sim.simulate(
            initial_balance=10000,
            daily_mean=0.001,
            daily_std=0.02,
            horizon_days=30,
            num_simulations=NUM_SIMS,
        )
        assert isinstance(result, SimulationResult)
        assert result.num_simulations == NUM_SIMS
        assert result.horizon_days == 30
        assert result.initial_balance == 10000
        assert result.mean_final_balance > 0
        assert result.median_final_balance > 0
        assert result.std_final_balance >= 0
        assert result.var_95 is not None
        assert result.var_99 is not None
        assert result.cvar_95 is not None
        assert result.max_drawdown_median >= 0
        assert result.max_drawdown_95 >= 0
        assert isinstance(result.percentile_paths, dict)
        assert result.ts > 0

    def test_simulate_from_returns(self):
        """Bootstrap simulation from historical daily returns."""
        import random
        rng = random.Random(123)
        daily_returns = [rng.gauss(0.0005, 0.015) for _ in range(100)]

        sim = MonteCarloSimulator(seed=SEED)
        result = sim.simulate(
            initial_balance=10000,
            daily_returns=daily_returns,
            horizon_days=30,
            num_simulations=NUM_SIMS,
        )
        assert result.num_simulations == NUM_SIMS
        assert result.mean_final_balance > 0
        assert len(result.percentile_paths) == 5

    def test_simulate_from_trades(self):
        """Simulation from historical trade PnLs."""
        import random
        rng = random.Random(456)
        trade_pnls = []
        for _ in range(200):
            if rng.random() < 0.55:
                trade_pnls.append(rng.uniform(10, 200))
            else:
                trade_pnls.append(rng.uniform(-150, -10))

        sim = MonteCarloSimulator(seed=SEED)
        result = sim.simulate(
            initial_balance=10000,
            trade_pnls=trade_pnls,
            trades_per_day=3.0,
            horizon_days=30,
            num_simulations=NUM_SIMS,
        )
        assert result.num_simulations == NUM_SIMS
        assert result.mean_final_balance > 0

    def test_var_95_is_negative(self):
        """VaR 95% should represent a loss (positive value = loss from initial)."""
        sim = MonteCarloSimulator(seed=SEED)
        result = sim.simulate(
            initial_balance=10000,
            daily_mean=0.0,
            daily_std=0.03,
            horizon_days=30,
            num_simulations=NUM_SIMS,
        )
        # With zero mean and positive std, the 5th percentile outcome
        # should be below initial balance, so VaR should be positive (a loss).
        assert result.var_95 > 0, f"VaR 95 should be positive (loss), got {result.var_95}"

    def test_cvar_worse_than_var(self):
        """CVaR (expected shortfall) should be worse (larger loss) than VaR."""
        sim = MonteCarloSimulator(seed=SEED)
        result = sim.simulate(
            initial_balance=10000,
            daily_mean=0.0,
            daily_std=0.03,
            horizon_days=30,
            num_simulations=NUM_SIMS,
        )
        assert result.cvar_95 >= result.var_95, (
            f"CVaR ({result.cvar_95}) should be >= VaR ({result.var_95})"
        )

    def test_percentile_paths_shape(self):
        """Should have 5 paths, each with horizon_days+1 points."""
        sim = MonteCarloSimulator(seed=SEED)
        horizon = 20
        result = sim.simulate(
            initial_balance=10000,
            daily_mean=0.001,
            daily_std=0.02,
            horizon_days=horizon,
            num_simulations=NUM_SIMS,
        )
        paths = result.percentile_paths
        assert len(paths) == 5
        expected_keys = {"p5", "p25", "p50", "p75", "p95"}
        assert set(paths.keys()) == expected_keys
        for key in expected_keys:
            assert len(paths[key]) == horizon + 1, (
                f"Path {key} has {len(paths[key])} points, expected {horizon + 1}"
            )

    def test_fan_chart_ordering(self):
        """p5 <= p25 <= p50 <= p75 <= p95 at each day."""
        sim = MonteCarloSimulator(seed=SEED)
        result = sim.simulate(
            initial_balance=10000,
            daily_mean=0.001,
            daily_std=0.02,
            horizon_days=20,
            num_simulations=NUM_SIMS,
        )
        paths = result.percentile_paths
        for day in range(len(paths["p5"])):
            p5 = paths["p5"][day]
            p25 = paths["p25"][day]
            p50 = paths["p50"][day]
            p75 = paths["p75"][day]
            p95 = paths["p95"][day]
            assert p5 <= p25 + 1e-9, f"Day {day}: p5={p5} > p25={p25}"
            assert p25 <= p50 + 1e-9, f"Day {day}: p25={p25} > p50={p50}"
            assert p50 <= p75 + 1e-9, f"Day {day}: p50={p50} > p75={p75}"
            assert p75 <= p95 + 1e-9, f"Day {day}: p75={p75} > p95={p95}"

    def test_drawdown_probabilities(self):
        """All drawdown probabilities should be between 0 and 1."""
        sim = MonteCarloSimulator(seed=SEED)
        result = sim.simulate(
            initial_balance=10000,
            daily_mean=0.001,
            daily_std=0.02,
            horizon_days=30,
            num_simulations=NUM_SIMS,
        )
        assert 0.0 <= result.prob_drawdown_10 <= 1.0
        assert 0.0 <= result.prob_drawdown_20 <= 1.0
        assert 0.0 <= result.prob_drawdown_50 <= 1.0
        # 10% dd should be more likely than 20%, which is more likely than 50%
        assert result.prob_drawdown_10 >= result.prob_drawdown_20
        assert result.prob_drawdown_20 >= result.prob_drawdown_50

    def test_high_volatility_higher_risk(self):
        """Higher daily_std should produce higher VaR and drawdown probability."""
        sim_low = MonteCarloSimulator(seed=SEED)
        result_low = sim_low.simulate(
            initial_balance=10000,
            daily_mean=0.0,
            daily_std=0.01,
            horizon_days=30,
            num_simulations=NUM_SIMS,
        )

        sim_high = MonteCarloSimulator(seed=SEED)
        result_high = sim_high.simulate(
            initial_balance=10000,
            daily_mean=0.0,
            daily_std=0.05,
            horizon_days=30,
            num_simulations=NUM_SIMS,
        )

        assert result_high.var_95 > result_low.var_95, (
            f"High vol VaR ({result_high.var_95}) should exceed low vol VaR ({result_low.var_95})"
        )
        assert result_high.prob_drawdown_10 > result_low.prob_drawdown_10, (
            "High vol should have higher drawdown probability"
        )

    def test_result_to_dict(self):
        """SimulationResult.to_dict() should produce a serializable dict."""
        sim = MonteCarloSimulator(seed=SEED)
        result = sim.simulate(
            initial_balance=10000,
            daily_mean=0.001,
            daily_std=0.02,
            horizon_days=10,
            num_simulations=NUM_SIMS,
        )
        d = result.to_dict()
        assert isinstance(d, dict)
        assert d["num_simulations"] == NUM_SIMS
        assert d["horizon_days"] == 10
        assert d["initial_balance"] == 10000
        assert "var_95" in d
        assert "var_99" in d
        assert "cvar_95" in d
        assert "percentile_paths" in d
        assert "ts" in d
        # Ensure JSON-serializable
        import json
        json.dumps(d)

    def test_deterministic_with_seed(self):
        """Same seed should produce identical results."""
        sim1 = MonteCarloSimulator(seed=SEED)
        result1 = sim1.simulate(
            initial_balance=10000,
            daily_mean=0.001,
            daily_std=0.02,
            horizon_days=20,
            num_simulations=NUM_SIMS,
        )

        sim2 = MonteCarloSimulator(seed=SEED)
        result2 = sim2.simulate(
            initial_balance=10000,
            daily_mean=0.001,
            daily_std=0.02,
            horizon_days=20,
            num_simulations=NUM_SIMS,
        )

        assert result1.mean_final_balance == result2.mean_final_balance
        assert result1.var_95 == result2.var_95
        assert result1.cvar_95 == result2.cvar_95
        assert result1.max_drawdown_median == result2.max_drawdown_median
        assert result1.percentile_paths == result2.percentile_paths

    def test_zero_volatility(self):
        """Zero std should produce identical paths with no VaR loss."""
        sim = MonteCarloSimulator(seed=SEED)
        result = sim.simulate(
            initial_balance=10000,
            daily_mean=0.0,
            daily_std=0.0,
            horizon_days=10,
            num_simulations=NUM_SIMS,
        )
        # All final balances should be exactly initial_balance
        assert result.mean_final_balance == pytest.approx(10000, abs=1e-6)
        assert result.median_final_balance == pytest.approx(10000, abs=1e-6)
        assert result.std_final_balance == pytest.approx(0.0, abs=1e-6)
        assert result.var_95 == pytest.approx(0.0, abs=1e-6)
        assert result.var_99 == pytest.approx(0.0, abs=1e-6)
        assert result.max_drawdown_median == pytest.approx(0.0, abs=1e-6)
        # All percentile paths should be identical
        paths = result.percentile_paths
        for key in paths:
            for val in paths[key]:
                assert val == pytest.approx(10000, abs=1e-6)


class TestMonteCarloGetStats:

    def test_get_stats_no_simulation(self):
        sim = MonteCarloSimulator(seed=SEED)
        stats = sim.get_stats()
        assert stats["status"] == "no_simulation_run"

    def test_get_stats_after_simulation(self):
        sim = MonteCarloSimulator(seed=SEED)
        sim.simulate(
            initial_balance=10000,
            daily_mean=0.001,
            daily_std=0.02,
            horizon_days=10,
            num_simulations=NUM_SIMS,
        )
        stats = sim.get_stats()
        assert stats["status"] == "completed"
        assert "last_result" in stats
        assert stats["last_result"]["num_simulations"] == NUM_SIMS
