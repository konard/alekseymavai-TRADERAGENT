"""
Gradual capital deployment manager for TRADERAGENT.

Implements the phased capital scaling strategy:
- Phase 1: 5% capital allocation (3 days monitoring)
- Phase 2: 25% allocation (1 week monitoring)
- Phase 3: 100% allocation (production)

Each phase has performance gates that must be met before scaling up.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from bot.utils.logger import get_logger

logger = get_logger(__name__)


class DeploymentPhase(str, Enum):
    """Capital deployment phases."""

    PHASE_1 = "phase_1_5pct"
    PHASE_2 = "phase_2_25pct"
    PHASE_3 = "phase_3_100pct"
    HALTED = "halted"


@dataclass
class PhaseConfig:
    """Configuration for a deployment phase."""

    phase: DeploymentPhase
    allocation_pct: Decimal
    min_duration_days: int
    max_drawdown_pct: Decimal
    min_trades: int
    min_win_rate: Decimal


@dataclass
class PhaseMetrics:
    """Recorded metrics for a deployment phase."""

    phase: DeploymentPhase
    started_at: datetime
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: Decimal = Decimal("0")
    max_drawdown_pct: Decimal = Decimal("0")
    peak_balance: Decimal = Decimal("0")
    current_balance: Decimal = Decimal("0")
    errors_count: int = 0

    @property
    def win_rate(self) -> Decimal:
        if self.total_trades == 0:
            return Decimal("0")
        return Decimal(str(self.winning_trades)) / Decimal(str(self.total_trades))

    @property
    def duration_days(self) -> int:
        delta = datetime.now(timezone.utc) - self.started_at
        return delta.days

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "started_at": self.started_at.isoformat(),
            "duration_days": self.duration_days,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "win_rate": float(self.win_rate),
            "total_pnl": str(self.total_pnl),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "current_balance": str(self.current_balance),
            "errors_count": self.errors_count,
        }


@dataclass
class ScalingDecision:
    """Decision on whether to scale up."""

    can_scale: bool
    current_phase: DeploymentPhase
    next_phase: DeploymentPhase | None
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


# Default phase configurations
DEFAULT_PHASES = [
    PhaseConfig(
        phase=DeploymentPhase.PHASE_1,
        allocation_pct=Decimal("0.05"),
        min_duration_days=3,
        max_drawdown_pct=Decimal("0.05"),
        min_trades=5,
        min_win_rate=Decimal("0.40"),
    ),
    PhaseConfig(
        phase=DeploymentPhase.PHASE_2,
        allocation_pct=Decimal("0.25"),
        min_duration_days=7,
        max_drawdown_pct=Decimal("0.10"),
        min_trades=20,
        min_win_rate=Decimal("0.45"),
    ),
    PhaseConfig(
        phase=DeploymentPhase.PHASE_3,
        allocation_pct=Decimal("1.00"),
        min_duration_days=0,  # No minimum for full deployment
        max_drawdown_pct=Decimal("0.15"),
        min_trades=0,
        min_win_rate=Decimal("0"),
    ),
]


class CapitalManager:
    """
    Manages gradual capital deployment with performance gates.

    Usage:
        cm = CapitalManager(total_capital=Decimal("10000"))
        cm.start_phase_1()
        # ... trading happens ...
        cm.record_trade(won=True, pnl=Decimal("50"))
        decision = cm.evaluate_scaling()
        if decision.can_scale:
            cm.advance_phase()
    """

    def __init__(
        self,
        total_capital: Decimal = Decimal("10000"),
        phases: list[PhaseConfig] | None = None,
    ):
        self.total_capital = total_capital
        self.phases = phases or DEFAULT_PHASES
        self.current_phase: DeploymentPhase = DeploymentPhase.HALTED
        self.current_metrics: PhaseMetrics | None = None
        self.phase_history: list[PhaseMetrics] = []

    @property
    def allocated_capital(self) -> Decimal:
        """Capital allocated for current phase."""
        if self.current_phase == DeploymentPhase.HALTED:
            return Decimal("0")
        config = self._get_phase_config(self.current_phase)
        return self.total_capital * config.allocation_pct

    def _get_phase_config(self, phase: DeploymentPhase) -> PhaseConfig:
        for p in self.phases:
            if p.phase == phase:
                return p
        raise ValueError(f"Unknown phase: {phase}")

    def start_phase_1(self) -> Decimal:
        """Start Phase 1: 5% capital allocation."""
        self.current_phase = DeploymentPhase.PHASE_1
        self.current_metrics = PhaseMetrics(
            phase=DeploymentPhase.PHASE_1,
            started_at=datetime.now(timezone.utc),
            current_balance=self.allocated_capital,
            peak_balance=self.allocated_capital,
        )
        logger.info(
            "capital_phase_started",
            phase="phase_1",
            allocation=str(self.allocated_capital),
        )
        return self.allocated_capital

    def record_trade(self, won: bool, pnl: Decimal) -> None:
        """Record a trade result."""
        if not self.current_metrics:
            raise RuntimeError("No active phase")

        self.current_metrics.total_trades += 1
        if won:
            self.current_metrics.winning_trades += 1
        self.current_metrics.total_pnl += pnl
        self.current_metrics.current_balance += pnl

        if self.current_metrics.current_balance > self.current_metrics.peak_balance:
            self.current_metrics.peak_balance = self.current_metrics.current_balance

        # Update drawdown
        if self.current_metrics.peak_balance > 0:
            drawdown = (
                self.current_metrics.peak_balance - self.current_metrics.current_balance
            ) / self.current_metrics.peak_balance
            if drawdown > self.current_metrics.max_drawdown_pct:
                self.current_metrics.max_drawdown_pct = drawdown

    def record_error(self) -> None:
        """Record an error event."""
        if self.current_metrics:
            self.current_metrics.errors_count += 1

    def evaluate_scaling(self) -> ScalingDecision:
        """Evaluate whether performance gates are met for scaling up."""
        if not self.current_metrics:
            return ScalingDecision(
                can_scale=False,
                current_phase=self.current_phase,
                next_phase=None,
                blockers=["No active phase"],
            )

        config = self._get_phase_config(self.current_phase)
        metrics = self.current_metrics

        # Determine next phase
        phase_order = [DeploymentPhase.PHASE_1, DeploymentPhase.PHASE_2, DeploymentPhase.PHASE_3]
        current_idx = phase_order.index(self.current_phase)
        if current_idx >= len(phase_order) - 1:
            return ScalingDecision(
                can_scale=False,
                current_phase=self.current_phase,
                next_phase=None,
                reasons=["Already at maximum allocation"],
            )
        next_phase = phase_order[current_idx + 1]

        # Check gates
        blockers = []
        reasons = []

        # Duration check
        if metrics.duration_days < config.min_duration_days:
            blockers.append(
                f"Duration {metrics.duration_days}d < required {config.min_duration_days}d"
            )
        else:
            reasons.append(f"Duration gate passed ({metrics.duration_days}d)")

        # Min trades check
        if metrics.total_trades < config.min_trades:
            blockers.append(f"Trades {metrics.total_trades} < required {config.min_trades}")
        else:
            reasons.append(f"Trade count gate passed ({metrics.total_trades})")

        # Win rate check
        if metrics.total_trades > 0 and metrics.win_rate < config.min_win_rate:
            blockers.append(f"Win rate {metrics.win_rate:.2%} < required {config.min_win_rate:.2%}")
        elif metrics.total_trades > 0:
            reasons.append(f"Win rate gate passed ({metrics.win_rate:.2%})")

        # Drawdown check
        if metrics.max_drawdown_pct > config.max_drawdown_pct:
            blockers.append(
                f"Drawdown {metrics.max_drawdown_pct:.2%} > max {config.max_drawdown_pct:.2%}"
            )
        else:
            reasons.append(f"Drawdown gate passed ({metrics.max_drawdown_pct:.2%})")

        # PnL check (must be positive)
        if metrics.total_pnl < 0:
            blockers.append(f"Net PnL is negative ({metrics.total_pnl})")
        else:
            reasons.append(f"PnL positive ({metrics.total_pnl})")

        can_scale = len(blockers) == 0

        return ScalingDecision(
            can_scale=can_scale,
            current_phase=self.current_phase,
            next_phase=next_phase,
            reasons=reasons,
            blockers=blockers,
        )

    def advance_phase(self) -> Decimal:
        """Advance to the next deployment phase."""
        decision = self.evaluate_scaling()
        if not decision.can_scale:
            raise RuntimeError(f"Cannot advance: {', '.join(decision.blockers)}")

        # Archive current metrics
        if self.current_metrics:
            self.phase_history.append(self.current_metrics)

        assert decision.next_phase is not None  # guaranteed by can_scale being True
        self.current_phase = decision.next_phase
        self.current_metrics = PhaseMetrics(
            phase=self.current_phase,
            started_at=datetime.now(timezone.utc),
            current_balance=self.allocated_capital,
            peak_balance=self.allocated_capital,
        )

        logger.info(
            "capital_phase_advanced",
            phase=self.current_phase.value,
            allocation=str(self.allocated_capital),
        )
        return self.allocated_capital

    def halt(self, reason: str = "manual") -> None:
        """Halt deployment — stop all trading."""
        if self.current_metrics:
            self.phase_history.append(self.current_metrics)
        self.current_phase = DeploymentPhase.HALTED
        self.current_metrics = None
        logger.warning("capital_deployment_halted", reason=reason)

    def get_report(self) -> dict[str, Any]:
        """Get comprehensive deployment report."""
        return {
            "total_capital": str(self.total_capital),
            "current_phase": self.current_phase.value,
            "allocated_capital": str(self.allocated_capital),
            "current_metrics": self.current_metrics.to_dict() if self.current_metrics else None,
            "phase_history": [m.to_dict() for m in self.phase_history],
        }
