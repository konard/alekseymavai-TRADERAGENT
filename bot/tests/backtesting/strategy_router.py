"""
StrategyRouter — mirrors BotOrchestrator._update_active_strategies() for backtests.

Maps market regime → active strategy set with a cooldown guard to prevent
rapid oscillation, exactly as the live bot does.

Usage::

    router = StrategyRouter(cooldown_bars=60)
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

logger = logging.getLogger(__name__)


# Mirrors BotOrchestrator._REGIME_TO_STRATEGIES
_REGIME_TO_STRATEGIES: dict[RecommendedStrategy, set[str]] = {
    RecommendedStrategy.GRID: {"grid"},
    RecommendedStrategy.DCA: {"dca"},
    RecommendedStrategy.HYBRID: {"grid", "dca"},
    RecommendedStrategy.HOLD: set(),
    RecommendedStrategy.REDUCE_EXPOSURE: set(),
}

# TrendFollower is the PRIMARY strategy for bull trends — replaces Grid/DCA
_TF_PRIMARY_REGIMES = {"bull_trend"}

# DCA is the PRIMARY strategy for bear trends
_DCA_PRIMARY_REGIMES = {"bear_trend"}

# SMC activates only for breakout/volatile — exclusive, not additive
_SMC_PRIMARY_REGIMES = {"volatile_transition", "breakout"}


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

    Mirrors the production logic in BotOrchestrator._update_active_strategies()
    — including cooldown, trending-regime bonuses for trend_follower / SMC,
    and the "no regime yet → enable everything" bootstrap behaviour.

    Args:
        cooldown_bars:  Minimum number of bars that must pass between two
                        strategy switches (equivalent to cooldown_seconds=600
                        at 10 bars/minute → 60 bars for M5 data).
        enable_smc:     Whether to allow SMC strategy activation (default: False
                        to match OrchestratorBacktestConfig.enable_smc=False).
        enable_trend_follower: Whether to allow trend_follower activation.
    """

    def __init__(
        self,
        cooldown_bars: int = 60,
        enable_smc: bool = False,
        enable_trend_follower: bool = True,
    ) -> None:
        self.cooldown_bars = cooldown_bars
        self.enable_smc = enable_smc
        self.enable_trend_follower = enable_trend_follower

        self._initial_strategies: set[str] = {"grid", "dca"}  # default "all active" bootstrap
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
            # No regime data yet — keep everything active (backward-compat)
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
        Compute the desired strategy set for a given regime — ADDITIVE routing.

        Steps:
        1. Start with base set from _REGIME_TO_STRATEGIES (grid/dca/etc.)
        2. For DCA-primary regimes (bear_trend): use {dca} as base
        3. For TF-primary regimes (bull_trend): add trend_follower if enabled
        4. For SMC-primary regimes (breakout/volatile): add smc if enabled
        """
        rv = regime.regime.value

        # Step 1 & 2: determine base set
        if rv in _DCA_PRIMARY_REGIMES:
            base = {"dca"}
        else:
            base = _REGIME_TO_STRATEGIES.get(regime.recommended_strategy, set()).copy()

        # Step 3: add trend_follower for trending regimes
        if self.enable_trend_follower and rv in _TF_PRIMARY_REGIMES:
            base.add("trend_follower")

        # Step 4: add smc for volatile/breakout regimes
        if self.enable_smc and rv in _SMC_PRIMARY_REGIMES:
            base.add("smc")
        # Also add smc for trending regimes when enabled
        elif self.enable_smc and rv in _TF_PRIMARY_REGIMES:
            base.add("smc")

        return base
