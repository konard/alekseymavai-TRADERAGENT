"""
Portfolio Risk Manager — shared capital pool and cross-pair risk controls.

SharedCapitalPool:
    Tracks total capital and per-bot allocations. Enforces hard limits
    on how much each bot can use.

PortfolioRiskManager:
    Wraps SharedCapitalPool with portfolio-level stop-loss, correlation
    limits, and per-pair exposure caps.

Usage:
    prm = PortfolioRiskManager(total_capital=Decimal("10000"))
    result = prm.check_allocation("bot_btc", Decimal("2000"))
    if result.approved:
        prm.confirm_allocation("bot_btc", Decimal("2000"))
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from bot.utils.logger import get_logger

logger = get_logger(__name__)


class RiskCheckStatus(str, Enum):
    """Outcome of a risk check."""

    APPROVED = "approved"
    REJECTED_EXPOSURE = "rejected_exposure"
    REJECTED_PAIR_LIMIT = "rejected_pair_limit"
    REJECTED_PORTFOLIO_HALTED = "rejected_portfolio_halted"
    REJECTED_CORRELATION = "rejected_correlation"


@dataclass
class RiskCheckResult:
    """Result of a portfolio risk check."""

    status: RiskCheckStatus
    approved: bool
    reason: str = ""
    max_allowed: Decimal = Decimal("0")

    def __bool__(self) -> bool:
        return self.approved


@dataclass
class BotAllocation:
    """Tracks capital allocation for a single bot."""

    bot_name: str
    allocated: Decimal = Decimal("0")  # reserved/committed capital
    deployed: Decimal = Decimal("0")  # actually in open positions
    max_limit: Decimal = Decimal("0")  # hard cap for this bot


class SharedCapitalPool:
    """
    Manages a shared pool of capital across multiple bots.

    Each bot has a reserved allocation that cannot exceed its individual
    limit. The pool enforces a global utilisation ceiling.
    """

    def __init__(
        self,
        total_capital: Decimal,
        max_utilization_pct: float = 0.80,
    ) -> None:
        self.total_capital = total_capital
        self.max_utilization_pct = Decimal(str(max_utilization_pct))
        self._allocations: dict[str, BotAllocation] = {}

    # ------------------------------------------------------------------
    # Capital registration / release
    # ------------------------------------------------------------------

    def register_bot(self, bot_name: str, max_limit: Decimal) -> None:
        """Register a bot with an individual capital cap."""
        if bot_name not in self._allocations:
            self._allocations[bot_name] = BotAllocation(bot_name=bot_name, max_limit=max_limit)

    def allocate(self, bot_name: str, amount: Decimal) -> bool:
        """
        Reserve *amount* for *bot_name*.

        Returns True if allocation was successful.
        """
        if bot_name not in self._allocations:
            # Auto-register with no individual cap (pool-level caps apply)
            self._allocations[bot_name] = BotAllocation(
                bot_name=bot_name,
                max_limit=self.total_capital,
            )

        alloc = self._allocations[bot_name]

        # Individual limit
        if alloc.max_limit > 0 and alloc.allocated + amount > alloc.max_limit:
            return False

        # Global utilisation cap
        if self.total_allocated + amount > self.total_capital * self.max_utilization_pct:
            return False

        alloc.allocated += amount
        logger.debug(
            "capital_allocated",
            bot_name=bot_name,
            amount=float(amount),
            total_allocated=float(self.total_allocated),
        )
        return True

    def release(self, bot_name: str, amount: Decimal) -> None:
        """Return *amount* to the pool for *bot_name*."""
        if bot_name not in self._allocations:
            return
        alloc = self._allocations[bot_name]
        alloc.allocated = max(Decimal("0"), alloc.allocated - amount)

    def update_deployed(self, bot_name: str, deployed: Decimal) -> None:
        """Update the deployed (in-position) amount for a bot."""
        if bot_name in self._allocations:
            self._allocations[bot_name].deployed = deployed

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def total_allocated(self) -> Decimal:
        return sum((a.allocated for a in self._allocations.values()), Decimal("0"))

    @property
    def total_deployed(self) -> Decimal:
        return sum((a.deployed for a in self._allocations.values()), Decimal("0"))

    def get_utilization(self) -> float:
        """Fraction of total capital currently allocated (0.0–1.0)."""
        if self.total_capital <= 0:
            return 0.0
        return float(self.total_allocated / self.total_capital)

    def get_available(self) -> Decimal:
        """Available capital that can still be allocated."""
        max_allocatable = self.total_capital * self.max_utilization_pct
        return max(Decimal("0"), max_allocatable - self.total_allocated)

    def get_bot_allocation(self, bot_name: str) -> BotAllocation | None:
        return self._allocations.get(bot_name)

    def get_summary(self) -> dict[str, Any]:
        return {
            "total_capital": float(self.total_capital),
            "total_allocated": float(self.total_allocated),
            "total_deployed": float(self.total_deployed),
            "utilization_pct": round(self.get_utilization() * 100, 2),
            "available": float(self.get_available()),
            "bots": {
                name: {
                    "allocated": float(a.allocated),
                    "deployed": float(a.deployed),
                    "max_limit": float(a.max_limit),
                }
                for name, a in self._allocations.items()
            },
        }


class PortfolioRiskManager:
    """
    Cross-pair risk controls for a multi-bot portfolio.

    Wraps SharedCapitalPool with:
    - per-pair exposure cap (max_single_pair_pct)
    - global exposure cap (max_total_exposure_pct)
    - portfolio stop-loss (portfolio_stop_loss_pct)
    - correlation limit (max_correlation_limit) — blocks adding highly
      correlated pairs to the same portfolio

    Example::

        prm = PortfolioRiskManager(total_capital=Decimal("10000"))
        result = prm.check_allocation("bot_btc", Decimal("1500"), balance=Decimal("10000"))
        if result.approved:
            prm.confirm_allocation("bot_btc", Decimal("1500"))
    """

    def __init__(
        self,
        total_capital: Decimal,
        max_total_exposure_pct: float = 0.80,
        max_single_pair_pct: float = 0.25,
        max_correlation_limit: float = 0.80,
        portfolio_stop_loss_pct: float = 0.15,
    ) -> None:
        self.total_capital = total_capital
        self.max_single_pair_pct = Decimal(str(max_single_pair_pct))
        self.max_correlation_limit = max_correlation_limit
        self.portfolio_stop_loss_pct = Decimal(str(portfolio_stop_loss_pct))

        self._pool = SharedCapitalPool(
            total_capital=total_capital,
            max_utilization_pct=max_total_exposure_pct,
        )

        # Peak value for portfolio-level drawdown tracking
        self._peak_value: Decimal = total_capital
        self._current_value: Decimal = total_capital
        self._halted: bool = False
        self._halt_reason: str = ""

        # Correlation tracking: symbol → set of symbols already active
        self._active_symbols: set[str] = set()
        # Manual correlation overrides: (sym_a, sym_b) → float
        self._correlation_overrides: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def check_allocation(
        self,
        bot_name: str,
        amount: Decimal,
        balance: Decimal | None = None,
        symbol: str | None = None,
    ) -> RiskCheckResult:
        """
        Check whether a bot may allocate *amount* from the shared pool.

        Args:
            bot_name:   Unique bot identifier.
            amount:     Requested capital amount.
            balance:    Current portfolio value (for drawdown check). If
                        omitted, uses *total_capital*.
            symbol:     Trading symbol (for correlation check). Optional.

        Returns:
            RiskCheckResult indicating approval or the rejection reason.
        """
        reference = balance if balance is not None else self.total_capital

        # 1. Portfolio halt check
        if self._halted:
            return RiskCheckResult(
                status=RiskCheckStatus.REJECTED_PORTFOLIO_HALTED,
                approved=False,
                reason=f"Portfolio halted: {self._halt_reason}",
            )

        # 2. Per-pair cap check
        per_pair_limit = reference * self.max_single_pair_pct
        if amount > per_pair_limit:
            return RiskCheckResult(
                status=RiskCheckStatus.REJECTED_PAIR_LIMIT,
                approved=False,
                reason=(
                    f"Amount {amount} exceeds per-pair limit "
                    f"{per_pair_limit:.2f} ({float(self.max_single_pair_pct)*100:.0f}%)"
                ),
                max_allowed=per_pair_limit,
            )

        # 3. Total exposure check (delegate to pool)
        available = self._pool.get_available()
        if amount > available:
            return RiskCheckResult(
                status=RiskCheckStatus.REJECTED_EXPOSURE,
                approved=False,
                reason=(
                    f"Amount {amount} exceeds available pool capital {available:.2f}. "
                    f"Utilization: {self._pool.get_utilization()*100:.1f}%"
                ),
                max_allowed=available,
            )

        # 4. Correlation check (simple: block if too many correlated symbols)
        if symbol and self._is_too_correlated(symbol):
            return RiskCheckResult(
                status=RiskCheckStatus.REJECTED_CORRELATION,
                approved=False,
                reason=(
                    f"Symbol {symbol} is too correlated with existing positions "
                    f"(limit={self.max_correlation_limit:.2f})"
                ),
            )

        return RiskCheckResult(
            status=RiskCheckStatus.APPROVED,
            approved=True,
            max_allowed=min(amount, available, per_pair_limit),
        )

    def confirm_allocation(self, bot_name: str, amount: Decimal, symbol: str | None = None) -> None:
        """Commit an allocation after a successful check_allocation."""
        self._pool.allocate(bot_name, amount)
        if symbol:
            self._active_symbols.add(symbol)

    def release_allocation(self, bot_name: str, amount: Decimal, symbol: str | None = None) -> None:
        """Return capital to the pool when a bot closes or is removed."""
        self._pool.release(bot_name, amount)
        if symbol and symbol in self._active_symbols:
            self._active_symbols.discard(symbol)

    # ------------------------------------------------------------------
    # Balance / drawdown tracking
    # ------------------------------------------------------------------

    def update_all_balances(self, balances: dict[str, Decimal]) -> None:
        """
        Update current portfolio value from per-bot balance snapshots.

        Also updates the peak value and triggers portfolio halt if the
        drawdown exceeds portfolio_stop_loss_pct.

        Args:
            balances: Mapping of bot_name → current balance.
        """
        total = sum(balances.values(), Decimal("0"))
        self._current_value = total

        if total > self._peak_value:
            self._peak_value = total

        drawdown_pct = (
            (self._peak_value - total) / self._peak_value if self._peak_value > 0 else Decimal("0")
        )

        if not self._halted and drawdown_pct >= self.portfolio_stop_loss_pct:
            self._halted = True
            self._halt_reason = (
                f"Portfolio drawdown {float(drawdown_pct)*100:.1f}% "
                f">= stop-loss {float(self.portfolio_stop_loss_pct)*100:.0f}%"
            )
            logger.warning(
                "portfolio_halt_triggered",
                drawdown_pct=float(drawdown_pct),
                current_value=float(total),
                peak_value=float(self._peak_value),
            )

    def is_portfolio_halted(self) -> bool:
        """Return True if portfolio stop-loss has been triggered."""
        return self._halted

    def resume(self) -> None:
        """Manually resume trading after a portfolio halt."""
        self._halted = False
        self._halt_reason = ""
        self._peak_value = self._current_value
        logger.info("portfolio_halt_cleared", current_value=float(self._current_value))

    # ------------------------------------------------------------------
    # Correlation helpers
    # ------------------------------------------------------------------

    def set_correlation(self, symbol_a: str, symbol_b: str, correlation: float) -> None:
        """Register a known correlation between two symbols."""
        sorted_pair = sorted([symbol_a, symbol_b])
        key: tuple[str, str] = (sorted_pair[0], sorted_pair[1])
        self._correlation_overrides[key] = correlation

    def _is_too_correlated(self, symbol: str) -> bool:
        """
        Check if *symbol* is too correlated with any active symbol.

        Uses _correlation_overrides if available, otherwise applies a
        simple heuristic: BTC/ETH pairs are assumed correlated (>0.8).
        """
        for active in self._active_symbols:
            corr = self._get_correlation(symbol, active)
            if corr >= self.max_correlation_limit:
                return True
        return False

    def _get_correlation(self, sym_a: str, sym_b: str) -> float:
        sorted_pair = sorted([sym_a, sym_b])
        key: tuple[str, str] = (sorted_pair[0], sorted_pair[1])
        if key in self._correlation_overrides:
            return self._correlation_overrides[key]
        # Simple heuristic: same base currency → assume high correlation
        base_a = sym_a.split("/")[0].split("USDT")[0]
        base_b = sym_b.split("/")[0].split("USDT")[0]
        if base_a == base_b:
            return 1.0
        # BTC and ETH often correlated
        if {base_a, base_b} <= {"BTC", "ETH"}:
            return 0.85
        return 0.3  # Default: low correlation

    # ------------------------------------------------------------------
    # Queries / reporting
    # ------------------------------------------------------------------

    def get_summary(self) -> dict[str, Any]:
        """Return a human-readable portfolio summary."""
        return {
            "total_capital": float(self.total_capital),
            "current_value": float(self._current_value),
            "peak_value": float(self._peak_value),
            "drawdown_pct": round(
                (
                    float((self._peak_value - self._current_value) / self._peak_value * 100)
                    if self._peak_value > 0
                    else 0.0
                ),
                2,
            ),
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "active_symbols": sorted(self._active_symbols),
            "pool": self._pool.get_summary(),
        }
