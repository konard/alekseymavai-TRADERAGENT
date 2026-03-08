"""
RoutingConfig — Unified strategy routing configuration loader.

Reads ``configs/strategy_routing.yaml`` and provides a single method
``get_strategies(conditions)`` that both the live ``StrategySelector`` and
the backtest ``StrategyRouter`` can call to obtain the active strategy list
for any given market condition.

This is the foundation of Epic #1 (issue #368): it ensures that Live and
Backtest routing decisions always share the same source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from bot.utils.logger import get_logger

logger = get_logger(__name__)

# Default path relative to the repository root
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "strategy_routing.yaml"


@dataclass(frozen=True)
class StrategyConfig:
    """A single strategy entry from the routing config."""

    type: str  # e.g. "grid", "dca", "trend_follower", "smc"
    weight: float  # 0.0 – 1.0 allocation weight
    priority: int  # lower = higher priority for signal conflict resolution


class RoutingConfig:
    """
    Loads and queries the unified strategy routing configuration.

    Usage::

        cfg = RoutingConfig()                         # loads default YAML
        cfg = RoutingConfig("/path/to/custom.yaml")   # custom path

        strategies = cfg.get_strategies({"market_regime": "bull_trend"})
        # → [StrategyConfig(type="trend_follower", weight=0.7, priority=1),
        #    StrategyConfig(type="dca", weight=0.3, priority=2)]

        hybrid = cfg.get_hybrid_weights()
        # → [StrategyConfig(type="dca", ...), ...]

    Error handling:
        - ``FileNotFoundError`` if the YAML file is not found.
        - ``ValueError``       if the YAML structure is invalid.
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        """
        Load the routing configuration from a YAML file.

        Args:
            config_path: Path to the routing YAML.  When *None* the default
                location (``configs/strategy_routing.yaml`` at the repository
                root) is used.

        Raises:
            FileNotFoundError: If the YAML file does not exist.
            ValueError:        If the YAML is malformed or missing required keys.
        """
        path = Path(config_path) if config_path is not None else _DEFAULT_CONFIG_PATH
        self._config_path = path
        self._raw = self._load(path)
        raw_rules: list[dict[str, Any]] = self._raw.get("rules", [])
        # Validate all rule strategy lists at load time so errors are caught early
        self._rules: list[dict[str, Any]] = self._validate_rules(raw_rules)
        self._fallback: list[StrategyConfig] = self._parse_strategies(
            self._raw.get("fallback", {}).get("strategies", [])
        )
        self._hybrid_weights: list[StrategyConfig] = self._parse_strategies(
            self._raw.get("hybrid_weights", [])
        )
        logger.info(
            "routing_config_loaded",
            path=str(path),
            rules_count=len(self._rules),
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def get_strategies(self, conditions: dict[str, Any]) -> list[StrategyConfig]:
        """
        Return the list of strategies for the given market conditions.

        Rules are evaluated in declaration order; the first matching rule wins.
        If no rule matches, the *fallback* list is returned (default: empty).

        Args:
            conditions: Dict of condition key-value pairs, e.g.::

                {"market_regime": "bull_trend"}
                {"market_regime": "tight_range", "recommended_strategy": "grid"}

        Returns:
            Ordered list of ``StrategyConfig`` objects for this condition set.
        """
        for rule in self._rules:
            rule_conditions: dict[str, Any] = rule.get("conditions", {})
            if self._matches(rule_conditions, conditions):
                strategies = self._parse_strategies(rule.get("strategies", []))
                logger.debug(
                    "routing_rule_matched",
                    rule=rule.get("name", "<unnamed>"),
                    conditions=conditions,
                    strategies=[s.type for s in strategies],
                )
                return strategies

        logger.debug(
            "routing_fallback_used",
            conditions=conditions,
        )
        return list(self._fallback)

    def get_hybrid_weights(self) -> list[StrategyConfig]:
        """Return the hybrid-mode strategy weights from the config."""
        return list(self._hybrid_weights)

    @property
    def rules(self) -> list[dict[str, Any]]:
        """Raw rule list (read-only copy)."""
        return list(self._rules)

    @property
    def config_path(self) -> Path:
        """Path to the loaded YAML file."""
        return self._config_path

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    @classmethod
    def _validate_rules(cls, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate all rule strategy entries eagerly to surface errors at load time."""
        for rule in rules:
            cls._parse_strategies(rule.get("strategies", []))
        return rules

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        """Load and parse the YAML file."""
        if not path.exists():
            raise FileNotFoundError(
                f"Strategy routing config not found: {path}\n" f"Expected at: {path.resolve()}"
            )
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(
                f"Routing config must be a YAML mapping (dict), got {type(data).__name__}"
            )
        return data

    @staticmethod
    def _parse_strategies(raw: list[dict[str, Any]]) -> list[StrategyConfig]:
        """Convert a list of raw strategy dicts into StrategyConfig objects."""
        result: list[StrategyConfig] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError(f"Strategy entry must be a mapping, got: {item!r}")
            try:
                result.append(
                    StrategyConfig(
                        type=str(item["type"]),
                        weight=float(item.get("weight", 1.0)),
                        priority=int(item.get("priority", 1)),
                    )
                )
            except KeyError as exc:
                raise ValueError(f"Strategy entry missing required key {exc}: {item!r}") from exc
        return result

    @staticmethod
    def _as_str(value: Any) -> Any:
        """Convert a value to its string form; enum values are unwrapped via .value."""
        if value is None:
            return None
        if hasattr(value, "value"):
            return value.value
        return value

    @staticmethod
    def _matches(rule_conditions: dict[str, Any], input_conditions: dict[str, Any]) -> bool:
        """Return True when all key-value pairs in *rule_conditions* are present in *input_conditions*."""
        for key, expected in rule_conditions.items():
            actual = input_conditions.get(key)
            # Support both enum values (objects with .value) and plain strings
            actual_str = RoutingConfig._as_str(actual)
            expected_str = RoutingConfig._as_str(expected)
            if actual_str != expected_str:
                return False
        return True
