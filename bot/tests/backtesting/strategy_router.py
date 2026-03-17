"""
StrategyRouter — mirrors StrategySelector routing logic for backtests.

Uses RoutingConfig as the single source of truth for strategy selection rules,
replacing the hard-coded _REGIME_TO_STRATEGIES dict that previously diverged from
the live-bot's StrategySelector.

The router produces a set of active strategy *names* (str) per bar, with a
cooldown guard to prevent rapid oscillation — exactly as the live bot does.

V2: Two-phase PRE_SWITCH gate mirrors StrategySelector (issue #371 follow-up).
The gate requires a timer + optional SMC structural signal (BOS/CHoCH) before
confirming a regime transition — eliminating the live-vs-backtest routing
divergence where the live bot would hold strategies longer during transitions.

Usage::

    from bot.orchestrator.routing_config import RoutingConfig

    cfg = RoutingConfig("configs/strategy_routing.yaml")
    router = StrategyRouter(routing_config=cfg, cooldown_bars=2)
    event = router.on_bar(regime_analysis, current_bar=i, current_timestamp=ts)
    active_strategies = event.active_strategies
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.routing_config import RoutingConfig, StrategyConfig
from bot.orchestrator.strategy_selector import (
    TRANSITION_SMC_REQUIREMENTS,
    TRANSITION_TIMERS,
    TransitionPhase,
)

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
    pre_switch_active: bool = False  # True when PRE_SWITCH gate is blocking
    pre_switch_elapsed_s: float = 0.0  # seconds elapsed in current PRE_SWITCH


class StrategyRouter:
    """
    Stateful strategy router for backtesting.

    Uses :class:`~bot.orchestrator.routing_config.RoutingConfig` as the single
    source of truth for strategy selection rules, mirroring the production
    ``StrategySelector`` (issue #371).

    The same ``_build_routing_conditions`` logic is used as in
    ``StrategySelector._build_routing_conditions()`` so both live and backtest
    produce identical strategy sets for any given ``RegimeAnalysis``.

    V2: Two-phase PRE_SWITCH gate (mirrors StrategySelector, issue #360).
    When ``enable_pre_switch_gate=True`` (default), regime transitions require
    both a timer and an optional SMC structural signal to be confirmed before
    strategies are switched.  This eliminates the live-vs-backtest divergence
    where the backtest switched strategies immediately while the live bot waited.

    Args:
        routing_config:              RoutingConfig instance loaded from
                                     ``configs/strategy_routing.yaml``.
        cooldown_bars:               Minimum number of bars that must pass
                                     between two strategy switches.
        initial_strategies:          Override the bootstrap set (used before
                                     the first regime analysis arrives).
                                     Defaults to ``{"grid", "dca"}``.
        enable_pre_switch_gate:      When True (default), use the two-phase
                                     PRE_SWITCH gate before each transition.
                                     Set False to restore the old single-phase
                                     cooldown-only behaviour.
        require_smc_confirmation:    When True (default), transitions that have
                                     a non-None SMC requirement in
                                     TRANSITION_SMC_REQUIREMENTS also need a
                                     BOS/CHoCH signal from RegimeAnalysis.
        pre_switch_duration_override: Override all TRANSITION_TIMERS with a
                                     single fixed value (seconds).  Useful for
                                     unit tests (pass 0.0 for instant gate).
    """

    def __init__(
        self,
        routing_config: RoutingConfig,
        cooldown_bars: int = 2,
        initial_strategies: set[str] | None = None,
        enable_pre_switch_gate: bool = True,
        require_smc_confirmation: bool = True,
        pre_switch_duration_override: float | None = None,
    ) -> None:
        self._routing_config = routing_config
        self.cooldown_bars = cooldown_bars
        self._enable_pre_switch_gate = enable_pre_switch_gate
        self._require_smc_confirmation = require_smc_confirmation
        self._pre_switch_duration_override = pre_switch_duration_override

        self._initial_strategies: set[str] = (
            initial_strategies.copy() if initial_strategies is not None else {"grid", "dca"}
        )
        self._active_strategies: set[str] = self._initial_strategies.copy()
        self._last_switch_bar: int = -cooldown_bars  # allow switch on bar 0
        self._switch_history: list[dict[str, Any]] = []

        # Two-phase gate state (mirrors StrategySelector)
        self._current_regime: MarketRegime | None = None
        self._transition_phase: TransitionPhase = TransitionPhase.STABLE
        self._pre_switch_target_regime: MarketRegime | None = None
        self._pre_switch_started_at: datetime | None = None
        self._pre_switch_smc_confirmed: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def on_bar(
        self,
        regime: RegimeAnalysis | None,
        current_bar: int,
        current_timestamp: datetime | None = None,
    ) -> StrategyRouterEvent:
        """
        Process one bar and return the current active strategy set.

        Args:
            regime:            Latest regime analysis (None → no regime yet).
            current_bar:       Current bar index (used for cooldown tracking).
            current_timestamp: Timestamp of the current bar.  Required when
                               ``enable_pre_switch_gate=True`` for timer-based
                               gate evaluation.  When None and the gate is
                               active, the gate timer never expires (safe
                               default — transitions only fire via SMC signal
                               or if the timer requirement is 0).

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
            # -------------------------------------------------------
            # Two-phase PRE_SWITCH gate (only after first regime seen)
            # REDUCE_EXPOSURE bypasses the gate — it is an emergency signal
            # that must take effect immediately (mirrors StrategySelector).
            # -------------------------------------------------------
            is_reduce_exposure = regime.recommended_strategy == RecommendedStrategy.REDUCE_EXPOSURE
            if (
                self._enable_pre_switch_gate
                and self._current_regime is not None
                and not is_reduce_exposure
            ):
                ts = current_timestamp or datetime.now(timezone.utc)
                gate_result = self._apply_two_phase_gate(
                    regime=regime,
                    now=ts,
                    prev=prev,
                )
                if gate_result is not None:
                    return gate_result

            # -------------------------------------------------------
            # Legacy cooldown guard (skipped for REDUCE_EXPOSURE)
            # -------------------------------------------------------
            bars_since_switch = current_bar - self._last_switch_bar
            if prev and bars_since_switch < self.cooldown_bars and not is_reduce_exposure:
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
            self._current_regime = regime.regime
            self._reset_pre_switch()

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
        else:
            # Same target — regime stable or returning to current
            if self._transition_phase == TransitionPhase.PRE_SWITCH:
                self._cancel_pre_switch("Regime returned to stable; no transition needed")
            self._current_regime = regime.regime

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
        current_timestamp: datetime | None = None,
    ) -> set[str]:
        """Convenience wrapper — returns only the active strategy set."""
        return self.on_bar(regime, current_bar, current_timestamp).active_strategies

    def reset(self) -> None:
        """Reset router state (use between independent backtest runs)."""
        self._active_strategies = self._initial_strategies.copy()
        self._last_switch_bar = -self.cooldown_bars
        self._switch_history.clear()
        self._current_regime = None
        self._reset_pre_switch()

    @property
    def switch_history(self) -> list[dict[str, Any]]:
        """List of all strategy switches recorded during a backtest run."""
        return list(self._switch_history)

    # ------------------------------------------------------------------
    # Two-phase gate helpers (mirrors StrategySelector)
    # ------------------------------------------------------------------

    def _apply_two_phase_gate(
        self,
        regime: RegimeAnalysis,
        now: datetime,
        prev: set[str],
    ) -> StrategyRouterEvent | None:
        """
        Evaluate the two-phase PRE_SWITCH / CONFIRMED gate.

        Returns a *blocking* StrategyRouterEvent when the gate has not yet
        been cleared, or ``None`` when the gate is cleared and the caller
        should proceed with a normal transition.

        Mirrors ``StrategySelector._apply_two_phase_gate()``.
        """
        from_regime = self._current_regime  # guaranteed non-None by caller
        target_regime = regime.regime

        if self._transition_phase == TransitionPhase.STABLE:
            # First detection of a potential regime change → enter PRE_SWITCH
            self._enter_pre_switch(target=target_regime, now=now)
            elapsed = 0.0
            logger.info(
                "pre_switch_entered",
                extra={
                    "from_regime": from_regime.value if from_regime else None,
                    "to_regime": target_regime.value,
                    "require_smc": self._require_smc_confirmation,
                },
            )
            return StrategyRouterEvent(
                active_strategies=prev.copy(),
                activated=set(),
                deactivated=set(),
                cooldown_remaining=0,
                regime_value=regime.regime.value,
                recommendation=regime.recommended_strategy.value,
                pre_switch_active=True,
                pre_switch_elapsed_s=elapsed,
            )

        if self._transition_phase == TransitionPhase.PRE_SWITCH:
            if target_regime != self._pre_switch_target_regime:
                # Target regime changed — restart PRE_SWITCH for new target
                self._cancel_pre_switch(
                    f"Target regime changed from {self._pre_switch_target_regime} to {target_regime}"
                )
                self._enter_pre_switch(target=target_regime, now=now)
                return StrategyRouterEvent(
                    active_strategies=prev.copy(),
                    activated=set(),
                    deactivated=set(),
                    cooldown_remaining=0,
                    regime_value=regime.regime.value,
                    recommendation=regime.recommended_strategy.value,
                    pre_switch_active=True,
                    pre_switch_elapsed_s=0.0,
                )

            # Update SMC confirmation from latest analysis
            if not self._pre_switch_smc_confirmed:
                self._pre_switch_smc_confirmed = self._check_smc_signal(
                    from_regime=from_regime,
                    to_regime=target_regime,
                    analysis=regime,
                )

            timer_ok = self._check_pre_switch_timer(
                from_regime=from_regime,
                to_regime=target_regime,
                now=now,
            )
            smc_ok = (not self._require_smc_confirmation) or self._pre_switch_smc_confirmed

            if timer_ok and smc_ok:
                # CONFIRMED → signal the caller to proceed with the transition
                self._transition_phase = TransitionPhase.CONFIRMED
                logger.info(
                    "pre_switch_confirmed",
                    extra={
                        "from_regime": from_regime.value if from_regime else None,
                        "to_regime": target_regime.value,
                        "smc_confirmed": self._pre_switch_smc_confirmed,
                    },
                )
                return None  # let on_bar execute the switch

            elapsed = (
                (now - self._pre_switch_started_at).total_seconds()
                if self._pre_switch_started_at
                else 0.0
            )
            required = self._get_pre_switch_timer(from_regime=from_regime, to_regime=target_regime)
            logger.debug(
                "pre_switch_waiting",
                extra={
                    "from_regime": from_regime.value if from_regime else None,
                    "to_regime": target_regime.value,
                    "elapsed_s": elapsed,
                    "required_s": required,
                    "timer_ok": timer_ok,
                    "smc_ok": smc_ok,
                },
            )
            return StrategyRouterEvent(
                active_strategies=prev.copy(),
                activated=set(),
                deactivated=set(),
                cooldown_remaining=0,
                regime_value=regime.regime.value,
                recommendation=regime.recommended_strategy.value,
                pre_switch_active=True,
                pre_switch_elapsed_s=elapsed,
            )

        # CONFIRMED or TRANSITIONING — proceed with the transition
        return None

    def _enter_pre_switch(self, target: MarketRegime, now: datetime) -> None:
        self._transition_phase = TransitionPhase.PRE_SWITCH
        self._pre_switch_target_regime = target
        self._pre_switch_started_at = now
        self._pre_switch_smc_confirmed = False

    def _cancel_pre_switch(self, reason: str) -> None:
        logger.info(
            "pre_switch_cancelled",
            extra={
                "reason": reason,
                "target": (
                    self._pre_switch_target_regime.value if self._pre_switch_target_regime else None
                ),
            },
        )
        self._reset_pre_switch()

    def _reset_pre_switch(self) -> None:
        self._transition_phase = TransitionPhase.STABLE
        self._pre_switch_target_regime = None
        self._pre_switch_started_at = None
        self._pre_switch_smc_confirmed = False

    def _get_pre_switch_timer(
        self, from_regime: MarketRegime | None, to_regime: MarketRegime
    ) -> float:
        if self._pre_switch_duration_override is not None:
            return self._pre_switch_duration_override
        if from_regime is not None:
            key = (from_regime, to_regime)
            if key in TRANSITION_TIMERS:
                return TRANSITION_TIMERS[key]  # type: ignore[return-value]
        return TRANSITION_TIMERS.get("default", 1800.0)  # type: ignore[return-value]

    def _check_pre_switch_timer(
        self, from_regime: MarketRegime | None, to_regime: MarketRegime, now: datetime
    ) -> bool:
        if self._pre_switch_started_at is None:
            return False
        required = self._get_pre_switch_timer(from_regime=from_regime, to_regime=to_regime)
        elapsed = (now - self._pre_switch_started_at).total_seconds()
        return elapsed >= required

    def _check_smc_signal(
        self,
        from_regime: MarketRegime | None,
        to_regime: MarketRegime,
        analysis: RegimeAnalysis,
    ) -> bool:
        """Return True when the required SMC structural signal is present."""
        if from_regime is not None:
            key = (from_regime, to_regime)
            required_signal = TRANSITION_SMC_REQUIREMENTS.get(
                key,  # type: ignore[arg-type]
                TRANSITION_SMC_REQUIREMENTS.get("default"),
            )
        else:
            required_signal = TRANSITION_SMC_REQUIREMENTS.get("default")

        if required_signal is None:
            return True

        detected = analysis.analysis_details.get("smc_signal")
        return detected == required_signal

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
