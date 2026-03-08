"""
StrategyRouter — unified strategy routing for backtests (issue #368 / #371).

Uses the same ``RoutingConfig`` (``configs/strategy_routing.yaml``) as the
live ``StrategySelector``, so Live and Backtest routing decisions are always
derived from the same single source of truth.

Key behaviour:
- ``on_bar()`` maps ``RegimeAnalysis.regime`` → active strategy set via
  ``RoutingConfig.get_strategies()``.
- HYBRID recommendation is handled via ``RoutingConfig.get_hybrid_weights()``.
- REDUCE_EXPOSURE / HOLD are honoured the same way as the live selector.
- A bar-based cooldown prevents rapid strategy oscillation (mirrors the
  live bot's ``transition_cooldown_seconds`` gate).

Usage::

    router = StrategyRouter()                        # default routing YAML
    router = StrategyRouter(routing_config=my_cfg)   # custom config

    event = router.on_bar(regime_analysis, current_bar=i)
    active = event.active_strategies   # set[str]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bot.orchestrator.market_regime import (
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.routing_config import RoutingConfig

logger = logging.getLogger(__name__)


@dataclass
class StrategyRouterEvent:
    """Result of processing one bar through the router."""

    active_strategies: set[str]
    activated: set[str]  # newly activated this bar
    deactivated: set[str]  # newly deactivated this bar
    cooldown_remaining: int  # bars left in cooldown (0 if not cooling down)
    regime_value: str  # regime name for logging
    recommendation: str  # recommended strategy name


class StrategyRouter:
    """
    Stateful strategy router for backtesting.

    Uses ``RoutingConfig`` (loaded from ``configs/strategy_routing.yaml``) so
    that routing decisions are identical to those made by the live
    ``StrategySelector``.

    Args:
        cooldown_bars:    Minimum bars between two strategy switches.
        routing_config:   Explicit RoutingConfig instance.  When *None* the
                          default ``configs/strategy_routing.yaml`` is loaded.
    """

    def __init__(
        self,
        cooldown_bars: int = 60,
        routing_config: RoutingConfig | None = None,
    ) -> None:
        self.cooldown_bars = cooldown_bars
        self._routing_config: RoutingConfig = (
            routing_config if routing_config is not None else RoutingConfig()
        )

        self._active_strategies: set[str] = set()  # empty until first regime is known
        self._last_switch_bar: int = -cooldown_bars  # allow switch on bar 0
        self._switch_history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def on_bar(
        self,
        regime: RegimeAnalysis | None,
        current_bar: int,
    ) -> StrategyRouterEvent:
        """
        Process one bar and return the current active strategy set.

        Args:
            regime:      Latest regime analysis (None → no regime yet).
            current_bar: Current bar index (used for cooldown tracking).

        Returns:
            StrategyRouterEvent with full routing state.
        """
        if regime is None:
            return StrategyRouterEvent(
                active_strategies=self._active_strategies.copy(),
                activated=set(),
                deactivated=set(),
                cooldown_remaining=0,
                regime_value="unknown",
                recommendation="none",
            )

        target = self._compute_target_strategies(regime)
        prev = self._active_strategies

        activated: set[str] = set()
        deactivated: set[str] = set()
        cooldown_remaining = 0

        if target != prev:
            bars_since_switch = current_bar - self._last_switch_bar
            if prev and bars_since_switch < self.cooldown_bars:
                # Cooldown active — block the switch
                cooldown_remaining = self.cooldown_bars - bars_since_switch
                logger.debug(
                    "strategy_switch_blocked_by_cooldown",
                    cooldown_remaining=cooldown_remaining,
                    current=sorted(prev),
                    wanted=sorted(target),
                )
                return StrategyRouterEvent(
                    active_strategies=prev.copy(),
                    activated=set(),
                    deactivated=set(),
                    cooldown_remaining=cooldown_remaining,
                    regime_value=regime.regime.value,
                    recommendation=regime.recommended_strategy.value,
                )

            # Execute the switch
            activated = target - prev
            deactivated = prev - target
            self._last_switch_bar = current_bar
            self._active_strategies = target.copy()

            self._switch_history.append(
                {
                    "bar": current_bar,
                    "from": sorted(prev),
                    "to": sorted(target),
                    "activated": sorted(activated),
                    "deactivated": sorted(deactivated),
                    "regime": regime.regime.value,
                    "recommendation": regime.recommended_strategy.value,
                }
            )

            logger.debug(
                "strategy_switch_executed",
                bar=current_bar,
                activated=sorted(activated),
                deactivated=sorted(deactivated),
                regime=regime.regime.value,
            )

        return StrategyRouterEvent(
            active_strategies=self._active_strategies.copy(),
            activated=activated,
            deactivated=deactivated,
            cooldown_remaining=0,
            regime_value=regime.regime.value,
            recommendation=regime.recommended_strategy.value,
        )

    def get_active_strategies(
        self,
        regime: RegimeAnalysis | None,
        current_bar: int,
    ) -> set[str]:
        """Convenience wrapper — returns only the active strategy set."""
        return self.on_bar(regime, current_bar).active_strategies

    def reset(self) -> None:
        """Reset router state (use between independent backtest runs)."""
        self._active_strategies = set()
        self._last_switch_bar = -self.cooldown_bars
        self._switch_history.clear()

    @property
    def switch_history(self) -> list[dict[str, Any]]:
        """List of all strategy switches recorded during a backtest run."""
        return list(self._switch_history)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_target_strategies(self, regime: RegimeAnalysis) -> set[str]:
        """
        Compute the desired strategy set for a given regime.

        Delegates to ``RoutingConfig.get_strategies()`` with special handling
        for HYBRID, REDUCE_EXPOSURE, and HOLD recommendations — matching the
        live ``StrategySelector._get_target_strategies()`` logic exactly.
        """
        recommended = regime.recommended_strategy

        # HYBRID: use hybrid_weights from RoutingConfig
        if recommended == RecommendedStrategy.HYBRID:
            return {sc.type for sc in self._routing_config.get_hybrid_weights()}

        # REDUCE_EXPOSURE: no active strategies
        if recommended == RecommendedStrategy.REDUCE_EXPOSURE:
            return set()

        # HOLD: keep current strategies unchanged
        if recommended == RecommendedStrategy.HOLD:
            return self._active_strategies.copy()

        # Standard regime lookup
        configs = self._routing_config.get_strategies({"market_regime": regime.regime})
        return {sc.type for sc in configs}
