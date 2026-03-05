"""
Tests for OrchestratorBacktestConfig.from_yaml_config() and adapter defaults.

Covers:
- from_yaml_config() maps grid / dca / tf / smc params correctly
- Only auto_start=true bots are included
- _m5 variant bots are skipped
- Adapter constructor defaults match live phase7_demo.yaml values
"""
from decimal import Decimal
from pathlib import Path

import pytest

from bot.tests.backtesting.orchestrator_engine import OrchestratorBacktestConfig

# Path to the live config (relative to repo root, resolved at import time)
_YAML = str(Path(__file__).parents[3] / "configs" / "phase7_demo.yaml")


# ---------------------------------------------------------------------------
# from_yaml_config — BTC/USDT
# ---------------------------------------------------------------------------

class TestFromYamlConfigBTC:
    def setup_method(self):
        self.cfg = OrchestratorBacktestConfig.from_yaml_config(_YAML, "BTC/USDT")

    def test_symbol_set(self):
        assert self.cfg.symbol == "BTC/USDT"

    # Grid params (from demo_btc_hybrid)
    def test_grid_num_levels(self):
        assert self.cfg.grid_params["num_levels"] == 6

    def test_grid_amount_per_grid(self):
        assert self.cfg.grid_params["amount_per_grid"] == Decimal("150")

    def test_grid_profit_per_grid(self):
        assert self.cfg.grid_params["profit_per_grid"] == Decimal("0.012")

    # DCA params (from demo_btc_hybrid)
    def test_dca_price_deviation(self):
        assert self.cfg.dca_params["price_deviation_pct"] == Decimal("0.04")

    def test_dca_max_safety_orders(self):
        assert self.cfg.dca_params["max_safety_orders"] == 4

    def test_dca_take_profit(self):
        assert self.cfg.dca_params["take_profit_pct"] == Decimal("0.08")

    # SMC params (from demo_btc_smc)
    def test_smc_swing_length(self):
        assert self.cfg.smc_params["swing_length"] == 10

    def test_smc_risk_per_trade(self):
        assert self.cfg.smc_params["risk_per_trade"] == Decimal("0.02")

    def test_smc_min_risk_reward(self):
        assert self.cfg.smc_params["min_risk_reward"] == Decimal("2.0")

    # Trend-follower params (from demo_btc_trend)
    def test_tf_ema_periods(self):
        assert self.cfg.tf_params["ema_fast_period"] == 20
        assert self.cfg.tf_params["ema_slow_period"] == 50

    def test_tf_risk_per_trade_pct(self):
        assert self.cfg.tf_params["risk_per_trade_pct"] == Decimal("0.01")

    # m5 variant must be skipped (auto_start=false)
    def test_m5_variant_not_overwriting_smc_params(self):
        # m5 variant has swing_length_h1 / swing_length_m5 but no top-level swing_length
        # Regular smc sets swing_length=10; m5 would not override it
        assert "swing_length" in self.cfg.smc_params


# ---------------------------------------------------------------------------
# from_yaml_config — ETH/USDT (grid only)
# ---------------------------------------------------------------------------

class TestFromYamlConfigETH:
    def setup_method(self):
        self.cfg = OrchestratorBacktestConfig.from_yaml_config(_YAML, "ETH/USDT")

    def test_symbol_set(self):
        assert self.cfg.symbol == "ETH/USDT"

    def test_grid_num_levels(self):
        assert self.cfg.grid_params["num_levels"] == 8

    def test_grid_profit_per_grid(self):
        assert self.cfg.grid_params["profit_per_grid"] == Decimal("0.015")

    def test_no_dca_params(self):
        assert self.cfg.dca_params == {}

    def test_no_smc_params(self):
        assert self.cfg.smc_params == {}


# ---------------------------------------------------------------------------
# from_yaml_config — SOL/USDT (dca only)
# ---------------------------------------------------------------------------

class TestFromYamlConfigSOL:
    def setup_method(self):
        self.cfg = OrchestratorBacktestConfig.from_yaml_config(_YAML, "SOL/USDT")

    def test_symbol_set(self):
        assert self.cfg.symbol == "SOL/USDT"

    def test_dca_price_deviation(self):
        assert self.cfg.dca_params["price_deviation_pct"] == Decimal("0.02")

    def test_dca_max_safety_orders(self):
        assert self.cfg.dca_params["max_safety_orders"] == 5

    def test_dca_take_profit(self):
        assert self.cfg.dca_params["take_profit_pct"] == Decimal("0.10")

    def test_no_grid_params(self):
        assert self.cfg.grid_params == {}


# ---------------------------------------------------------------------------
# from_yaml_config — unknown symbol returns empty params
# ---------------------------------------------------------------------------

class TestFromYamlConfigUnknown:
    def test_unknown_symbol_empty_params(self):
        cfg = OrchestratorBacktestConfig.from_yaml_config(_YAML, "XYZ/USDT")
        assert cfg.symbol == "XYZ/USDT"
        assert cfg.grid_params == {}
        assert cfg.dca_params == {}
        assert cfg.smc_params == {}
        assert cfg.tf_params == {}

    def test_returns_config_instance(self):
        cfg = OrchestratorBacktestConfig.from_yaml_config(_YAML, "XYZ/USDT")
        assert isinstance(cfg, OrchestratorBacktestConfig)


# ---------------------------------------------------------------------------
# Adapter constructor defaults match live YAML values
# ---------------------------------------------------------------------------

class TestAdapterDefaults:
    def test_grid_adapter_defaults_match_live(self):
        from bot.strategies.grid_adapter import GridAdapter
        a = GridAdapter()
        assert a._num_levels == 6
        assert a._amount_per_grid == Decimal("150")
        assert a._profit_per_grid == Decimal("0.012")

    def test_dca_adapter_defaults_match_live(self):
        from bot.strategies.dca_adapter import DCAAdapter
        a = DCAAdapter()
        assert a._max_safety_orders == 4
        assert a._price_deviation_pct == Decimal("0.04")
        assert a._take_profit_pct == Decimal("0.08")
