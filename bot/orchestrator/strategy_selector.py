"""
StrategySelector - Routes market signals to appropriate strategies based on regime.

Responsibilities:
- Map market regimes to strategy configurations with priorities
- Manage smooth transitions between strategies (cooldown, min duration)
- Resolve overlapping signal conflicts via priority ranking
- Track transition history for analysis
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
        StrategyWeight(strategy_type="dca", weight=0.7, priority=1),
        StrategyWeight(strategy_type="trend_follower", weight=0.3, priority=2),
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
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        regime_strategies: dict[MarketRegime, list[StrategyWeight]] | None = None,
        hybrid_weights: list[StrategyWeight] | None = None,
        transition_cooldown_seconds: float = 300.0,
        min_regime_duration_seconds: float = 120.0,
        max_transition_history: int = 50,
    ):
        """
        Args:
            registry: Strategy registry for lifecycle management.
            regime_strategies: Custom mapping of regimes to strategy weights.
            hybrid_weights: Custom weights for hybrid mode.
            transition_cooldown_seconds: Minimum time between transitions (default 5 min).
            min_regime_duration_seconds: Minimum regime duration before transition (default 2 min).
            max_transition_history: Max transition records to keep.
        """
        self._registry = registry
        self._regime_strategies = regime_strategies or DEFAULT_REGIME_STRATEGIES
        self._hybrid_weights = hybrid_weights or HYBRID_STRATEGY_WEIGHTS
        self._transition_cooldown = transition_cooldown_seconds
        self._min_regime_duration = min_regime_duration_seconds
        self._max_history = max_transition_history

        self._last_transition_time: datetime | None = None
        self._current_regime: MarketRegime | None = None
        self._transition_history: list[TransitionRecord] = []

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

    def select(self, analysis: RegimeAnalysis) -> SelectionResult:
        """
        Determine which strategies should be active based on regime analysis.

        Does NOT execute transitions — returns a SelectionResult describing
        what should change. Call `execute_transition()` to apply.

        Args:
            analysis: Current market regime analysis.

        Returns:
            SelectionResult with strategies to start/stop/keep.
        """
        regime = analysis.regime
        recommended = analysis.recommended_strategy

        # Get target strategies for the detected regime
        target_weights = self._get_target_strategies(regime, recommended)
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

        # Check if transition is allowed
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
        }

    # =========================================================================
    # Internal Methods
    # =========================================================================

    def _get_target_strategies(
        self, regime: MarketRegime, recommended: RecommendedStrategy
    ) -> list[StrategyWeight]:
        """Get target strategy weights for the given regime and recommendation."""
        # Hybrid mode overrides default weights
        if recommended == RecommendedStrategy.HYBRID:
            return list(self._hybrid_weights)

        # Reduce exposure: no strategies active
        if recommended == RecommendedStrategy.REDUCE_EXPOSURE:
            return []

        # Hold: keep current, return empty (no change)
        if recommended == RecommendedStrategy.HOLD:
            return self._get_current_weights()

        return list(self._regime_strategies.get(regime, []))

    def _get_current_weights(self) -> list[StrategyWeight]:
        """Get weights for currently active regime."""
        if self._current_regime is None:
            return []
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
