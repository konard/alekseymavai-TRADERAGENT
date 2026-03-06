"""
Bug detector for accelerated replay.

Wraps the BotOrchestrator during replay and periodically checks for
anomalies:
- Negative or inflated balances
- Orders open for an unreasonable number of candles
- Grid engine state diverging from exchange open orders
- Uncaught exceptions propagating from the orchestrator
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from bot.replay.replay_exchange import ReplayExchangeClient
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class BugDetector:
    """Collects anomalies detected during an accelerated replay run."""

    def __init__(
        self,
        exchange: ReplayExchangeClient,
        initial_balance: Decimal = Decimal("10000"),
        check_interval: int = 100,
    ) -> None:
        """
        Args:
            exchange: The replay exchange client to inspect.
            initial_balance: Starting USDT balance (for invariant checks).
            check_interval: Run checks every N candles.
        """
        self._exchange = exchange
        self._initial_balance = initial_balance
        self._check_interval = check_interval

        self.anomalies: list[dict[str, Any]] = []
        self.exceptions: list[dict[str, Any]] = []
        self._last_check_candle = 0
        self._checks_run = 0

    # =====================================================================
    # Periodic runner
    # =====================================================================

    def check_periodic(self, orchestrator: Any) -> None:
        """Run all checks if enough candles have elapsed since last check."""
        current = self._exchange.processed_candles
        if current - self._last_check_candle < self._check_interval:
            return
        self._last_check_candle = current
        self._checks_run += 1

        self.check_balance_invariant()
        self.check_orphaned_orders()
        self.check_state_consistency(orchestrator)

    # =====================================================================
    # Individual checks
    # =====================================================================

    def check_balance_invariant(self) -> None:
        """
        Free balance should never go negative.
        Total balance (free + used) should be plausible.
        """
        free = self._exchange._free_balance
        used = self._exchange._used_balance
        base = self._exchange._base_balance

        if free < 0:
            self._record(
                "CRITICAL",
                "balance_negative_free",
                {
                    "free_balance": str(free),
                    "used_balance": str(used),
                },
            )

        if used < 0:
            self._record(
                "CRITICAL",
                "balance_negative_used",
                {
                    "free_balance": str(free),
                    "used_balance": str(used),
                },
            )

        if base < 0:
            self._record(
                "CRITICAL",
                "balance_negative_base",
                {
                    "base_balance": str(base),
                },
            )

        # Sanity: total USDT should not exceed 10x initial (could indicate a bug)
        total = free + used
        if total > self._initial_balance * 10:
            self._record(
                "WARNING",
                "balance_suspiciously_high",
                {
                    "total_usdt": str(total),
                    "initial": str(self._initial_balance),
                },
            )

    def check_orphaned_orders(self, max_candles: int = 500) -> None:
        """Flag limit orders that have been open for too many candles."""
        current_ts = self._exchange._clock.current_time * 1000
        for oid, order in self._exchange._open_orders.items():
            order_ts = order.get("timestamp", 0)
            # 5-min candles → each candle = 300_000 ms
            age_candles = (current_ts - order_ts) / 300_000
            if age_candles > max_candles:
                self._record(
                    "WARNING",
                    "orphaned_order",
                    {
                        "order_id": oid,
                        "side": order.get("side"),
                        "price": order.get("price"),
                        "age_candles": int(age_candles),
                    },
                )

    def check_state_consistency(self, orchestrator: Any) -> None:
        """
        If the grid engine is active, its ``active_orders`` dict should be
        a subset of the exchange's open orders.
        """
        grid_engine = getattr(orchestrator, "grid_engine", None)
        if grid_engine is None or not grid_engine.active_orders:
            return

        exchange_open_ids = set(self._exchange._open_orders.keys())
        grid_ids = set(grid_engine.active_orders.keys())

        # Orders in grid engine but not on exchange
        ghost_ids = grid_ids - exchange_open_ids
        # Also remove orders that are in history as 'closed' (filled)
        for gid in list(ghost_ids):
            hist = self._exchange._order_history.get(gid, {})
            if hist.get("status") == "closed":
                ghost_ids.discard(gid)

        if ghost_ids:
            self._record(
                "WARNING",
                "grid_exchange_mismatch",
                {
                    "ghost_order_ids": list(ghost_ids),
                    "grid_count": len(grid_ids),
                    "exchange_open_count": len(exchange_open_ids),
                },
            )

    # =====================================================================
    # Exception capture
    # =====================================================================

    def record_exception(self, exc: Exception, context: str = "") -> None:
        """Record an exception that escaped the orchestrator."""
        entry = {
            "candle": self._exchange.processed_candles,
            "timestamp": self._exchange._clock.current_time,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "context": context,
        }
        self.exceptions.append(entry)
        logger.error(
            "replay_exception_captured",
            exc_type=type(exc).__name__,
            message=str(exc),
            context=context,
        )

    # =====================================================================
    # Report
    # =====================================================================

    def get_report(self) -> dict[str, Any]:
        """Return a summary of all detected issues."""
        severity_counts: dict[str, int] = {}
        for a in self.anomalies:
            sev = a["severity"]
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        return {
            "checks_run": self._checks_run,
            "total_anomalies": len(self.anomalies),
            "total_exceptions": len(self.exceptions),
            "severity_counts": severity_counts,
            "anomalies": self.anomalies,
            "exceptions": self.exceptions,
        }

    # =====================================================================
    # Internals
    # =====================================================================

    def _record(self, severity: str, code: str, details: dict[str, Any]) -> None:
        entry = {
            "candle": self._exchange.processed_candles,
            "timestamp": self._exchange._clock.current_time,
            "severity": severity,
            "code": code,
            **details,
        }
        self.anomalies.append(entry)
        log_fn = logger.warning if severity == "WARNING" else logger.error
        log_fn("replay_anomaly", severity=severity, code=code, **details)
