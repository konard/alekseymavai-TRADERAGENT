"""
HealthMonitor - Monitors health of strategy instances and orchestrator components.

Features:
- Periodic heartbeat checks for each strategy
- Error rate tracking with automatic pause/restart
- Performance degradation detection
- Resource monitoring (memory, tasks)
- Configurable thresholds and actions
"""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from bot.orchestrator.strategy_registry import StrategyInstance, StrategyRegistry, StrategyState
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class HealthStatus(str, Enum):
    """Health status levels."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    CRITICAL = "critical"


@dataclass
class HealthCheckResult:
    """Result of a single health check."""

    strategy_id: str
    status: HealthStatus
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthThresholds:
    """Configurable thresholds for health monitoring."""

    # Max errors before marking unhealthy
    max_error_count: int = 10
    # Max consecutive errors before critical
    max_consecutive_errors: int = 3
    # Max seconds without a signal before degraded
    signal_timeout_seconds: float = 300.0
    # Max seconds without a trade before degraded
    trade_timeout_seconds: float = 3600.0
    # Error rate (errors/minute) threshold for degraded
    error_rate_threshold: float = 1.0
    # Auto-restart strategies in ERROR state
    auto_restart: bool = True
    # Max auto-restart attempts
    max_restart_attempts: int = 3


class HealthMonitor:
    """
    Monitors health of all registered strategies.

    Runs periodic checks and can trigger automatic actions
    (pause, stop, restart) based on configurable thresholds.
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        thresholds: HealthThresholds | None = None,
        check_interval: float = 30.0,
    ):
        """
        Args:
            registry: Strategy registry to monitor.
            thresholds: Health check thresholds.
            check_interval: Seconds between health checks.
        """
        self._registry = registry
        self._thresholds = thresholds or HealthThresholds()
        self._check_interval = check_interval

        self._running = False
        self._monitor_task: asyncio.Task | None = None
        self._health_history: dict[str, list[HealthCheckResult]] = {}
        self._restart_counts: dict[str, int] = {}
        self._consecutive_errors: dict[str, int] = {}

        # Callbacks for health events
        self._on_unhealthy: Callable[[str, HealthCheckResult], Coroutine[Any, Any, None]] | None = (
            None
        )
        self._on_critical: Callable[[str, HealthCheckResult], Coroutine[Any, Any, None]] | None = (
            None
        )

    def set_unhealthy_callback(
        self,
        callback: Callable[[str, HealthCheckResult], Coroutine[Any, Any, None]],
    ) -> None:
        """Set callback for unhealthy strategy events."""
        self._on_unhealthy = callback

    def set_critical_callback(
        self,
        callback: Callable[[str, HealthCheckResult], Coroutine[Any, Any, None]],
    ) -> None:
        """Set callback for critical health events."""
        self._on_critical = callback

    async def start(self) -> None:
        """Start the health monitoring loop."""
        if self._running:
            logger.warning("health_monitor_already_running")
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "health_monitor_started",
            interval=self._check_interval,
        )

    async def stop(self) -> None:
        """Stop the health monitoring loop."""
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("health_monitor_stopped")

    async def check_strategy(self, strategy: StrategyInstance) -> HealthCheckResult:
        """
        Perform health check on a single strategy.

        Evaluates: error count, signal/trade freshness, state consistency.
        """
        strategy_id = strategy.strategy_id
        now = datetime.now(timezone.utc)

        # Strategy in ERROR — always critical
        if strategy.state == StrategyState.ERROR:
            return HealthCheckResult(
                strategy_id=strategy_id,
                status=HealthStatus.CRITICAL,
                message=f"Strategy in ERROR state: {strategy.metrics.last_error}",
                details={
                    "error_count": strategy.metrics.error_count,
                    "consecutive_errors": self._consecutive_errors.get(strategy_id, 0),
                    "state": strategy.state.value,
                },
            )

        # Strategy not running — skip detailed checks
        if strategy.state not in (StrategyState.ACTIVE, StrategyState.PAUSED):
            return HealthCheckResult(
                strategy_id=strategy_id,
                status=HealthStatus.HEALTHY,
                message=f"Strategy in {strategy.state.value} state",
            )

        issues: list[str] = []
        status = HealthStatus.HEALTHY

        # Check error count
        if strategy.metrics.error_count >= self._thresholds.max_error_count:
            issues.append(f"High error count: {strategy.metrics.error_count}")
            status = HealthStatus.UNHEALTHY

        # Check consecutive errors
        consec = self._consecutive_errors.get(strategy_id, 0)
        if consec >= self._thresholds.max_consecutive_errors:
            issues.append(f"Consecutive errors: {consec}")
            status = HealthStatus.CRITICAL

        # Check signal freshness (only for ACTIVE)
        if strategy.state == StrategyState.ACTIVE and strategy.metrics.last_signal_time:
            signal_age = (now - strategy.metrics.last_signal_time).total_seconds()
            if signal_age > self._thresholds.signal_timeout_seconds:
                issues.append(
                    f"No signals for {signal_age:.0f}s "
                    f"(threshold: {self._thresholds.signal_timeout_seconds}s)"
                )
                if status == HealthStatus.HEALTHY:
                    status = HealthStatus.DEGRADED

        # Check trade freshness
        if strategy.state == StrategyState.ACTIVE and strategy.metrics.last_trade_time:
            trade_age = (now - strategy.metrics.last_trade_time).total_seconds()
            if trade_age > self._thresholds.trade_timeout_seconds:
                issues.append(
                    f"No trades for {trade_age:.0f}s "
                    f"(threshold: {self._thresholds.trade_timeout_seconds}s)"
                )
                if status == HealthStatus.HEALTHY:
                    status = HealthStatus.DEGRADED

        message = "; ".join(issues) if issues else "All checks passed"

        result = HealthCheckResult(
            strategy_id=strategy_id,
            status=status,
            message=message,
            details={
                "error_count": strategy.metrics.error_count,
                "consecutive_errors": consec,
                "state": strategy.state.value,
            },
        )

        # Store in history
        history = self._health_history.setdefault(strategy_id, [])
        history.append(result)
        # Keep last 100 checks
        if len(history) > 100:
            self._health_history[strategy_id] = history[-100:]

        return result

    async def check_all(self) -> list[HealthCheckResult]:
        """Run health checks on all registered strategies."""
        results = []
        for strategy in self._registry.get_all():
            result = await self.check_strategy(strategy)
            results.append(result)

            # Trigger callbacks
            if result.status == HealthStatus.UNHEALTHY and self._on_unhealthy:
                await self._on_unhealthy(strategy.strategy_id, result)
            elif result.status == HealthStatus.CRITICAL and self._on_critical:
                await self._on_critical(strategy.strategy_id, result)

            # Auto-restart on error
            if (
                result.status == HealthStatus.CRITICAL
                and self._thresholds.auto_restart
                and strategy.state == StrategyState.ERROR
            ):
                await self._attempt_restart(strategy.strategy_id)

        return results

    async def _attempt_restart(self, strategy_id: str) -> bool:
        """Attempt to restart an errored strategy."""
        attempts = self._restart_counts.get(strategy_id, 0)

        if attempts >= self._thresholds.max_restart_attempts:
            logger.warning(
                "max_restart_attempts_reached",
                strategy_id=strategy_id,
                attempts=attempts,
            )
            return False

        logger.info(
            "attempting_strategy_restart",
            strategy_id=strategy_id,
            attempt=attempts + 1,
        )

        # Reset and restart
        if await self._registry.reset_strategy(strategy_id):
            if await self._registry.start_strategy(strategy_id):
                self._restart_counts[strategy_id] = attempts + 1
                self._consecutive_errors[strategy_id] = 0
                logger.info(
                    "strategy_restarted",
                    strategy_id=strategy_id,
                )
                return True

        return False

    def record_error(self, strategy_id: str) -> None:
        """Record an error for consecutive error tracking."""
        self._consecutive_errors[strategy_id] = self._consecutive_errors.get(strategy_id, 0) + 1

    def record_success(self, strategy_id: str) -> None:
        """Record a successful operation, resetting consecutive error counter."""
        self._consecutive_errors[strategy_id] = 0

    def get_health_summary(self) -> dict[str, Any]:
        """Get overall health summary."""
        strategies_health: dict[str, str] = {}
        for strategy in self._registry.get_all():
            history = self._health_history.get(strategy.strategy_id, [])
            if history:
                strategies_health[strategy.strategy_id] = history[-1].status.value
            else:
                strategies_health[strategy.strategy_id] = HealthStatus.HEALTHY.value

        # Overall status: worst of all strategies
        all_statuses = list(strategies_health.values())
        if HealthStatus.CRITICAL.value in all_statuses:
            overall = HealthStatus.CRITICAL
        elif HealthStatus.UNHEALTHY.value in all_statuses:
            overall = HealthStatus.UNHEALTHY
        elif HealthStatus.DEGRADED.value in all_statuses:
            overall = HealthStatus.DEGRADED
        else:
            overall = HealthStatus.HEALTHY

        return {
            "overall": overall.value,
            "monitor_running": self._running,
            "check_interval": self._check_interval,
            "strategies": strategies_health,
            "restart_counts": dict(self._restart_counts),
        }

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await self.check_all()
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("health_monitor_error", error=str(e), exc_info=True)
                await asyncio.sleep(self._check_interval)
