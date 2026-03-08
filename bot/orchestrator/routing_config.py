"""
RoutingConfig — loads and parses strategy_routing.yaml.

Provides a single source of truth for strategy selection rules, replacing
the hard-coded DEFAULT_REGIME_STRATEGIES / HYBRID_STRATEGY_WEIGHTS dicts that
are currently split between live-bot (StrategySelector) and backtest
(StrategyRouter).

Usage::

    from bot.orchestrator.routing_config import RoutingConfig

    cfg = RoutingConfig("configs/strategy_routing.yaml")
    strategies = cfg.get_strategies(
        {"market_regime": "bull_trend", "confluence_high": True}
    )
    # → [StrategyConfig(name="trend_follower", params={"weight": 0.7, "priority": 1}),
    #    StrategyConfig(name="dca",            params={"weight": 0.3, "priority": 2})]
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class StrategyConfig:
    """Configuration for a single strategy entry returned by a routing rule."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class _RoutingRule:
    """Internal representation of one parsed routing rule."""

    rule_name: str
    conditions: dict[str, Any]
    strategies: list[StrategyConfig]


class RoutingConfig:
    """Loads and evaluates strategy routing rules from a YAML file.

    Rules are ordered; the *first* rule whose conditions are a subset of the
    supplied context dict is returned (first-match semantics).  An empty
    ``conditions`` dict matches any context and therefore acts as a fallback.

    Args:
        config_path: Path to the YAML config file.

    Raises:
        FileNotFoundError: When *config_path* does not exist.
        ValueError: When the YAML is structurally invalid.
    """

    def __init__(self, config_path: str) -> None:
        self._config_path = config_path
        self._rules: list[_RoutingRule] = []
        self._load(config_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_strategies(self, conditions: dict[str, Any]) -> list[StrategyConfig]:
        """Return strategies for the first rule that matches *conditions*.

        Args:
            conditions: Mapping of condition keys to their current values,
                e.g. ``{"market_regime": "bull_trend", "confluence_high": True}``.

        Returns:
            List of :class:`StrategyConfig` from the first matching rule, or
            an empty list when no rule matches (which only happens when the
            config contains no fallback rule).
        """
        for rule in self._rules:
            if self._matches(rule.conditions, conditions):
                logger.debug(
                    "routing_config: rule matched",
                    rule=rule.rule_name,
                    conditions=conditions,
                )
                return list(rule.strategies)

        logger.warning(
            "routing_config: no rule matched",
            conditions=conditions,
        )
        return []

    @property
    def rules(self) -> list[_RoutingRule]:
        """Read-only ordered list of parsed routing rules."""
        return list(self._rules)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self, config_path: str) -> None:
        """Load and parse the YAML file, populating ``self._rules``."""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Strategy routing config not found: {config_path!r}")

        with open(config_path, encoding="utf-8") as fh:
            try:
                data = yaml.safe_load(fh)
            except yaml.YAMLError as exc:
                raise ValueError(f"Invalid YAML in routing config {config_path!r}: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(f"Routing config must be a YAML mapping, got {type(data).__name__}")

        raw_rules = data.get("rules")
        if not isinstance(raw_rules, list):
            raise ValueError("Routing config must contain a top-level 'rules' list")

        self._rules = [self._parse_rule(r, idx) for idx, r in enumerate(raw_rules)]
        logger.info(
            "routing_config: loaded",
            path=config_path,
            rules=len(self._rules),
        )

    @staticmethod
    def _parse_rule(raw: Any, idx: int) -> _RoutingRule:
        """Parse a single rule dict from the YAML."""
        if not isinstance(raw, dict):
            raise ValueError(f"Rule #{idx} must be a mapping, got {type(raw).__name__}")

        rule_name: str = raw.get("name", f"rule_{idx}")

        conditions = raw.get("conditions", {})
        if not isinstance(conditions, dict):
            raise ValueError(f"Rule {rule_name!r}: 'conditions' must be a mapping")

        raw_strategies = raw.get("strategies", [])
        if not isinstance(raw_strategies, list):
            raise ValueError(f"Rule {rule_name!r}: 'strategies' must be a list")

        strategies = [
            RoutingConfig._parse_strategy(s, rule_name, sidx)
            for sidx, s in enumerate(raw_strategies)
        ]

        return _RoutingRule(
            rule_name=rule_name,
            conditions=dict(conditions),
            strategies=strategies,
        )

    @staticmethod
    def _parse_strategy(raw: Any, rule_name: str, idx: int) -> StrategyConfig:
        """Parse a single strategy entry within a rule."""
        if not isinstance(raw, dict):
            raise ValueError(f"Rule {rule_name!r}, strategy #{idx} must be a mapping")

        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"Rule {rule_name!r}, strategy #{idx}: 'name' must be a non-empty string"
            )

        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"Rule {rule_name!r}, strategy {name!r}: 'params' must be a mapping")

        return StrategyConfig(name=name, params=dict(params))

    @staticmethod
    def _matches(rule_conditions: dict[str, Any], context: dict[str, Any]) -> bool:
        """Return True when all rule conditions are satisfied by the context.

        An empty *rule_conditions* dict matches any context (fallback rule).
        """
        for key, expected in rule_conditions.items():
            if context.get(key) != expected:
                return False
        return True
