"""
StrategyRegistry - Manages multiple trading strategy instances with independent lifecycles.

Each strategy has its own state machine:
  idle → starting → active → paused → stopping → stopped → idle
                                    ↓
                              pre_switch  (new entries blocked; issue #360)

The PRE_SWITCH state is entered when StrategySelector detects a potential
regime change but has not yet confirmed it.  Strategies in PRE_SWITCH hold
existing positions but do not open new ones.  They return to ACTIVE if the
regime stabilises, or advance to STOPPING when a confirmed transition executes.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from bot.utils.logger import get_logger

logger = get_logger(__name__)


class StrategyState(str, Enum):
    """Strategy lifecycle states."""

    IDLE = "idle"
    STARTING = "starting"
    ACTIVE = "active"
    PRE_SWITCH = "pre_switch"  # New entries blocked; existing positions held (issue #360)
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


# Valid state transitions
_VALID_TRANSITIONS: dict[StrategyState, set[StrategyState]] = {
    StrategyState.IDLE: {StrategyState.STARTING},
    StrategyState.STARTING: {StrategyState.ACTIVE, StrategyState.ERROR},
    StrategyState.ACTIVE: {
        StrategyState.PRE_SWITCH,
        StrategyState.PAUSED,
        StrategyState.STOPPING,
        StrategyState.ERROR,
    },
    StrategyState.PRE_SWITCH: {
        StrategyState.ACTIVE,  # regime returned to stable
        StrategyState.STOPPING,  # confirmed transition → graceful close
        StrategyState.ERROR,
    },
    StrategyState.PAUSED: {StrategyState.ACTIVE, StrategyState.STOPPING, StrategyState.ERROR},
    StrategyState.STOPPING: {StrategyState.STOPPED, StrategyState.ERROR},
    StrategyState.STOPPED: {StrategyState.IDLE},
    StrategyState.ERROR: {StrategyState.IDLE, StrategyState.STOPPING},
}


@runtime_checkable
class TradingStrategy(Protocol):
    """Protocol that all trading strategies must implement for v2.0 integration."""

    def get_strategy_name(self) -> str:
        """Return unique strategy name."""
        ...

    def get_strategy_type(self) -> str:
        """Return strategy type (e.g., 'smc', 'trend_follower', 'grid', 'dca')."""
        ...


@dataclass
class StrategyMetrics:
    """Runtime metrics for a strategy instance."""

    total_signals: int = 0
    executed_trades: int = 0
    profitable_trades: int = 0
    total_pnl: Decimal = Decimal("0")
    last_signal_time: datetime | None = None
    last_trade_time: datetime | None = None
    error_count: int = 0
    last_error: str | None = None
    last_error_time: datetime | None = None
    uptime_seconds: float = 0.0


@dataclass
class StrategyInstance:
    """
    Wraps a trading strategy with lifecycle state and metrics.

    Manages state transitions and tracks runtime health/performance.
    """

    strategy_id: str
    strategy_type: str
    config: dict[str, Any]
    state: StrategyState = StrategyState.IDLE
    metrics: StrategyMetrics = field(default_factory=StrategyMetrics)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    _state_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def can_transition_to(self, new_state: StrategyState) -> bool:
        """Check if transition to new_state is valid."""
        return new_state in _VALID_TRANSITIONS.get(self.state, set())

    async def transition_to(self, new_state: StrategyState) -> bool:
        """
        Attempt state transition. Returns True if successful.

        Thread-safe via async lock.
        """
        async with self._state_lock:
            if not self.can_transition_to(new_state):
                logger.warning(
                    "invalid_state_transition",
                    strategy_id=self.strategy_id,
                    current=self.state.value,
                    requested=new_state.value,
                )
                return False

            old_state = self.state
            self.state = new_state

            if new_state == StrategyState.ACTIVE and self.started_at is None:
                self.started_at = datetime.now(timezone.utc)
            elif new_state == StrategyState.STOPPED:
                self.stopped_at = datetime.now(timezone.utc)
                if self.started_at:
                    delta = self.stopped_at - self.started_at
                    self.metrics.uptime_seconds += delta.total_seconds()

            logger.info(
                "strategy_state_changed",
                strategy_id=self.strategy_id,
                old_state=old_state.value,
                new_state=new_state.value,
            )
            return True

    def record_error(self, error: str) -> None:
        """Record an error occurrence."""
        self.metrics.error_count += 1
        self.metrics.last_error = error
        self.metrics.last_error_time = datetime.now(timezone.utc)

    def record_signal(self) -> None:
        """Record a signal generation."""
        self.metrics.total_signals += 1
        self.metrics.last_signal_time = datetime.now(timezone.utc)

    def record_trade(self, pnl: Decimal, profitable: bool) -> None:
        """Record a completed trade."""
        self.metrics.executed_trades += 1
        self.metrics.total_pnl += pnl
        if profitable:
            self.metrics.profitable_trades += 1
        self.metrics.last_trade_time = datetime.now(timezone.utc)

    def get_status(self) -> dict[str, Any]:
        """Get comprehensive status of this strategy instance."""
        win_rate = 0.0
        if self.metrics.executed_trades > 0:
            win_rate = self.metrics.profitable_trades / self.metrics.executed_trades

        return {
            "strategy_id": self.strategy_id,
            "strategy_type": self.strategy_type,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "metrics": {
                "total_signals": self.metrics.total_signals,
                "executed_trades": self.metrics.executed_trades,
                "profitable_trades": self.metrics.profitable_trades,
                "win_rate": round(win_rate, 4),
                "total_pnl": str(self.metrics.total_pnl),
                "error_count": self.metrics.error_count,
                "last_error": self.metrics.last_error,
                "uptime_seconds": round(self.metrics.uptime_seconds, 1),
            },
        }


class StrategyRegistry:
    """
    Registry for managing multiple strategy instances.

    Supports registering, starting, stopping, and querying strategies.
    Enforces unique strategy IDs and manages lifecycle transitions.
    """

    def __init__(self, max_strategies: int = 10):
        self._strategies: dict[str, StrategyInstance] = {}
        self._max_strategies = max_strategies
        self._lock = asyncio.Lock()

    @property
    def strategy_count(self) -> int:
        """Number of registered strategies."""
        return len(self._strategies)

    def register(
        self,
        strategy_id: str,
        strategy_type: str,
        config: dict[str, Any] | None = None,
    ) -> StrategyInstance:
        """
        Register a new strategy instance.

        Args:
            strategy_id: Unique identifier for this strategy instance.
            strategy_type: Type of strategy (e.g., 'smc', 'trend_follower', 'grid', 'dca').
            config: Strategy-specific configuration.

        Returns:
            The created StrategyInstance.

        Raises:
            ValueError: If strategy_id already exists or max limit reached.
        """
        if strategy_id in self._strategies:
            raise ValueError(f"Strategy '{strategy_id}' already registered")
        if len(self._strategies) >= self._max_strategies:
            raise ValueError(f"Maximum strategies ({self._max_strategies}) reached")

        instance = StrategyInstance(
            strategy_id=strategy_id,
            strategy_type=strategy_type,
            config=config or {},
        )
        self._strategies[strategy_id] = instance

        logger.info(
            "strategy_registered",
            strategy_id=strategy_id,
            strategy_type=strategy_type,
        )
        return instance

    def unregister(self, strategy_id: str) -> bool:
        """
        Remove a strategy from the registry.

        Only allowed if strategy is in IDLE or STOPPED state.
        """
        instance = self._strategies.get(strategy_id)
        if not instance:
            logger.warning("strategy_not_found", strategy_id=strategy_id)
            return False

        if instance.state not in (StrategyState.IDLE, StrategyState.STOPPED):
            logger.warning(
                "cannot_unregister_active_strategy",
                strategy_id=strategy_id,
                state=instance.state.value,
            )
            return False

        del self._strategies[strategy_id]
        logger.info("strategy_unregistered", strategy_id=strategy_id)
        return True

    def get(self, strategy_id: str) -> StrategyInstance | None:
        """Get a strategy instance by ID."""
        return self._strategies.get(strategy_id)

    def get_all(self) -> list[StrategyInstance]:
        """Get all registered strategy instances."""
        return list(self._strategies.values())

    def get_by_type(self, strategy_type: str) -> list[StrategyInstance]:
        """Get all strategies of a given type."""
        return [s for s in self._strategies.values() if s.strategy_type == strategy_type]

    def get_active(self) -> list[StrategyInstance]:
        """Get all strategies in ACTIVE state."""
        return [s for s in self._strategies.values() if s.state == StrategyState.ACTIVE]

    def get_by_state(self, state: StrategyState) -> list[StrategyInstance]:
        """Get all strategies in a specific state."""
        return [s for s in self._strategies.values() if s.state == state]

    async def start_strategy(self, strategy_id: str) -> bool:
        """Transition a strategy from IDLE to STARTING → ACTIVE."""
        instance = self.get(strategy_id)
        if not instance:
            logger.warning("strategy_not_found", strategy_id=strategy_id)
            return False

        if not await instance.transition_to(StrategyState.STARTING):
            return False

        return await instance.transition_to(StrategyState.ACTIVE)

    async def stop_strategy(self, strategy_id: str) -> bool:
        """Transition a strategy to STOPPING → STOPPED."""
        instance = self.get(strategy_id)
        if not instance:
            logger.warning("strategy_not_found", strategy_id=strategy_id)
            return False

        if not await instance.transition_to(StrategyState.STOPPING):
            return False

        return await instance.transition_to(StrategyState.STOPPED)

    async def pause_strategy(self, strategy_id: str) -> bool:
        """Pause an active strategy."""
        instance = self.get(strategy_id)
        if not instance:
            return False
        return await instance.transition_to(StrategyState.PAUSED)

    async def resume_strategy(self, strategy_id: str) -> bool:
        """Resume a paused strategy."""
        instance = self.get(strategy_id)
        if not instance:
            return False
        return await instance.transition_to(StrategyState.ACTIVE)

    async def reset_strategy(self, strategy_id: str) -> bool:
        """Reset a stopped/error strategy back to IDLE for restart."""
        instance = self.get(strategy_id)
        if not instance:
            return False

        if instance.state == StrategyState.ERROR:
            if not await instance.transition_to(StrategyState.IDLE):
                return False
        elif instance.state == StrategyState.STOPPED:
            if not await instance.transition_to(StrategyState.IDLE):
                return False
        else:
            logger.warning(
                "cannot_reset_strategy",
                strategy_id=strategy_id,
                state=instance.state.value,
            )
            return False

        instance.started_at = None
        instance.stopped_at = None
        return True

    async def enter_pre_switch(self, strategy_id: str) -> bool:
        """Transition an ACTIVE strategy to PRE_SWITCH (new entries blocked)."""
        instance = self.get(strategy_id)
        if not instance:
            return False
        return await instance.transition_to(StrategyState.PRE_SWITCH)

    async def exit_pre_switch(self, strategy_id: str) -> bool:
        """Return a PRE_SWITCH strategy to ACTIVE (regime stabilised)."""
        instance = self.get(strategy_id)
        if not instance:
            return False
        return await instance.transition_to(StrategyState.ACTIVE)

    def get_pre_switch(self) -> list[StrategyInstance]:
        """Get all strategies in PRE_SWITCH state."""
        return [s for s in self._strategies.values() if s.state == StrategyState.PRE_SWITCH]

    async def stop_all(self) -> dict[str, bool]:
        """Stop all active/paused/pre_switch strategies. Returns dict of results."""
        results = {}
        for strategy_id, instance in self._strategies.items():
            if instance.state in (
                StrategyState.ACTIVE,
                StrategyState.PAUSED,
                StrategyState.PRE_SWITCH,
            ):
                results[strategy_id] = await self.stop_strategy(strategy_id)
        return results

    def get_registry_status(self) -> dict[str, Any]:
        """Get overall registry status summary."""
        states_count: dict[str, int] = {}
        for instance in self._strategies.values():
            state = instance.state.value
            states_count[state] = states_count.get(state, 0) + 1

        return {
            "total_strategies": len(self._strategies),
            "max_strategies": self._max_strategies,
            "states": states_count,
            "strategies": [s.get_status() for s in self._strategies.values()],
        }
