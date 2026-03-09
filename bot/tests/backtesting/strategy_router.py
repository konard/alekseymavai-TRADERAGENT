"""
StrategyRouter — mirrors StrategySelector routing logic for backtests.

Uses RoutingConfig as the single source of truth for strategy selection rules,
replacing the hard-coded _REGIME_TO_STRATEGIES dict that previously diverged from
the live-bot's StrategySelector.

The router produces a set of active strategy *names* (str) per bar, with a
cooldown guard to prevent rapid oscillation — exactly as the live bot does.

Usage::

    from bot.orchestrator.routing_config import RoutingConfig

    cfg = RoutingConfig("configs/strategy_routing.yaml")
    router = StrategyRouter(routing_config=cfg, cooldown_bars=2)
    event = router.on_bar(regime_analysis, current_bar=i)
    active_strategies = event.active_strategies
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bot.orchestrator.market_regime import (
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.routing_config import RoutingConfig, StrategyConfig

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

    Uses :class:`~bot.orchestrator.routing_config.RoutingConfig` as the single
    source of truth for strategy selection rules, mirroring the production
    ``StrategySelector`` (issue #371).

    The same ``_build_routing_conditions`` logic is used as in
    ``StrategySelector._build_routing_conditions()`` so both live and backtest
    produce identical strategy sets for any given ``RegimeAnalysis``.

    Args:
        routing_config:        RoutingConfig instance loaded from
                               ``configs/strategy_routing.yaml``.
        cooldown_bars:         Minimum number of bars that must pass between two
                               strategy switches (equivalent to cooldown_seconds=600
                               at 10 bars/minute → 2 bars for M5 data).
        initial_strategies:    Override the bootstrap set (used before the first
                               regime analysis arrives). Defaults to ``{"grid", "dca"}``.
    """

    def __init__(
        self,
        routing_config: RoutingConfig,
        cooldown_bars: int = 2,
        initial_strategies: set[str] | None = None,
    ) -> None:
        self._routing_config = routing_config
        self.cooldown_bars = cooldown_bars

        self._initial_strategies: set[str] = (
            initial_strategies.copy() if initial_strategies is not None else {"grid", "dca"}
        )
        self._active_strategies: set[str] = self._initial_strategies.copy()
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
            # No regime data yet — keep everything active (bootstrap)
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
                    extra={
                        "cooldown_remaining": cooldown_remaining,
                        "current": sorted(prev),
                        "wanted": sorted(target),
                    },
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
                extra={
                    "bar": current_bar,
                    "activated": sorted(activated),
                    "deactivated": sorted(deactivated),
                    "regime": regime.regime.value,
                },
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
        self._active_strategies = self._initial_strategies.copy()
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

        Delegates to :meth:`RoutingConfig.get_strategies` using the same
        condition-building logic as ``StrategySelector._build_routing_conditions``
        so that live and backtest routing always produce identical results.

        Special cases (mirrors StrategySelector._get_target_strategies):
        - REDUCE_EXPOSURE → empty set (no strategies active)
        - HOLD            → keep current active strategies unchanged
        """
        recommended = regime.recommended_strategy

        # REDUCE_EXPOSURE: no strategies active — always takes precedence
        if recommended == RecommendedStrategy.REDUCE_EXPOSURE:
            return set()

        # HOLD: keep whatever is currently active
        if recommended == RecommendedStrategy.HOLD:
            return self._active_strategies.copy()

        # Build conditions dict the same way as StrategySelector
        conditions = self._build_routing_conditions(regime)
        strategy_configs: list[StrategyConfig] = self._routing_config.get_strategies(conditions)
        return {sc.name for sc in strategy_configs}

    @staticmethod
    def _build_routing_conditions(regime: RegimeAnalysis) -> dict[str, Any]:
        """
        Build the conditions dict passed to :meth:`RoutingConfig.get_strategies`.

        Exactly mirrors ``StrategySelector._build_routing_conditions`` so that
        live and backtest routing share the same matching logic.

        Keys produced:
        - ``market_regime``:   regime value string (e.g. ``"bull_trend"``)
        - ``confluence_high``: ``True`` when ``confluence_score >= 0.7`` or
                               ``recommended == HYBRID``
        - ``adx_high``:        ``True`` when ``adx > 25.0`` (mirrors
                               HybridCoordinator.adx_dca_threshold)
        """
        conditions: dict[str, Any] = {"market_regime": regime.regime.value}
        if regime.recommended_strategy == RecommendedStrategy.HYBRID or (
            regime.confluence_score >= 0.7
        ):
            conditions["confluence_high"] = True
        # ADX-based Grid/DCA coordination — mirrors HybridCoordinator.evaluate()
        if regime.adx is not None and regime.adx > 25.0:
            conditions["adx_high"] = True
        return conditions
