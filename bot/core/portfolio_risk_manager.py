"""
Portfolio Risk Manager — shared capital pool and cross-pair risk controls.

SharedCapitalPool:
    Tracks total capital and per-bot allocations. Enforces hard limits
    on how much each bot can use.

PortfolioRiskManager:
    Wraps SharedCapitalPool with portfolio-level stop-loss, correlation
    limits, and per-pair exposure caps.

    Per-symbol global stop-loss:
    When multiple strategies run on the same symbol simultaneously, their
    combined exposure is tracked. If aggregate risk exceeds
    ``global_stop_loss_pct``, ``force_close_all(symbol)`` is triggered
    automatically:
    - All strategy adapters registered for that symbol are asked to close
      their positions (via their ``get_open_positions()`` + close callbacks).
    - New entries for that symbol are blocked during a configurable cooldown.
    - The event is logged (Telegram notification is responsibility of the
      caller — pass ``on_force_close`` callback).

Usage:
    prm = PortfolioRiskManager(total_capital=Decimal("10000"))
    result = prm.check_allocation("bot_btc", Decimal("2000"))
    if result.approved:
        prm.confirm_allocation("bot_btc", Decimal("2000"))
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from bot.utils.logger import get_logger

logger = get_logger(__name__)


@runtime_checkable
class StrategyPositionProvider(Protocol):
    """
    Protocol for strategy adapters that expose open positions for
    per-symbol risk aggregation.
    """

    def get_open_positions(self) -> list[dict[str, Any]]:
        """
        Return a list of open position dicts, each containing at minimum:
          - 'symbol': str          — trading pair (e.g. 'BTC/USDT')
          - 'size': Decimal        — position size in quote currency
          - 'atr_stop_distance': Decimal — distance to stop-loss (risk per unit)
        """
        ...


class RiskCheckStatus(str, Enum):
    """Outcome of a risk check."""

    APPROVED = "approved"
    REJECTED_EXPOSURE = "rejected_exposure"
    REJECTED_PAIR_LIMIT = "rejected_pair_limit"
    REJECTED_PORTFOLIO_HALTED = "rejected_portfolio_halted"
    REJECTED_CORRELATION = "rejected_correlation"
    REJECTED_SYMBOL_COOLDOWN = "rejected_symbol_cooldown"


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


@dataclass
class SymbolRiskState:
    """Tracks per-symbol global stop-loss cooldown state."""

    symbol: str
    force_closed_at: float = 0.0  # monotonic timestamp of last force-close
    is_cooling_down: bool = False


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
        return sum(a.allocated for a in self._allocations.values())

    @property
    def total_deployed(self) -> Decimal:
        return sum(a.deployed for a in self._allocations.values())

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
        global_stop_loss_pct: float | None = None,
        force_close_cooldown_seconds: int = 1800,
    ) -> None:
        self.total_capital = total_capital
        self.max_single_pair_pct = Decimal(str(max_single_pair_pct))
        self.max_correlation_limit = max_correlation_limit
        self.portfolio_stop_loss_pct = Decimal(str(portfolio_stop_loss_pct))

        # Per-symbol global stop-loss (None → feature disabled)
        self.global_stop_loss_pct: Decimal | None = (
            Decimal(str(global_stop_loss_pct)) if global_stop_loss_pct is not None else None
        )
        self.force_close_cooldown_seconds: int = force_close_cooldown_seconds

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

        # Per-symbol cooldown state after force_close_all
        self._symbol_risk: dict[str, SymbolRiskState] = {}

        # Registered strategy position providers for per-symbol aggregation.
        # symbol → list of (strategy_name, provider)
        self._symbol_providers: dict[str, list[tuple[str, StrategyPositionProvider]]] = {}

        # Optional callback invoked when force_close_all is triggered.
        # Signature: on_force_close(symbol: str, reason: str) -> None
        self._on_force_close: Callable[[str, str], None] | None = None

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

        # 1b. Per-symbol cooldown check (after force_close_all)
        if symbol and self._is_symbol_cooling_down(symbol):
            state = self._symbol_risk[symbol]
            remaining = int(
                self.force_close_cooldown_seconds - (time.monotonic() - state.force_closed_at)
            )
            return RiskCheckResult(
                status=RiskCheckStatus.REJECTED_SYMBOL_COOLDOWN,
                approved=False,
                reason=(
                    f"Symbol {symbol} is in cooldown after force-close " f"({remaining}s remaining)"
                ),
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
    # Per-symbol position provider registry
    # ------------------------------------------------------------------

    def set_force_close_callback(self, callback: Callable[[str, str], None]) -> None:
        """
        Register a callback invoked when ``force_close_all`` is triggered.

        Args:
            callback: ``on_force_close(symbol, reason) -> None``.  Typically
                      sends a Telegram alert and initiates exchange-level
                      order cancellation.
        """
        self._on_force_close = callback

    def register_symbol_provider(
        self,
        symbol: str,
        strategy_name: str,
        provider: StrategyPositionProvider,
    ) -> None:
        """
        Register a strategy adapter as a position data provider for *symbol*.

        Args:
            symbol:         Trading pair (e.g. 'BTC/USDT').
            strategy_name:  Unique name of the strategy (for logging).
            provider:       Object that implements ``get_open_positions()``.
        """
        if symbol not in self._symbol_providers:
            self._symbol_providers[symbol] = []
        # Avoid duplicate registration
        for name, _ in self._symbol_providers[symbol]:
            if name == strategy_name:
                return
        self._symbol_providers[symbol].append((strategy_name, provider))
        logger.info(
            "symbol_provider_registered",
            symbol=symbol,
            strategy=strategy_name,
        )

    def unregister_symbol_provider(self, symbol: str, strategy_name: str) -> None:
        """Remove a previously registered position provider."""
        if symbol not in self._symbol_providers:
            return
        self._symbol_providers[symbol] = [
            (name, p) for name, p in self._symbol_providers[symbol] if name != strategy_name
        ]

    # ------------------------------------------------------------------
    # Per-symbol exposure aggregation
    # ------------------------------------------------------------------

    def _get_total_exposure(self, symbol: str) -> Decimal:
        """
        Sum aggregate risk exposure for all strategies on *symbol*.

        Exposure per position = ``size × atr_stop_distance``.
        Falls back to ``size`` alone if ``atr_stop_distance`` is absent.

        Returns:
            Total exposure in quote currency.
        """
        providers = self._symbol_providers.get(symbol, [])
        total = Decimal("0")
        for strategy_name, provider in providers:
            try:
                positions = provider.get_open_positions()
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "get_open_positions_failed",
                    strategy=strategy_name,
                    symbol=symbol,
                    error=str(exc),
                )
                continue
            for pos in positions:
                if pos.get("symbol") != symbol:
                    continue
                size = Decimal(str(pos.get("size", "0")))
                atr_stop = Decimal(str(pos.get("atr_stop_distance", "1")))
                total += size * atr_stop
        return total

    def check_global_stop_loss(self, symbol: str) -> bool:
        """
        Return True if per-symbol global stop-loss is triggered.

        Checks whether aggregate risk exposure on *symbol* expressed as a
        fraction of total capital exceeds ``global_stop_loss_pct``.
        Returns False when ``global_stop_loss_pct`` is not configured or
        the symbol has no registered providers.
        """
        if self.global_stop_loss_pct is None:
            return False
        if self.total_capital <= 0:
            return False
        exposure = self._get_total_exposure(symbol)
        exposure_pct = exposure / self.total_capital
        if exposure_pct >= self.global_stop_loss_pct:
            logger.warning(
                "global_stop_loss_exceeded",
                symbol=symbol,
                exposure=float(exposure),
                exposure_pct=float(exposure_pct),
                limit_pct=float(self.global_stop_loss_pct),
            )
            return True
        return False

    def force_close_all(self, symbol: str) -> None:
        """
        Trigger a global stop-loss for *symbol*.

        Actions:
        1. Mark symbol as cooling-down (blocks new entries for
           ``force_close_cooldown_seconds``).
        2. Invoke the registered ``on_force_close`` callback so the
           orchestrator can cancel open orders and close all positions
           with ``reduceOnly=True`` market orders.
        3. Log the event with structured fields.

        Args:
            symbol: Trading pair to force-close (e.g. 'BTC/USDT').
        """
        now = time.monotonic()
        state = self._symbol_risk.setdefault(symbol, SymbolRiskState(symbol=symbol))
        state.force_closed_at = now
        state.is_cooling_down = True

        exposure = self._get_total_exposure(symbol)
        reason = (
            f"Global stop-loss triggered for {symbol}: "
            f"aggregate exposure {float(exposure):.2f} "
            f">= {float(self.global_stop_loss_pct or 0) * 100:.1f}% "
            f"of total capital {float(self.total_capital):.2f}"
        )

        logger.critical(
            "force_close_all_triggered",
            symbol=symbol,
            exposure=float(exposure),
            total_capital=float(self.total_capital),
            cooldown_seconds=self.force_close_cooldown_seconds,
        )

        if self._on_force_close is not None:
            try:
                self._on_force_close(symbol, reason)
            except Exception as exc:  # pragma: no cover
                logger.error(
                    "force_close_callback_failed",
                    symbol=symbol,
                    error=str(exc),
                )

    def _is_symbol_cooling_down(self, symbol: str) -> bool:
        """Return True if *symbol* is within its post-force-close cooldown."""
        state = self._symbol_risk.get(symbol)
        if state is None or not state.is_cooling_down:
            return False
        elapsed = time.monotonic() - state.force_closed_at
        if elapsed >= self.force_close_cooldown_seconds:
            state.is_cooling_down = False
            logger.info("symbol_cooldown_expired", symbol=symbol)
            return False
        return True

    def tick_global_stop_loss(self, symbols: list[str] | None = None) -> list[str]:
        """
        Check all registered symbols (or *symbols* if given) for a global
        stop-loss breach and trigger ``force_close_all`` where needed.

        Intended to be called every tick from the main orchestrator loop.

        Returns:
            List of symbols for which ``force_close_all`` was triggered.
        """
        if self.global_stop_loss_pct is None:
            return []

        targets = symbols if symbols is not None else list(self._symbol_providers.keys())
        triggered: list[str] = []

        for symbol in targets:
            if self._is_symbol_cooling_down(symbol):
                continue
            if self.check_global_stop_loss(symbol):
                self.force_close_all(symbol)
                triggered.append(symbol)

        return triggered

    def get_risk_report(self) -> dict[str, Any]:
        """
        Return a per-symbol risk summary for monitoring.

        Includes:
        - current aggregate exposure per symbol
        - whether a symbol is in cooldown
        - global stop-loss configuration
        """
        report: dict[str, Any] = {
            "global_stop_loss_pct": (
                float(self.global_stop_loss_pct) if self.global_stop_loss_pct is not None else None
            ),
            "force_close_cooldown_seconds": self.force_close_cooldown_seconds,
            "symbols": {},
        }
        for symbol, providers in self._symbol_providers.items():
            exposure = self._get_total_exposure(symbol)
            exposure_pct = float(exposure / self.total_capital) if self.total_capital > 0 else 0.0
            state = self._symbol_risk.get(symbol)
            report["symbols"][symbol] = {
                "strategies": [name for name, _ in providers],
                "aggregate_exposure": float(exposure),
                "exposure_pct": round(exposure_pct * 100, 2),
                "is_cooling_down": self._is_symbol_cooling_down(symbol),
                "force_closed_at": state.force_closed_at if state else None,
            }
        return report

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
        total = sum(balances.values())
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
        key = tuple(sorted([symbol_a, symbol_b]))  # type: ignore[arg-type]
        self._correlation_overrides[key] = correlation  # type: ignore[index]

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
        key = tuple(sorted([sym_a, sym_b]))  # type: ignore[arg-type]
        if key in self._correlation_overrides:  # type: ignore[operator]
            return self._correlation_overrides[key]  # type: ignore[return-value]
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
