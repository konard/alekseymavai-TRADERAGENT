"""
Unit tests for bot/orchestrator/routing_config.py

Covers:
- StrategyConfig dataclass creation
- RoutingConfig loading from a valid YAML
- get_strategies() first-match semantics
- Condition matching logic (market_regime, volatility_regime, confluence_high)
- Fallback (empty-conditions) rule
- Error handling: file not found, invalid YAML, structural errors
"""

import os
import textwrap

import pytest

from bot.orchestrator.routing_config import RoutingConfig, StrategyConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_YAML = textwrap.dedent(
    """\
    rules:
      - name: bull_trend
        conditions:
          market_regime: bull_trend
        strategies:
          - name: trend_follower
            params:
              weight: 0.7
              priority: 1
          - name: dca
            params:
              weight: 0.3
              priority: 2

      - name: fallback
        conditions: {}
        strategies:
          - name: grid
            params:
              weight: 1.0
              priority: 1
    """
)

_MULTI_CONDITION_YAML = textwrap.dedent(
    """\
    rules:
      - name: bull_trend_hybrid
        conditions:
          market_regime: bull_trend
          confluence_high: true
        strategies:
          - name: dca
            params:
              weight: 0.5
              priority: 1
          - name: trend_follower
            params:
              weight: 0.5
              priority: 2

      - name: bull_trend_default
        conditions:
          market_regime: bull_trend
        strategies:
          - name: trend_follower
            params:
              weight: 0.7
              priority: 1

      - name: fallback
        conditions: {}
        strategies:
          - name: grid
            params:
              weight: 1.0
              priority: 1
    """
)


def _write_tmp_yaml(
    tmp_path: "os.PathLike[str]", content: str, filename: str = "routing.yaml"
) -> str:
    path = os.path.join(str(tmp_path), filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


# ---------------------------------------------------------------------------
# StrategyConfig tests
# ---------------------------------------------------------------------------


class TestStrategyConfig:
    def test_creation_with_params(self) -> None:
        sc = StrategyConfig(name="grid", params={"weight": 1.0, "priority": 1})
        assert sc.name == "grid"
        assert sc.params["weight"] == 1.0
        assert sc.params["priority"] == 1

    def test_creation_default_params(self) -> None:
        sc = StrategyConfig(name="dca")
        assert sc.name == "dca"
        assert sc.params == {}


# ---------------------------------------------------------------------------
# RoutingConfig loading tests
# ---------------------------------------------------------------------------


class TestRoutingConfigLoading:
    def test_load_valid_config(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, _MINIMAL_YAML)
        cfg = RoutingConfig(path)
        assert len(cfg.rules) == 2

    def test_rules_property_returns_copy(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, _MINIMAL_YAML)
        cfg = RoutingConfig(path)
        rules_a = cfg.rules
        rules_b = cfg.rules
        # Should be independent lists (mutations don't propagate)
        assert rules_a is not rules_b

    def test_file_not_found_raises(self, tmp_path: "os.PathLike[str]") -> None:
        missing = os.path.join(str(tmp_path), "nonexistent.yaml")
        with pytest.raises(FileNotFoundError, match="nonexistent.yaml"):
            RoutingConfig(missing)

    def test_invalid_yaml_raises_value_error(self, tmp_path: "os.PathLike[str]") -> None:
        bad_yaml = "rules: [\n  - unclosed"
        path = _write_tmp_yaml(tmp_path, bad_yaml)
        with pytest.raises(ValueError, match="Invalid YAML"):
            RoutingConfig(path)

    def test_non_mapping_root_raises(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, "- just\n- a\n- list\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            RoutingConfig(path)

    def test_missing_rules_key_raises(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, "something_else: []\n")
        with pytest.raises(ValueError, match="'rules' list"):
            RoutingConfig(path)

    def test_rules_not_a_list_raises(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, "rules: not_a_list\n")
        with pytest.raises(ValueError, match="'rules' list"):
            RoutingConfig(path)

    def test_rule_not_a_mapping_raises(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, "rules:\n  - just_a_string\n")
        with pytest.raises(ValueError, match="must be a mapping"):
            RoutingConfig(path)

    def test_strategy_missing_name_raises(self, tmp_path: "os.PathLike[str]") -> None:
        bad = textwrap.dedent(
            """\
            rules:
              - name: bad_rule
                conditions: {}
                strategies:
                  - weight: 1.0
            """
        )
        path = _write_tmp_yaml(tmp_path, bad)
        with pytest.raises(ValueError, match="'name' must be a non-empty string"):
            RoutingConfig(path)

    def test_strategy_params_not_mapping_raises(self, tmp_path: "os.PathLike[str]") -> None:
        bad = textwrap.dedent(
            """\
            rules:
              - name: bad_rule
                conditions: {}
                strategies:
                  - name: grid
                    params: not_a_dict
            """
        )
        path = _write_tmp_yaml(tmp_path, bad)
        with pytest.raises(ValueError, match="'params' must be a mapping"):
            RoutingConfig(path)

    def test_conditions_not_mapping_raises(self, tmp_path: "os.PathLike[str]") -> None:
        bad = textwrap.dedent(
            """\
            rules:
              - name: bad_rule
                conditions: [list, not, dict]
                strategies: []
            """
        )
        path = _write_tmp_yaml(tmp_path, bad)
        with pytest.raises(ValueError, match="'conditions' must be a mapping"):
            RoutingConfig(path)


# ---------------------------------------------------------------------------
# get_strategies() — first-match semantics
# ---------------------------------------------------------------------------


class TestGetStrategies:
    def test_bull_trend_returns_trend_follower_and_dca(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, _MINIMAL_YAML)
        cfg = RoutingConfig(path)
        result = cfg.get_strategies({"market_regime": "bull_trend"})
        names = [s.name for s in result]
        assert names == ["trend_follower", "dca"]

    def test_params_are_passed_through(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, _MINIMAL_YAML)
        cfg = RoutingConfig(path)
        result = cfg.get_strategies({"market_regime": "bull_trend"})
        tf = next(s for s in result if s.name == "trend_follower")
        assert tf.params["weight"] == pytest.approx(0.7)
        assert tf.params["priority"] == 1

    def test_fallback_rule_when_no_match(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, _MINIMAL_YAML)
        cfg = RoutingConfig(path)
        # UNKNOWN regime doesn't match "bull_trend" rule → fallback
        result = cfg.get_strategies({"market_regime": "unknown"})
        assert len(result) == 1
        assert result[0].name == "grid"

    def test_empty_context_hits_fallback(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, _MINIMAL_YAML)
        cfg = RoutingConfig(path)
        result = cfg.get_strategies({})
        assert result[0].name == "grid"

    def test_no_match_returns_empty_when_no_fallback(self, tmp_path: "os.PathLike[str]") -> None:
        no_fallback = textwrap.dedent(
            """\
            rules:
              - name: bull_trend
                conditions:
                  market_regime: bull_trend
                strategies:
                  - name: trend_follower
                    params: {}
            """
        )
        path = _write_tmp_yaml(tmp_path, no_fallback)
        cfg = RoutingConfig(path)
        result = cfg.get_strategies({"market_regime": "bear_trend"})
        assert result == []


# ---------------------------------------------------------------------------
# Multi-condition (AND) matching
# ---------------------------------------------------------------------------


class TestMultiConditionMatching:
    def test_high_confluence_bull_trend_picks_hybrid_rule(
        self, tmp_path: "os.PathLike[str]"
    ) -> None:
        path = _write_tmp_yaml(tmp_path, _MULTI_CONDITION_YAML)
        cfg = RoutingConfig(path)
        result = cfg.get_strategies({"market_regime": "bull_trend", "confluence_high": True})
        names = [s.name for s in result]
        assert "dca" in names
        assert "trend_follower" in names
        # hybrid rule has 2 strategies
        assert len(result) == 2

    def test_low_confluence_bull_trend_picks_default_rule(
        self, tmp_path: "os.PathLike[str]"
    ) -> None:
        path = _write_tmp_yaml(tmp_path, _MULTI_CONDITION_YAML)
        cfg = RoutingConfig(path)
        result = cfg.get_strategies({"market_regime": "bull_trend", "confluence_high": False})
        # only trend_follower in the default bull_trend rule
        assert len(result) == 1
        assert result[0].name == "trend_follower"

    def test_condition_value_false_does_not_match_true(self, tmp_path: "os.PathLike[str]") -> None:
        path = _write_tmp_yaml(tmp_path, _MULTI_CONDITION_YAML)
        cfg = RoutingConfig(path)
        # Missing confluence_high → doesn't satisfy the hybrid rule
        result = cfg.get_strategies({"market_regime": "bull_trend"})
        # Falls to bull_trend_default (no confluence_high condition)
        assert len(result) == 1
        assert result[0].name == "trend_follower"


# ---------------------------------------------------------------------------
# Production config smoke test
# ---------------------------------------------------------------------------


class TestProductionConfig:
    """Verify that the shipped configs/strategy_routing.yaml is valid."""

    _PROD_PATH = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "configs",
        "strategy_routing.yaml",
    )

    def test_production_config_loads(self) -> None:
        cfg = RoutingConfig(self._PROD_PATH)
        # Issue DoD: at least 4 rules including BULL_TREND
        assert len(cfg.rules) >= 4

    def test_production_config_has_bull_trend_rule(self) -> None:
        cfg = RoutingConfig(self._PROD_PATH)
        result = cfg.get_strategies({"market_regime": "bull_trend"})
        assert len(result) >= 1

    def test_production_config_bull_trend_includes_trend_follower(self) -> None:
        cfg = RoutingConfig(self._PROD_PATH)
        result = cfg.get_strategies({"market_regime": "bull_trend"})
        names = [s.name for s in result]
        assert "trend_follower" in names

    def test_production_config_bear_trend_returns_dca(self) -> None:
        cfg = RoutingConfig(self._PROD_PATH)
        result = cfg.get_strategies({"market_regime": "bear_trend"})
        names = [s.name for s in result]
        assert "dca" in names

    def test_production_config_tight_range_returns_grid(self) -> None:
        cfg = RoutingConfig(self._PROD_PATH)
        result = cfg.get_strategies({"market_regime": "tight_range"})
        names = [s.name for s in result]
        assert "grid" in names

    def test_production_config_fallback_returns_strategies(self) -> None:
        cfg = RoutingConfig(self._PROD_PATH)
        result = cfg.get_strategies({"market_regime": "unknown"})
        assert len(result) >= 1

    def test_production_config_hybrid_bull_trend_high_confluence(self) -> None:
        cfg = RoutingConfig(self._PROD_PATH)
        result = cfg.get_strategies({"market_regime": "bull_trend", "confluence_high": True})
        names = [s.name for s in result]
        # Hybrid rule should activate multiple strategies
        assert len(names) >= 2
