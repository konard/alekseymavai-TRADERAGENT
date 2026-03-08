"""
StrategySelector - Routes market signals to appropriate strategies based on regime.

Responsibilities:
- Map market regimes to strategy configurations with priorities
- Manage smooth transitions between strategies (cooldown, min duration)
- Resolve overlapping signal conflicts via priority ranking
- Track transition history for analysis
- Two-phase PRE_SWITCH / CONFIRMED logic with SMC confirmation (issue #360)
- Route via RoutingConfig (issue #370) when provided
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from bot.orchestrator.market_regime import (
    MarketRegime,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.routing_config import RoutingConfig, StrategyConfig
from bot.orchestrator.strategy_registry import (
    StrategyRegistry,
    StrategyState,
)
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class TransitionState(str, Enum):
    """State of a strategy transition."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    COOLDOWN = "cooldown"


class TransitionPhase(str, Enum):
    """Two-phase transition state for regime switching (issue #360).

    STABLE
        Current regime is stable; strategies run normally.

    PRE_SWITCH
        A potential regime change was detected.  New positions are blocked;
        existing positions are held.  Both a timer *and* an SMC structural
        confirmation (BOS / CHoCH) must be satisfied before advancing.

    CONFIRMED
        Both criteria (timer + SMC signal) are met.  The transition has been
        approved and will execute on the next orchestrator tick.

    TRANSITIONING
        Graceful hand-off in progress: new strategies warming up while old
        strategies close existing positions before being deactivated.
    """

    STABLE = "stable"
    PRE_SWITCH = "pre_switch"
    CONFIRMED = "confirmed"
    TRANSITIONING = "transitioning"


# ---------------------------------------------------------------------------
# Per-transition-type confirmation requirements
# ---------------------------------------------------------------------------

# Minimum time (seconds) a PRE_SWITCH phase must persist before a transition
# type is eligible for confirmation.  Keyed by (from_regime, to_regime) pairs;
# falls back to the default entry when no specific rule matches.
_DEFAULT_PRE_SWITCH_SECONDS = 1800.0  # 30 min default

TRANSITION_TIMERS: dict[tuple[MarketRegime, MarketRegime] | str, float] = {
    # Range → Trend: 30 min
    (MarketRegime.TIGHT_RANGE, MarketRegime.BULL_TREND): 1800.0,
    (MarketRegime.TIGHT_RANGE, MarketRegime.BEAR_TREND): 1800.0,
    (MarketRegime.WIDE_RANGE, MarketRegime.BULL_TREND): 1800.0,
    (MarketRegime.WIDE_RANGE, MarketRegime.BEAR_TREND): 1800.0,
    # Trend → Range: 45 min
    (MarketRegime.BULL_TREND, MarketRegime.TIGHT_RANGE): 2700.0,
    (MarketRegime.BULL_TREND, MarketRegime.WIDE_RANGE): 2700.0,
    (MarketRegime.BEAR_TREND, MarketRegime.TIGHT_RANGE): 2700.0,
    (MarketRegime.BEAR_TREND, MarketRegime.WIDE_RANGE): 2700.0,
    # Trend → Counter-trend: 60 min
    (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND): 3600.0,
    (MarketRegime.BEAR_TREND, MarketRegime.BULL_TREND): 3600.0,
    # Any → ACCUMULATION / DISTRIBUTION: 30 min
    (MarketRegime.TIGHT_RANGE, MarketRegime.ACCUMULATION): 1800.0,
    (MarketRegime.WIDE_RANGE, MarketRegime.ACCUMULATION): 1800.0,
    (MarketRegime.BULL_TREND, MarketRegime.ACCUMULATION): 1800.0,
    (MarketRegime.BEAR_TREND, MarketRegime.ACCUMULATION): 1800.0,
    (MarketRegime.TIGHT_RANGE, MarketRegime.DISTRIBUTION): 1800.0,
    (MarketRegime.WIDE_RANGE, MarketRegime.DISTRIBUTION): 1800.0,
    (MarketRegime.BULL_TREND, MarketRegime.DISTRIBUTION): 1800.0,
    (MarketRegime.BEAR_TREND, MarketRegime.DISTRIBUTION): 1800.0,
    # Default fallback
    "default": _DEFAULT_PRE_SWITCH_SECONDS,
}

# SMC confirmation signal types required per transition type.
# Values match the ``smc_signal`` keys returned by RegimeAnalysis.analysis_details.
#   "BOS"   – Break of Structure (trend continuation confirmed)
#   "CHoCH" – Change of Character (trend reversal confirmed)
#   None    – no SMC confirmation required for this pair
TRANSITION_SMC_REQUIREMENTS: dict[tuple[MarketRegime, MarketRegime] | str, str | None] = {
    (MarketRegime.TIGHT_RANGE, MarketRegime.BULL_TREND): "BOS",
    (MarketRegime.WIDE_RANGE, MarketRegime.BULL_TREND): "BOS",
    (MarketRegime.TIGHT_RANGE, MarketRegime.BEAR_TREND): "BOS",
    (MarketRegime.WIDE_RANGE, MarketRegime.BEAR_TREND): "BOS",
    (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND): "CHoCH",
    (MarketRegime.BEAR_TREND, MarketRegime.BULL_TREND): "CHoCH",
    (MarketRegime.BULL_TREND, MarketRegime.TIGHT_RANGE): None,  # absence of BOS over 2h
    (MarketRegime.BULL_TREND, MarketRegime.WIDE_RANGE): None,
    (MarketRegime.BEAR_TREND, MarketRegime.TIGHT_RANGE): None,
    (MarketRegime.BEAR_TREND, MarketRegime.WIDE_RANGE): None,
    (MarketRegime.TIGHT_RANGE, MarketRegime.ACCUMULATION): "CHoCH",
    (MarketRegime.WIDE_RANGE, MarketRegime.ACCUMULATION): "CHoCH",
    (MarketRegime.BULL_TREND, MarketRegime.ACCUMULATION): "CHoCH",
    (MarketRegime.BEAR_TREND, MarketRegime.ACCUMULATION): "CHoCH",
    (MarketRegime.TIGHT_RANGE, MarketRegime.DISTRIBUTION): "CHoCH",
    (MarketRegime.WIDE_RANGE, MarketRegime.DISTRIBUTION): "CHoCH",
    (MarketRegime.BULL_TREND, MarketRegime.DISTRIBUTION): "CHoCH",
    (MarketRegime.BEAR_TREND, MarketRegime.DISTRIBUTION): "CHoCH",
    "default": None,
}


@dataclass
class StrategyWeight:
    """Weight/priority assignment for a strategy type within a regime."""

    strategy_type: str
    weight: float  # 0.0 - 1.0, higher = more allocation
    priority: int  # Lower number = higher priority for signal conflicts


# Default strategy weights per regime (v2.0 — 6 regimes)
DEFAULT_REGIME_STRATEGIES: dict[MarketRegime, list[StrategyWeight]] = {
    MarketRegime.TIGHT_RANGE: [
        StrategyWeight(strategy_type="grid", weight=1.0, priority=1),
    ],
    MarketRegime.WIDE_RANGE: [
        StrategyWeight(strategy_type="grid", weight=1.0, priority=1),
    ],
    MarketRegime.QUIET_TRANSITION: [
        StrategyWeight(strategy_type="grid", weight=0.7, priority=1),
    ],
    MarketRegime.VOLATILE_TRANSITION: [
        StrategyWeight(strategy_type="smc", weight=1.0, priority=1),
    ],
    MarketRegime.BULL_TREND: [
        StrategyWeight(strategy_type="trend_follower", weight=0.7, priority=1),
        StrategyWeight(strategy_type="dca", weight=0.3, priority=2),
    ],
    MarketRegime.BEAR_TREND: [
        StrategyWeight(strategy_type="dca", weight=1.0, priority=1),
    ],
    MarketRegime.UNKNOWN: [],
}

# Hybrid mode adds grid to trending strategies
HYBRID_STRATEGY_WEIGHTS: list[StrategyWeight] = [
    StrategyWeight(strategy_type="dca", weight=0.5, priority=1),
    StrategyWeight(strategy_type="grid", weight=0.3, priority=2),
    StrategyWeight(strategy_type="trend_follower", weight=0.2, priority=3),
]


@dataclass
class TransitionRecord:
    """Record of a strategy transition."""

    from_regime: MarketRegime
    to_regime: MarketRegime
    from_strategies: list[str]  # strategy types that were active
    to_strategies: list[str]  # strategy types to activate
    recommended: RecommendedStrategy
    timestamp: datetime
    state: TransitionState

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "from_regime": self.from_regime.value,
            "to_regime": self.to_regime.value,
            "from_strategies": self.from_strategies,
            "to_strategies": self.to_strategies,
            "recommended": self.recommended.value,
            "timestamp": self.timestamp.isoformat(),
            "state": self.state.value,
        }


@dataclass
class SelectionResult:
    """Result of strategy selection."""

    strategies_to_start: list[StrategyWeight]
    strategies_to_stop: list[str]  # strategy types to deactivate
    strategies_to_keep: list[str]  # strategy types that remain active
    regime: MarketRegime
    recommended: RecommendedStrategy
    transition_needed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "strategies_to_start": [
                {"type": s.strategy_type, "weight": s.weight, "priority": s.priority}
                for s in self.strategies_to_start
            ],
            "strategies_to_stop": self.strategies_to_stop,
            "strategies_to_keep": self.strategies_to_keep,
            "regime": self.regime.value,
            "recommended": self.recommended.value,
            "transition_needed": self.transition_needed,
            "reason": self.reason,
        }


class StrategySelector:
    """
    Selects and manages active strategies based on market regime detection.

    Coordinates with StrategyRegistry for lifecycle management and
    MarketRegimeDetector for regime-based routing decisions.

    Two-phase PRE_SWITCH / CONFIRMED logic (issue #360):
    - On detecting a potential regime change the selector enters PRE_SWITCH.
    - Strategies are NOT switched immediately; new entries are blocked.
    - After the required timer *and* SMC confirmation are satisfied the
      selector advances to CONFIRMED and executes the graceful transition.
    - If the pending regime disappears before confirmation the selector
      returns to STABLE and increments the ``cancelled_pre_switch_count``
      metric for analysis.
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        regime_strategies: dict[MarketRegime, list[StrategyWeight]] | None = None,
        hybrid_weights: list[StrategyWeight] | None = None,
        transition_cooldown_seconds: float = 300.0,
        min_regime_duration_seconds: float = 120.0,
        max_transition_history: int = 50,
        require_smc_confirmation: bool = True,
        pre_switch_duration_seconds: float | None = None,
        tighten_stops_on_pre_switch: bool = True,
        transition_timers: dict[tuple[MarketRegime, MarketRegime] | str, float] | None = None,
        transition_smc_requirements: (
            dict[tuple[MarketRegime, MarketRegime] | str, str | None] | None
        ) = None,
        routing_config: RoutingConfig | None = None,
    ):
        """
        Args:
            registry: Strategy registry for lifecycle management.
            regime_strategies: Custom mapping of regimes to strategy weights.
                Ignored when *routing_config* is supplied.
            hybrid_weights: Custom weights for hybrid mode.
                Ignored when *routing_config* is supplied.
            transition_cooldown_seconds: Minimum time between transitions (default 5 min).
            min_regime_duration_seconds: Minimum regime duration before transition (default 2 min).
            max_transition_history: Max transition records to keep.
            require_smc_confirmation: When True, transitions also require a BOS/CHoCH signal.
            pre_switch_duration_seconds: Override for the default PRE_SWITCH timer (seconds).
                When None the per-transition table (TRANSITION_TIMERS) is used.
            tighten_stops_on_pre_switch: Signal strategies to tighten stops in PRE_SWITCH.
            transition_timers: Custom per-transition timer overrides.
            transition_smc_requirements: Custom per-transition SMC signal requirements.
            routing_config: When supplied, strategy selection is delegated to
                :class:`~bot.orchestrator.routing_config.RoutingConfig` instead of the
                hard-coded *regime_strategies* / *hybrid_weights* dicts (issue #370).
        """
        self._registry = registry
        self._regime_strategies = regime_strategies or DEFAULT_REGIME_STRATEGIES
        self._hybrid_weights = hybrid_weights or HYBRID_STRATEGY_WEIGHTS
        self._transition_cooldown = transition_cooldown_seconds
        self._min_regime_duration = min_regime_duration_seconds
        self._max_history = max_transition_history
        self._require_smc_confirmation = require_smc_confirmation
        self._pre_switch_duration_override = pre_switch_duration_seconds
        self._tighten_stops_on_pre_switch = tighten_stops_on_pre_switch
        self._transition_timers = transition_timers or TRANSITION_TIMERS
        self._transition_smc_requirements = (
            transition_smc_requirements or TRANSITION_SMC_REQUIREMENTS
        )
        self._routing_config = routing_config

        self._last_transition_time: datetime | None = None
        self._current_regime: MarketRegime | None = None
        self._transition_history: list[TransitionRecord] = []

        # Two-phase PRE_SWITCH state (issue #360)
        self._transition_phase: TransitionPhase = TransitionPhase.STABLE
        self._pre_switch_target_regime: MarketRegime | None = None
        self._pre_switch_started_at: datetime | None = None
        self._pre_switch_smc_confirmed: bool = False
        self._cancelled_pre_switch_count: int = 0

    @property
    def current_regime(self) -> MarketRegime | None:
        """Currently active regime."""
        return self._current_regime

    @property
    def transition_history(self) -> list[TransitionRecord]:
        """Transition history (most recent first)."""
        return self._transition_history.copy()

    @property
    def last_transition_time(self) -> datetime | None:
        """Time of last completed transition."""
        return self._last_transition_time

    @property
    def transition_phase(self) -> TransitionPhase:
        """Current two-phase transition state."""
        return self._transition_phase

    @property
    def cancelled_pre_switch_count(self) -> int:
        """Number of PRE_SWITCH phases that were cancelled (regime returned to STABLE)."""
        return self._cancelled_pre_switch_count

    def select(self, analysis: RegimeAnalysis) -> SelectionResult:
        """
        Determine which strategies should be active based on regime analysis.

        Does NOT execute transitions — returns a SelectionResult describing
        what should change. Call ``execute_transition()`` to apply.

        Two-phase PRE_SWITCH logic (issue #360):
        - When a regime change is detected the selector first enters PRE_SWITCH.
        - The transition is only approved (``transition_needed=True``) once both
          the per-transition timer *and* the SMC structural signal are satisfied.
        - If the potential target regime disappears during PRE_SWITCH, the
          selector returns to STABLE and increments ``cancelled_pre_switch_count``.

        Args:
            analysis: Current market regime analysis.

        Returns:
            SelectionResult with strategies to start/stop/keep.
        """
        regime = analysis.regime
        recommended = analysis.recommended_strategy
        now = datetime.now(timezone.utc)

        # Get target strategies for the detected regime
        target_weights = self._get_target_strategies(regime, recommended, analysis)
        target_types = {w.strategy_type for w in target_weights}

        # Get currently active strategy types
        active_instances = self._registry.get_active()
        active_types = {inst.strategy_type for inst in active_instances}

        # Determine what needs to change
        to_start_types = target_types - active_types
        to_stop_types = active_types - target_types
        to_keep_types = active_types & target_types

        strategies_to_start = [w for w in target_weights if w.strategy_type in to_start_types]
        transition_needed = bool(to_start_types or to_stop_types)

        # -----------------------------------------------------------------------
        # Two-phase PRE_SWITCH gate (issue #360)
        # -----------------------------------------------------------------------
        if transition_needed and self._current_regime is not None:
            # Determine whether we should use the two-phase gate.
            # The gate is skipped for the very first regime selection (startup).
            result = self._apply_two_phase_gate(
                analysis=analysis,
                now=now,
                active_types=active_types,
                regime=regime,
                recommended=recommended,
            )
            if result is not None:
                # Gate returned a non-None result → either PRE_SWITCH hold or ready
                return result

        # -----------------------------------------------------------------------
        # Legacy single-phase gate (cooldown / short regime / confidence)
        # -----------------------------------------------------------------------
        if transition_needed:
            blocked, reason = self._check_transition_blocked(analysis)
            if blocked:
                return SelectionResult(
                    strategies_to_start=[],
                    strategies_to_stop=[],
                    strategies_to_keep=list(active_types),
                    regime=regime,
                    recommended=recommended,
                    transition_needed=False,
                    reason=reason,
                )

        # If a PRE_SWITCH was active for a *different* target regime but now the
        # regime changed back to the current one (no transition needed), cancel it.
        if not transition_needed and self._transition_phase == TransitionPhase.PRE_SWITCH:
            self._cancel_pre_switch(reason="Regime returned to stable; no transition needed")

        reason = self._build_reason(
            regime, recommended, to_start_types, to_stop_types, to_keep_types
        )

        return SelectionResult(
            strategies_to_start=strategies_to_start,
            strategies_to_stop=list(to_stop_types),
            strategies_to_keep=list(to_keep_types),
            regime=regime,
            recommended=recommended,
            transition_needed=transition_needed,
            reason=reason,
        )

    async def execute_transition(self, result: SelectionResult) -> TransitionRecord:
        """
        Execute a strategy transition based on SelectionResult.

        Stops strategies first, then starts new ones (safe order).

        Args:
            result: SelectionResult from select().

        Returns:
            TransitionRecord documenting the transition.
        """
        old_regime = self._current_regime or MarketRegime.UNKNOWN
        from_strategies = list(result.strategies_to_keep) + result.strategies_to_stop

        record = TransitionRecord(
            from_regime=old_regime,
            to_regime=result.regime,
            from_strategies=from_strategies,
            to_strategies=[w.strategy_type for w in result.strategies_to_start]
            + result.strategies_to_keep,
            recommended=result.recommended,
            timestamp=datetime.now(timezone.utc),
            state=TransitionState.IN_PROGRESS,
        )

        try:
            # Phase 1: Stop strategies that should no longer be active
            for strategy_type in result.strategies_to_stop:
                instances = self._registry.get_by_type(strategy_type)
                for inst in instances:
                    if inst.state in (StrategyState.ACTIVE, StrategyState.PAUSED):
                        await self._registry.stop_strategy(inst.strategy_id)
                        logger.info(
                            "strategy_stopped_by_selector",
                            strategy_id=inst.strategy_id,
                            strategy_type=strategy_type,
                            reason=result.reason,
                        )

            # Phase 2: Start strategies for the new regime
            for weight in result.strategies_to_start:
                instances = self._registry.get_by_type(weight.strategy_type)
                for inst in instances:
                    if inst.state == StrategyState.IDLE:
                        await self._registry.start_strategy(inst.strategy_id)
                        logger.info(
                            "strategy_started_by_selector",
                            strategy_id=inst.strategy_id,
                            strategy_type=weight.strategy_type,
                            weight=weight.weight,
                            priority=weight.priority,
                        )
                    elif inst.state == StrategyState.PAUSED:
                        await self._registry.resume_strategy(inst.strategy_id)
                    elif inst.state == StrategyState.STOPPED:
                        await self._registry.reset_strategy(inst.strategy_id)
                        await self._registry.start_strategy(inst.strategy_id)

            record.state = TransitionState.COMPLETED
            self._current_regime = result.regime
            self._last_transition_time = record.timestamp
            # Reset two-phase state now that the transition is done
            self._reset_transition_phase()

            logger.info(
                "strategy_transition_completed",
                from_regime=old_regime.value,
                to_regime=result.regime.value,
                started=[w.strategy_type for w in result.strategies_to_start],
                stopped=result.strategies_to_stop,
                kept=result.strategies_to_keep,
            )

        except Exception as e:
            record.state = TransitionState.CANCELLED
            # Also reset two-phase state on failure so we can retry
            self._reset_transition_phase()
            logger.error(
                "strategy_transition_failed",
                error=str(e),
                from_regime=old_regime.value,
                to_regime=result.regime.value,
            )

        self._add_history(record)
        return record

    def resolve_signal_conflict(self, signals: list[dict[str, Any]]) -> dict[str, Any] | None:
        """
        Resolve conflicting signals from multiple active strategies.

        Uses priority ranking: lower priority number wins.
        Ties broken by confidence score.

        Args:
            signals: List of signal dicts, each must have 'strategy_type' and 'confidence'.

        Returns:
            The winning signal, or None if no signals.
        """
        if not signals:
            return None

        if len(signals) == 1:
            return signals[0]

        # Get current target weights for priority lookup
        target_weights = self._get_current_weights()
        priority_map = {w.strategy_type: w.priority for w in target_weights}

        def signal_sort_key(signal: dict[str, Any]) -> tuple[int, float]:
            strategy_type = signal.get("strategy_type", "")
            priority = priority_map.get(strategy_type, 999)
            confidence = signal.get("confidence", 0.0)
            return (priority, -confidence)  # Lower priority number first, higher confidence first

        sorted_signals = sorted(signals, key=signal_sort_key)

        winner = sorted_signals[0]
        logger.info(
            "signal_conflict_resolved",
            winner_type=winner.get("strategy_type"),
            winner_confidence=winner.get("confidence"),
            total_signals=len(signals),
        )
        return winner

    def get_status(self) -> dict[str, Any]:
        """Get current selector status."""
        active = self._registry.get_active()
        target = self._get_current_weights()

        return {
            "current_regime": self._current_regime.value if self._current_regime else None,
            "active_strategies": [
                {"id": inst.strategy_id, "type": inst.strategy_type} for inst in active
            ],
            "target_strategies": [
                {"type": w.strategy_type, "weight": w.weight, "priority": w.priority}
                for w in target
            ],
            "last_transition": (
                self._last_transition_time.isoformat() if self._last_transition_time else None
            ),
            "transition_cooldown_seconds": self._transition_cooldown,
            "min_regime_duration_seconds": self._min_regime_duration,
            "history_count": len(self._transition_history),
            # Two-phase PRE_SWITCH status (issue #360)
            "transition_phase": self._transition_phase.value,
            "pre_switch_target_regime": (
                self._pre_switch_target_regime.value if self._pre_switch_target_regime else None
            ),
            "pre_switch_started_at": (
                self._pre_switch_started_at.isoformat() if self._pre_switch_started_at else None
            ),
            "pre_switch_smc_confirmed": self._pre_switch_smc_confirmed,
            "cancelled_pre_switch_count": self._cancelled_pre_switch_count,
            "require_smc_confirmation": self._require_smc_confirmation,
            "tighten_stops_on_pre_switch": self._tighten_stops_on_pre_switch,
        }

    # =========================================================================
    # Internal Methods
    # =========================================================================

    # ------------------------------------------------------------------
    # Two-phase PRE_SWITCH helpers (issue #360)
    # ------------------------------------------------------------------

    def _apply_two_phase_gate(
        self,
        analysis: RegimeAnalysis,
        now: datetime,
        active_types: set[str],
        regime: MarketRegime,
        recommended: RecommendedStrategy,
    ) -> "SelectionResult | None":
        """
        Evaluate the two-phase PRE_SWITCH / CONFIRMED gate.

        Returns a *blocking* ``SelectionResult`` (transition_needed=False) when
        the gate has not yet been cleared, or ``None`` when the gate is cleared
        and the caller should proceed with a normal transition.
        """
        from_regime = self._current_regime  # guaranteed non-None by caller

        if self._transition_phase == TransitionPhase.STABLE:
            # First detection of a potential regime change → enter PRE_SWITCH
            self._enter_pre_switch(target=regime, now=now)
            logger.info(
                "pre_switch_entered",
                from_regime=from_regime.value if from_regime else None,
                to_regime=regime.value,
                require_smc=self._require_smc_confirmation,
                tighten_stops=self._tighten_stops_on_pre_switch,
            )
            return SelectionResult(
                strategies_to_start=[],
                strategies_to_stop=[],
                strategies_to_keep=list(active_types),
                regime=regime,
                recommended=recommended,
                transition_needed=False,
                reason=(
                    f"PRE_SWITCH entered: waiting for timer + SMC confirmation "
                    f"({from_regime.value if from_regime else '?'} → {regime.value})"
                ),
            )

        if self._transition_phase == TransitionPhase.PRE_SWITCH:
            # We are already in PRE_SWITCH — check whether the target changed
            if regime != self._pre_switch_target_regime:
                # Target regime changed; cancel current PRE_SWITCH and start fresh
                self._cancel_pre_switch(
                    reason=f"Target regime changed from "
                    f"{self._pre_switch_target_regime} to {regime}"
                )
                self._enter_pre_switch(target=regime, now=now)
                return SelectionResult(
                    strategies_to_start=[],
                    strategies_to_stop=[],
                    strategies_to_keep=list(active_types),
                    regime=regime,
                    recommended=recommended,
                    transition_needed=False,
                    reason=(f"PRE_SWITCH restarted: target changed to {regime.value}"),
                )

            # Update SMC confirmation state from latest analysis
            if not self._pre_switch_smc_confirmed:
                self._pre_switch_smc_confirmed = self._check_smc_signal(
                    from_regime=from_regime,
                    to_regime=regime,
                    analysis=analysis,
                )

            # Check whether both criteria are met
            timer_ok = self._check_pre_switch_timer(
                from_regime=from_regime,
                to_regime=regime,
                now=now,
            )
            smc_ok = (not self._require_smc_confirmation) or self._pre_switch_smc_confirmed

            if timer_ok and smc_ok:
                # Advance to CONFIRMED → signal the caller to execute the transition
                self._transition_phase = TransitionPhase.CONFIRMED
                logger.info(
                    "pre_switch_confirmed",
                    from_regime=from_regime.value if from_regime else None,
                    to_regime=regime.value,
                    timer_elapsed_s=(
                        (now - self._pre_switch_started_at).total_seconds()
                        if self._pre_switch_started_at
                        else None
                    ),
                    smc_confirmed=self._pre_switch_smc_confirmed,
                )
                # Return None → let the caller proceed with the actual transition
                return None

            # Gate not yet cleared — log waiting state and block
            elapsed = (
                (now - self._pre_switch_started_at).total_seconds()
                if self._pre_switch_started_at
                else 0.0
            )
            required_timer = self._get_pre_switch_timer(from_regime=from_regime, to_regime=regime)
            logger.debug(
                "pre_switch_waiting",
                from_regime=from_regime.value if from_regime else None,
                to_regime=regime.value,
                elapsed_s=elapsed,
                required_s=required_timer,
                timer_ok=timer_ok,
                smc_ok=smc_ok,
            )
            return SelectionResult(
                strategies_to_start=[],
                strategies_to_stop=[],
                strategies_to_keep=list(active_types),
                regime=regime,
                recommended=recommended,
                transition_needed=False,
                reason=(
                    f"PRE_SWITCH pending ({elapsed:.0f}s / {required_timer:.0f}s elapsed; "
                    f"smc={'ok' if smc_ok else 'pending'})"
                ),
            )

        if self._transition_phase == TransitionPhase.CONFIRMED:
            # Already confirmed — proceed with the transition
            return None

        # TRANSITIONING: another transition is in progress; block
        return SelectionResult(
            strategies_to_start=[],
            strategies_to_stop=[],
            strategies_to_keep=list(active_types),
            regime=regime,
            recommended=recommended,
            transition_needed=False,
            reason="Transition already in TRANSITIONING phase; waiting for completion",
        )

    def _enter_pre_switch(self, target: MarketRegime, now: datetime) -> None:
        """Enter PRE_SWITCH phase for the given target regime."""
        self._transition_phase = TransitionPhase.PRE_SWITCH
        self._pre_switch_target_regime = target
        self._pre_switch_started_at = now
        self._pre_switch_smc_confirmed = False

    def _cancel_pre_switch(self, reason: str) -> None:
        """Cancel the current PRE_SWITCH phase and return to STABLE."""
        self._cancelled_pre_switch_count += 1
        logger.info(
            "pre_switch_cancelled",
            reason=reason,
            target_regime=(
                self._pre_switch_target_regime.value if self._pre_switch_target_regime else None
            ),
            cancelled_total=self._cancelled_pre_switch_count,
        )
        self._transition_phase = TransitionPhase.STABLE
        self._pre_switch_target_regime = None
        self._pre_switch_started_at = None
        self._pre_switch_smc_confirmed = False

    def _get_pre_switch_timer(
        self, from_regime: MarketRegime | None, to_regime: MarketRegime
    ) -> float:
        """Return the required PRE_SWITCH duration in seconds for this transition."""
        if self._pre_switch_duration_override is not None:
            return self._pre_switch_duration_override
        if from_regime is not None:
            key = (from_regime, to_regime)
            if key in self._transition_timers:
                return self._transition_timers[key]
        return self._transition_timers.get("default", _DEFAULT_PRE_SWITCH_SECONDS)

    def _check_pre_switch_timer(
        self, from_regime: MarketRegime | None, to_regime: MarketRegime, now: datetime
    ) -> bool:
        """Return True when the PRE_SWITCH timer requirement is satisfied."""
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
        """
        Return True when the required SMC structural signal is present.

        Checks ``analysis.analysis_details["smc_signal"]`` for the expected
        signal type (BOS / CHoCH).  When no SMC signal is required for the
        given transition pair, returns True immediately.
        """
        if from_regime is not None:
            key = (from_regime, to_regime)
            required_signal = self._transition_smc_requirements.get(
                key,
                self._transition_smc_requirements.get("default"),
            )
        else:
            required_signal = self._transition_smc_requirements.get("default")

        if required_signal is None:
            # No SMC signal required → always satisfied
            return True

        detected = analysis.analysis_details.get("smc_signal")
        return detected == required_signal

    def _reset_transition_phase(self) -> None:
        """Reset two-phase state to STABLE after a completed transition."""
        self._transition_phase = TransitionPhase.STABLE
        self._pre_switch_target_regime = None
        self._pre_switch_started_at = None
        self._pre_switch_smc_confirmed = False

    def _get_target_strategies(
        self,
        regime: MarketRegime,
        recommended: RecommendedStrategy,
        analysis: "RegimeAnalysis | None" = None,
    ) -> list[StrategyWeight]:
        """Get target strategy weights for the given regime and recommendation.

        When *routing_config* was supplied at construction time the selection is
        delegated to :meth:`RoutingConfig.get_strategies` using the current market
        conditions as the matching context.  The legacy *regime_strategies* /
        *hybrid_weights* dicts are used as the fallback when no ``routing_config``
        was provided (backwards-compatible behaviour).
        """
        # Reduce exposure: no strategies active (always takes precedence)
        if recommended == RecommendedStrategy.REDUCE_EXPOSURE:
            return []

        # Hold: keep current strategies unchanged
        if recommended == RecommendedStrategy.HOLD:
            return self._get_current_weights()

        # --- RoutingConfig path (issue #370) ---
        if self._routing_config is not None:
            conditions = self._build_routing_conditions(regime, recommended, analysis)
            strategy_configs: list[StrategyConfig] = self._routing_config.get_strategies(
                conditions
            )
            return [
                StrategyWeight(
                    strategy_type=sc.name,
                    weight=float(sc.params.get("weight", 1.0)),
                    priority=int(sc.params.get("priority", 1)),
                )
                for sc in strategy_configs
            ]

        # --- Legacy hard-coded path ---
        # Hybrid mode overrides default weights
        if recommended == RecommendedStrategy.HYBRID:
            return list(self._hybrid_weights)

        return list(self._regime_strategies.get(regime, []))

    @staticmethod
    def _build_routing_conditions(
        regime: MarketRegime,
        recommended: RecommendedStrategy,
        analysis: "RegimeAnalysis | None",
    ) -> dict[str, Any]:
        """Build the conditions dict passed to :meth:`RoutingConfig.get_strategies`.

        Keys produced:
        - ``market_regime``: regime value string (e.g. ``"bull_trend"``)
        - ``confluence_high``: ``True`` when ``confluence_score >= 0.7`` (HYBRID signal)
        """
        conditions: dict[str, Any] = {"market_regime": regime.value}
        if recommended == RecommendedStrategy.HYBRID or (
            analysis is not None and analysis.confluence_score >= 0.7
        ):
            conditions["confluence_high"] = True
        return conditions

    def _get_current_weights(self) -> list[StrategyWeight]:
        """Get weights for currently active regime."""
        if self._current_regime is None:
            return []
        if self._routing_config is not None:
            conditions: dict[str, Any] = {"market_regime": self._current_regime.value}
            strategy_configs = self._routing_config.get_strategies(conditions)
            return [
                StrategyWeight(
                    strategy_type=sc.name,
                    weight=float(sc.params.get("weight", 1.0)),
                    priority=int(sc.params.get("priority", 1)),
                )
                for sc in strategy_configs
            ]
        return list(self._regime_strategies.get(self._current_regime, []))

    def _check_transition_blocked(self, analysis: RegimeAnalysis) -> tuple[bool, str]:
        """
        Check if transition should be blocked.

        The very first transition (no prior regime) is never blocked by
        duration or confidence gates — the bot must always start with a
        strategy set, regardless of initial data quality.

        Returns:
            Tuple of (is_blocked, reason).
        """
        now = datetime.now(timezone.utc)

        # Check cooldown (only after the first transition has occurred)
        if self._last_transition_time:
            elapsed = (now - self._last_transition_time).total_seconds()
            if elapsed < self._transition_cooldown:
                remaining = self._transition_cooldown - elapsed
                return True, f"Transition cooldown active ({remaining:.0f}s remaining)"

        # Duration and confidence gates are only applied after the first regime
        # has been established.  On startup, activate strategies unconditionally.
        if self._current_regime is None:
            return False, ""

        # Check minimum regime duration
        if analysis.regime_duration_seconds < self._min_regime_duration:
            return True, (
                f"Regime too young ({analysis.regime_duration_seconds}s "
                f"< {self._min_regime_duration:.0f}s minimum)"
            )

        # Check confidence threshold
        if analysis.confidence < 0.3:
            return True, f"Confidence too low ({analysis.confidence:.2f} < 0.30)"

        return False, ""

    def _build_reason(
        self,
        regime: MarketRegime,
        recommended: RecommendedStrategy,
        to_start: set[str],
        to_stop: set[str],
        to_keep: set[str],
    ) -> str:
        """Build human-readable reason for the selection."""
        parts = [f"Regime: {regime.value}, Recommended: {recommended.value}"]
        if to_start:
            parts.append(f"Start: {', '.join(sorted(to_start))}")
        if to_stop:
            parts.append(f"Stop: {', '.join(sorted(to_stop))}")
        if to_keep:
            parts.append(f"Keep: {', '.join(sorted(to_keep))}")
        if not to_start and not to_stop:
            parts.append("No changes needed")
        return ". ".join(parts)

    def _add_history(self, record: TransitionRecord) -> None:
        """Add transition record to history."""
        self._transition_history.insert(0, record)
        if len(self._transition_history) > self._max_history:
            self._transition_history = self._transition_history[: self._max_history]
