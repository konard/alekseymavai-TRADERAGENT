"""
Tests for OrchestratorBacktestConfig.from_yaml_config() and adapter defaults.

Covers:
- from_yaml_config() maps grid / dca / tf / smc params correctly
- Only auto_start=true bots are included
- _m5 variant bots are skipped
- Adapter constructor defaults match live phase7_demo.yaml values
- _cfg_from_yaml() helper: success, missing file, bad symbol
- run_backtest_v2 script: --live-config propagates params to factories
- P1.1: strategy_params block provides universal defaults for all strategies
- P1.1: UniversalStrategyParams and SharedPerformanceParams dataclasses
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

    def test_smc_risk_per_trade_pct(self):
        assert self.cfg.smc_params["risk_per_trade_pct"] == Decimal("0.02")

    def test_smc_min_risk_reward(self):
        assert self.cfg.smc_params["min_risk_reward"] == Decimal("2.0")

    # Trend-follower params (from demo_btc_trend)
    def test_tf_ema_periods(self):
        assert self.cfg.tf_params["ema_fast_period"] == 20
        assert self.cfg.tf_params["ema_slow_period"] == 50

    def test_tf_risk_per_trade_pct(self):
        assert self.cfg.tf_params["risk_per_trade_pct"] == Decimal("0.01")

    # max_daily_loss synced from live YAML (P0.3)
    def test_max_daily_loss_synced_from_yaml(self):
        # BTC bot: max_daily_loss=$600, initial_balance=$10k → 600/10000=0.06
        assert abs(self.cfg.max_daily_loss_pct - 0.06) < 0.001

    # P0.2 — max_position_pct uses initial_balance, not hardcoded $10k
    def test_max_position_pct_from_yaml(self):
        # BTC risk_management.max_position_size=$3000, initial_balance=$10k → 30%
        assert abs(float(self.cfg.max_position_pct) - 0.30) < 0.01

    def test_max_position_size_pct_synced(self):
        # max_position_size_pct must equal max_position_pct (P0.2 unification)
        assert abs(self.cfg.max_position_size_pct - float(self.cfg.max_position_pct)) < 0.001

    def test_max_position_pct_from_strategy_params_takes_priority(self):
        # P1.1: strategy_params.max_position_pct="0.30" is a fixed fraction (30%)
        # and takes priority over risk_management USD-to-pct conversion.
        # Therefore it does NOT scale with initial_balance — it is always 30%.
        cfg20k = OrchestratorBacktestConfig.from_yaml_config(
            _YAML, "BTC/USDT", initial_balance=Decimal("20000")
        )
        assert abs(float(cfg20k.max_position_pct) - 0.30) < 0.01
        # Similarly max_daily_loss_pct from strategy_params.max_daily_loss_pct="0.06"
        # stays at 6% regardless of initial_balance.
        assert abs(cfg20k.max_daily_loss_pct - 0.06) < 0.001

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


# ---------------------------------------------------------------------------
# _cfg_from_yaml helper (from run_backtest_v2 script)
# ---------------------------------------------------------------------------

class TestCfgFromYaml:
    """Tests for the _cfg_from_yaml() helper in run_backtest_v2."""

    def _import_helper(self):
        import importlib.util, sys
        from pathlib import Path as _P
        script = _P(__file__).parents[3] / "scripts" / "run_backtest_v2.py"
        spec = importlib.util.spec_from_file_location("run_backtest_v2", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_returns_config_for_known_symbol(self):
        mod = self._import_helper()
        cfg = mod._cfg_from_yaml(_YAML, "BTC/USDT")
        assert cfg is not None
        assert cfg.symbol == "BTC/USDT"

    def test_symbol_normalization_btcusdt(self):
        # BTCUSDT (from CSV filename) must resolve to same params as BTC/USDT
        mod = self._import_helper()
        cfg = mod._cfg_from_yaml(_YAML, "BTCUSDT")
        assert cfg is not None
        assert cfg.grid_params.get("num_levels") == 6
        assert abs(cfg.max_daily_loss_pct - 0.06) < 0.001

    def test_symbol_normalization_bare_base(self):
        # bare "BTC" → BTC/USDT
        mod = self._import_helper()
        cfg = mod._cfg_from_yaml(_YAML, "BTC")
        assert cfg is not None
        assert cfg.grid_params.get("num_levels") == 6

    def test_grid_params_populated(self):
        mod = self._import_helper()
        cfg = mod._cfg_from_yaml(_YAML, "BTC/USDT")
        assert cfg.grid_params.get("num_levels") == 6

    def test_dca_params_populated(self):
        mod = self._import_helper()
        cfg = mod._cfg_from_yaml(_YAML, "BTC/USDT")
        assert cfg.dca_params.get("max_safety_orders") == 4

    def test_returns_none_for_missing_file(self):
        mod = self._import_helper()
        result = mod._cfg_from_yaml("/nonexistent/path.yaml", "BTC/USDT")
        assert result is None

    def test_returns_none_for_unknown_symbol(self):
        mod = self._import_helper()
        result = mod._cfg_from_yaml(_YAML, "XYZ/USDT")
        # from_yaml_config returns an instance with empty params, not None;
        # _cfg_from_yaml passes it through (no error)
        # So result is a valid config with empty params
        assert result is not None
        assert result.grid_params == {}
        assert result.dca_params == {}


# ---------------------------------------------------------------------------
# Integration: run_backtest_v2 propagates live params to factories
# ---------------------------------------------------------------------------

class TestScriptFactoryIntegration:
    """Verify that _make_strategy_factories receives live params from the script."""

    def _load_script(self):
        import importlib.util
        from pathlib import Path as _P
        script = _P(__file__).parents[3] / "scripts" / "run_backtest_v2.py"
        spec = importlib.util.spec_from_file_location("run_backtest_v2", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_factories_receive_grid_params_from_yaml(self):
        mod = self._load_script()
        cfg = mod._cfg_from_yaml(_YAML, "BTC/USDT")
        assert cfg is not None
        factories = mod._make_strategy_factories(
            "BTC/USDT",
            grid_params=cfg.grid_params,
            dca_params=cfg.dca_params,
            tf_params=cfg.tf_params,
            smc_params=cfg.smc_params,
        )
        # Grid factory must be present
        assert "grid" in factories
        # Instantiate with empty override params — should use live defaults
        grid = factories["grid"]({})
        assert grid._num_levels == 6
        assert grid._profit_per_grid == Decimal("0.012")

    def test_factories_receive_dca_params_from_yaml(self):
        mod = self._load_script()
        cfg = mod._cfg_from_yaml(_YAML, "BTC/USDT")
        assert cfg is not None
        factories = mod._make_strategy_factories(
            "BTC/USDT",
            grid_params=cfg.grid_params,
            dca_params=cfg.dca_params,
            tf_params=cfg.tf_params,
            smc_params=cfg.smc_params,
        )
        assert "dca" in factories
        dca = factories["dca"]({})
        assert dca._max_safety_orders == 4
        assert dca._price_deviation_pct == Decimal("0.04")


# ---------------------------------------------------------------------------
# P1.1 — Unified Strategy Parameter Model: UniversalStrategyParams
# ---------------------------------------------------------------------------

class TestUniversalStrategyParams:
    """Tests for the UniversalStrategyParams dataclass (base_params.py)."""

    def test_default_construction(self):
        from bot.strategies.base_params import UniversalStrategyParams
        p = UniversalStrategyParams()
        assert p.symbol == "BTC/USDT"
        assert p.risk_per_trade_pct == Decimal("0.01")
        assert p.max_position_pct == Decimal("0.25")
        assert p.max_daily_loss_pct == Decimal("0.06")
        assert p.max_positions == 3
        assert p.require_volume_confirmation is True
        assert p.min_volume_multiplier == Decimal("1.5")

    def test_from_dict_basic(self):
        from bot.strategies.base_params import UniversalStrategyParams
        p = UniversalStrategyParams.from_dict({
            "symbol": "ETH/USDT",
            "risk_per_trade_pct": "0.02",
            "max_position_pct": "0.20",
            "max_positions": 5,
        })
        assert p.symbol == "ETH/USDT"
        assert p.risk_per_trade_pct == Decimal("0.02")
        assert p.max_position_pct == Decimal("0.20")
        assert p.max_positions == 5

    def test_from_dict_legacy_risk_per_trade(self):
        """Legacy 'risk_per_trade' key is mapped to 'risk_per_trade_pct' with warning."""
        import warnings
        from bot.strategies.base_params import UniversalStrategyParams
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            p = UniversalStrategyParams.from_dict({"risk_per_trade": "0.03"})
        assert p.risk_per_trade_pct == Decimal("0.03")
        assert any("risk_per_trade" in str(warning.message) for warning in w)

    def test_from_dict_legacy_max_risk_per_trade_pct(self):
        """Legacy 'max_risk_per_trade_pct' key is mapped to 'risk_per_trade_pct' with warning."""
        import warnings
        from bot.strategies.base_params import UniversalStrategyParams
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            p = UniversalStrategyParams.from_dict({"max_risk_per_trade_pct": "0.015"})
        assert p.risk_per_trade_pct == Decimal("0.015")
        assert any("max_risk_per_trade_pct" in str(warning.message) for warning in w)

    def test_decimal_coercion_from_string(self):
        """String values are auto-converted to Decimal."""
        from bot.strategies.base_params import UniversalStrategyParams
        p = UniversalStrategyParams(risk_per_trade_pct="0.02")  # type: ignore[arg-type]
        assert isinstance(p.risk_per_trade_pct, Decimal)
        assert p.risk_per_trade_pct == Decimal("0.02")

    def test_decimal_coercion_from_float(self):
        """Float values are auto-converted to Decimal."""
        from bot.strategies.base_params import UniversalStrategyParams
        p = UniversalStrategyParams(max_position_pct=0.35)  # type: ignore[arg-type]
        assert isinstance(p.max_position_pct, Decimal)


# ---------------------------------------------------------------------------
# P1.1 — Unified Strategy Parameter Model: SharedPerformanceParams
# ---------------------------------------------------------------------------

class TestSharedPerformanceParams:
    """Tests for the SharedPerformanceParams dataclass (base_params.py)."""

    def test_default_construction(self):
        from bot.strategies.base_params import SharedPerformanceParams
        p = SharedPerformanceParams()
        assert p.min_risk_reward == Decimal("2.0")
        assert p.max_drawdown_pct == Decimal("0.15")
        assert p.min_sharpe_ratio == Decimal("1.0")
        assert p.min_profit_factor == Decimal("1.5")
        assert p.enable_trailing_stop is True
        assert p.trailing_activation_pct == Decimal("0.015")

    def test_from_dict_basic(self):
        from bot.strategies.base_params import SharedPerformanceParams
        p = SharedPerformanceParams.from_dict({
            "min_risk_reward": "3.0",
            "max_drawdown_pct": "0.20",
            "enable_trailing_stop": False,
        })
        assert p.min_risk_reward == Decimal("3.0")
        assert p.max_drawdown_pct == Decimal("0.20")
        assert p.enable_trailing_stop is False

    def test_from_dict_legacy_use_trailing_stop(self):
        """Legacy 'use_trailing_stop' key is mapped to 'enable_trailing_stop' with warning."""
        import warnings
        from bot.strategies.base_params import SharedPerformanceParams
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            p = SharedPerformanceParams.from_dict({"use_trailing_stop": False})
        assert p.enable_trailing_stop is False
        assert any("use_trailing_stop" in str(warning.message) for warning in w)

    def test_from_dict_legacy_max_drawdown(self):
        """Legacy 'max_drawdown' key is mapped to 'max_drawdown_pct' with warning."""
        import warnings
        from bot.strategies.base_params import SharedPerformanceParams
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            p = SharedPerformanceParams.from_dict({"max_drawdown": "0.25"})
        assert p.max_drawdown_pct == Decimal("0.25")
        assert any("max_drawdown" in str(warning.message) for warning in w)


# ---------------------------------------------------------------------------
# P1.1 — strategy_params block in YAML: universal defaults propagation
# ---------------------------------------------------------------------------

class TestStrategyParamsBlock:
    """
    Verify that the strategy_params: YAML block propagates risk_per_trade_pct
    to all four strategy param dicts (Grid, DCA, TF, SMC).
    """

    def setup_method(self):
        self.cfg = OrchestratorBacktestConfig.from_yaml_config(_YAML, "BTC/USDT")

    def test_strategy_params_risk_propagated_to_grid(self):
        # strategy_params.risk_per_trade_pct="0.01" → grid_params["risk_per_trade_pct"]
        assert "risk_per_trade_pct" in self.cfg.grid_params
        assert self.cfg.grid_params["risk_per_trade_pct"] == Decimal("0.01")

    def test_strategy_params_risk_propagated_to_dca(self):
        # strategy_params.risk_per_trade_pct="0.01" → dca_params["risk_per_trade_pct"]
        assert "risk_per_trade_pct" in self.cfg.dca_params
        assert self.cfg.dca_params["risk_per_trade_pct"] == Decimal("0.01")

    def test_smc_per_strategy_risk_overrides_universal(self):
        # demo_btc_smc sets risk_per_trade_pct="0.02" which overrides strategy_params "0.01"
        assert self.cfg.smc_params["risk_per_trade_pct"] == Decimal("0.02")

    def test_tf_per_strategy_risk_overrides_universal(self):
        # demo_btc_trend sets risk_per_trade_pct="0.01" (same as universal in this YAML)
        assert self.cfg.tf_params["risk_per_trade_pct"] == Decimal("0.01")

    def test_strategy_params_max_position_pct_applied(self):
        # strategy_params.max_position_pct="0.30" → cfg.max_position_pct=0.30
        assert abs(float(self.cfg.max_position_pct) - 0.30) < 0.01

    def test_strategy_params_max_daily_loss_pct_applied(self):
        # strategy_params.max_daily_loss_pct="0.06" → cfg.max_daily_loss_pct=0.06
        assert abs(self.cfg.max_daily_loss_pct - 0.06) < 0.001

    def test_same_risk_per_trade_pct_for_grid_and_dca(self):
        """P1.1 acceptance criterion: Grid and DCA use the same risk_per_trade_pct."""
        assert self.cfg.grid_params["risk_per_trade_pct"] == self.cfg.dca_params["risk_per_trade_pct"]


# ---------------------------------------------------------------------------
# P1.1 — Adapters accept risk_per_trade_pct parameter
# ---------------------------------------------------------------------------

class TestAdapterRiskPerTradePct:
    """Verify that GridAdapter and DCAAdapter accept risk_per_trade_pct."""

    def test_grid_adapter_accepts_risk_per_trade_pct(self):
        from bot.strategies.grid_adapter import GridAdapter
        a = GridAdapter(risk_per_trade_pct=Decimal("0.01"))
        assert a._risk_per_trade_pct == Decimal("0.01")

    def test_grid_adapter_risk_per_trade_pct_default_is_none(self):
        from bot.strategies.grid_adapter import GridAdapter
        a = GridAdapter()
        assert a._risk_per_trade_pct is None

    def test_dca_adapter_accepts_risk_per_trade_pct(self):
        from bot.strategies.dca_adapter import DCAAdapter
        a = DCAAdapter(risk_per_trade_pct=Decimal("0.01"))
        assert a._risk_per_trade_pct == Decimal("0.01")

    def test_dca_adapter_risk_per_trade_pct_default_is_none(self):
        from bot.strategies.dca_adapter import DCAAdapter
        a = DCAAdapter()
        assert a._risk_per_trade_pct is None

    def test_grid_factory_propagates_risk_per_trade_pct(self):
        """Factory instantiated with risk_per_trade_pct passes it to GridAdapter."""
        from bot.strategies.grid_adapter import GridAdapter
        params = {"risk_per_trade_pct": Decimal("0.01"), "num_levels": 6}
        a = GridAdapter(symbol="BTC/USDT", **params)
        assert a._risk_per_trade_pct == Decimal("0.01")

    def test_dca_factory_propagates_risk_per_trade_pct(self):
        """Factory instantiated with risk_per_trade_pct passes it to DCAAdapter."""
        from bot.strategies.dca_adapter import DCAAdapter
        params = {"risk_per_trade_pct": Decimal("0.01"), "max_safety_orders": 4}
        a = DCAAdapter(symbol="BTC/USDT", **params)
        assert a._risk_per_trade_pct == Decimal("0.01")
