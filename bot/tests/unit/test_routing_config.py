"""
Unit tests for bot/orchestrator/routing_config.py

Covers:
- Loading the default YAML config
- get_strategies() returns correct strategy lists for each regime
- get_hybrid_weights() returns hybrid weights from config
- FileNotFoundError when config file is missing
- ValueError when YAML is malformed
- Regime conditions matching (exact string and enum values)
- Fallback when no rule matches
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bot.orchestrator.market_regime import MarketRegime
from bot.orchestrator.routing_config import RoutingConfig, StrategyConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, content: str) -> RoutingConfig:
    """Write *content* to a temp YAML and return a RoutingConfig for it."""
    p = tmp_path / "routing.yaml"
    p.write_text(textwrap.dedent(content))
    return RoutingConfig(p)


# ---------------------------------------------------------------------------
# StrategyConfig dataclass
# ---------------------------------------------------------------------------


class TestStrategyConfig:
    def test_frozen(self) -> None:
        sc = StrategyConfig(type="grid", weight=1.0, priority=1)
        with pytest.raises((AttributeError, TypeError)):
            sc.type = "dca"  # type: ignore[misc]

    def test_fields(self) -> None:
        sc = StrategyConfig(type="trend_follower", weight=0.7, priority=1)
        assert sc.type == "trend_follower"
        assert sc.weight == 0.7
        assert sc.priority == 1


# ---------------------------------------------------------------------------
# Loading — default config file
# ---------------------------------------------------------------------------


class TestRoutingConfigLoad:
    def test_loads_default_config(self) -> None:
        """RoutingConfig() with no args should load the shipping YAML."""
        cfg = RoutingConfig()
        assert len(cfg.rules) >= 4  # at least the basic rules must be present

    def test_config_path_stored(self, tmp_path: Path) -> None:
        cfg = _make_config(
            tmp_path,
            """
            version: "1.0"
            rules: []
            """,
        )
        assert cfg.config_path == tmp_path / "routing.yaml"

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Strategy routing config not found"):
            RoutingConfig(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("key: [unclosed")
        with pytest.raises(ValueError, match="Invalid YAML"):
            RoutingConfig(p)

    def test_non_mapping_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "list.yaml"
        p.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            RoutingConfig(p)

    def test_missing_strategy_type_raises(self, tmp_path: Path) -> None:
        cfg_text = """
        version: "1.0"
        rules:
          - name: bad_rule
            conditions:
              market_regime: tight_range
            strategies:
              - weight: 1.0
                priority: 1
        """
        with pytest.raises(ValueError, match="missing required key"):
            _make_config(tmp_path, cfg_text)


# ---------------------------------------------------------------------------
# get_strategies — default config
# ---------------------------------------------------------------------------


class TestGetStrategiesDefaultConfig:
    """Verify that the shipping strategy_routing.yaml yields correct results."""

    def setup_method(self) -> None:
        self.cfg = RoutingConfig()

    def test_tight_range_returns_grid(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "tight_range"})
        types = [s.type for s in result]
        assert types == ["grid"]

    def test_wide_range_returns_grid(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "wide_range"})
        types = [s.type for s in result]
        assert types == ["grid"]

    def test_quiet_transition_returns_grid_weighted(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "quiet_transition"})
        assert len(result) == 1
        assert result[0].type == "grid"
        assert result[0].weight == 0.7

    def test_volatile_transition_returns_smc(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "volatile_transition"})
        types = [s.type for s in result]
        assert "smc" in types

    def test_bull_trend_returns_tf_and_dca(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "bull_trend"})
        types = [s.type for s in result]
        assert "trend_follower" in types
        assert "dca" in types

    def test_bull_trend_tf_has_higher_priority_than_dca(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "bull_trend"})
        pmap = {s.type: s.priority for s in result}
        assert pmap["trend_follower"] < pmap["dca"]

    def test_bear_trend_returns_dca_only(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "bear_trend"})
        types = [s.type for s in result]
        assert types == ["dca"]
        assert result[0].weight == 1.0

    def test_accumulation_returns_smc(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "accumulation"})
        types = [s.type for s in result]
        assert types == ["smc"]

    def test_distribution_returns_smc(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "distribution"})
        types = [s.type for s in result]
        assert types == ["smc"]

    def test_unknown_returns_empty(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "unknown"})
        assert result == []

    def test_no_match_returns_fallback_empty(self) -> None:
        result = self.cfg.get_strategies({"market_regime": "non_existent_regime"})
        assert result == []


# ---------------------------------------------------------------------------
# get_strategies — enum values as conditions
# ---------------------------------------------------------------------------


class TestGetStrategiesEnumConditions:
    """Passing MarketRegime enum values should work the same as strings."""

    def setup_method(self) -> None:
        self.cfg = RoutingConfig()

    def test_bull_trend_enum(self) -> None:
        result = self.cfg.get_strategies({"market_regime": MarketRegime.BULL_TREND})
        types = {s.type for s in result}
        assert "trend_follower" in types
        assert "dca" in types

    def test_bear_trend_enum(self) -> None:
        result = self.cfg.get_strategies({"market_regime": MarketRegime.BEAR_TREND})
        types = {s.type for s in result}
        assert types == {"dca"}

    def test_tight_range_enum(self) -> None:
        result = self.cfg.get_strategies({"market_regime": MarketRegime.TIGHT_RANGE})
        assert result[0].type == "grid"

    def test_accumulation_enum(self) -> None:
        result = self.cfg.get_strategies({"market_regime": MarketRegime.ACCUMULATION})
        assert result[0].type == "smc"

    def test_distribution_enum(self) -> None:
        result = self.cfg.get_strategies({"market_regime": MarketRegime.DISTRIBUTION})
        assert result[0].type == "smc"


# ---------------------------------------------------------------------------
# get_hybrid_weights
# ---------------------------------------------------------------------------


class TestGetHybridWeights:
    def test_hybrid_weights_returned(self) -> None:
        cfg = RoutingConfig()
        weights = cfg.get_hybrid_weights()
        assert len(weights) >= 1

    def test_hybrid_contains_expected_types(self) -> None:
        cfg = RoutingConfig()
        weights = cfg.get_hybrid_weights()
        types = {w.type for w in weights}
        # Shipping config has dca, grid, trend_follower in hybrid mode
        assert "dca" in types
        assert "grid" in types
        assert "trend_follower" in types

    def test_hybrid_weights_sum_to_one(self) -> None:
        cfg = RoutingConfig()
        weights = cfg.get_hybrid_weights()
        total = sum(w.weight for w in weights)
        assert abs(total - 1.0) < 1e-9

    def test_hybrid_returns_copy(self) -> None:
        cfg = RoutingConfig()
        w1 = cfg.get_hybrid_weights()
        w2 = cfg.get_hybrid_weights()
        assert w1 is not w2


# ---------------------------------------------------------------------------
# Custom YAML — rule ordering and fallback
# ---------------------------------------------------------------------------


class TestCustomConfig:
    def test_first_rule_wins(self, tmp_path: Path) -> None:
        """When multiple rules could match, the first one wins."""
        cfg = _make_config(
            tmp_path,
            """
            version: "1.0"
            rules:
              - name: rule_a
                conditions:
                  market_regime: test_regime
                strategies:
                  - type: grid
                    weight: 1.0
                    priority: 1
              - name: rule_b
                conditions:
                  market_regime: test_regime
                strategies:
                  - type: dca
                    weight: 1.0
                    priority: 1
            """,
        )
        result = cfg.get_strategies({"market_regime": "test_regime"})
        assert result[0].type == "grid"  # first rule

    def test_fallback_used_when_no_match(self, tmp_path: Path) -> None:
        cfg = _make_config(
            tmp_path,
            """
            version: "1.0"
            rules:
              - name: grid_rule
                conditions:
                  market_regime: tight_range
                strategies:
                  - type: grid
                    weight: 1.0
                    priority: 1
            fallback:
              strategies:
                - type: dca
                  weight: 1.0
                  priority: 1
            """,
        )
        result = cfg.get_strategies({"market_regime": "bear_trend"})
        assert result[0].type == "dca"

    def test_empty_strategy_list_valid(self, tmp_path: Path) -> None:
        cfg = _make_config(
            tmp_path,
            """
            version: "1.0"
            rules:
              - name: empty
                conditions:
                  market_regime: unknown
                strategies: []
            """,
        )
        result = cfg.get_strategies({"market_regime": "unknown"})
        assert result == []

    def test_multi_condition_matching(self, tmp_path: Path) -> None:
        """Rules with multiple conditions must ALL match."""
        cfg = _make_config(
            tmp_path,
            """
            version: "1.0"
            rules:
              - name: specific
                conditions:
                  market_regime: bull_trend
                  recommended_strategy: hybrid
                strategies:
                  - type: smc
                    weight: 1.0
                    priority: 1
              - name: generic
                conditions:
                  market_regime: bull_trend
                strategies:
                  - type: dca
                    weight: 1.0
                    priority: 1
            """,
        )
        # Both conditions present — specific rule matches
        result_specific = cfg.get_strategies(
            {"market_regime": "bull_trend", "recommended_strategy": "hybrid"}
        )
        assert result_specific[0].type == "smc"

        # Only market_regime — specific rule does NOT match (missing recommended_strategy)
        result_generic = cfg.get_strategies({"market_regime": "bull_trend"})
        assert result_generic[0].type == "dca"

    def test_weight_and_priority_defaults(self, tmp_path: Path) -> None:
        """weight and priority should default to 1.0 and 1 respectively."""
        cfg = _make_config(
            tmp_path,
            """
            version: "1.0"
            rules:
              - name: minimal
                conditions:
                  market_regime: tight_range
                strategies:
                  - type: grid
            """,
        )
        result = cfg.get_strategies({"market_regime": "tight_range"})
        assert result[0].weight == 1.0
        assert result[0].priority == 1

    def test_get_strategies_returns_copy(self, tmp_path: Path) -> None:
        """Mutating the returned list should not affect internal state."""
        cfg = _make_config(
            tmp_path,
            """
            version: "1.0"
            rules:
              - name: r
                conditions:
                  market_regime: tight_range
                strategies:
                  - type: grid
                    weight: 1.0
                    priority: 1
            """,
        )
        r1 = cfg.get_strategies({"market_regime": "tight_range"})
        r1.clear()
        r2 = cfg.get_strategies({"market_regime": "tight_range"})
        assert len(r2) == 1
