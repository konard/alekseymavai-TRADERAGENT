"""
RiskManager - Risk management and capital control
Handles balance checking, position sizing, stop-loss, drawdown monitoring
"""

from decimal import Decimal

from bot.utils.logger import get_logger

logger = get_logger(__name__)


class RiskCheckResult:
    """Result of a risk check"""

    def __init__(self, allowed: bool, reason: str | None = None):
        self.allowed = allowed
        self.reason = reason

    def __bool__(self) -> bool:
        return self.allowed

    def __repr__(self) -> str:
        if self.allowed:
            return "RiskCheckResult(allowed=True)"
        return f"RiskCheckResult(allowed=False, reason='{self.reason}')"


class RiskManager:
    """
    Risk management and capital control system.

    Features:
    - Monitor available balance
    - Validate order sizes against minimums
    - Portfolio-level stop-loss protection
    - Drawdown monitoring with automatic halt
    - Position size limits
    - Daily loss tracking
    """

    def __init__(
        self,
        max_position_size: Decimal,
        min_order_size: Decimal,
        stop_loss_percentage: Decimal | None = None,
        max_daily_loss: Decimal | None = None,
        max_position_size_per_trade: Decimal | None = None,
    ):
        """
        Initialize Risk Manager.

        Args:
            max_position_size: Maximum total position size in quote currency
            min_order_size: Minimum order size in quote currency
            stop_loss_percentage: Optional portfolio stop-loss percentage
            max_daily_loss: Optional maximum daily loss in quote currency
            max_position_size_per_trade: Optional max size of a single entry in quote currency
        """
        if max_position_size <= 0:
            raise ValueError("max_position_size must be positive")
        if min_order_size <= 0:
            raise ValueError("min_order_size must be positive")
        if stop_loss_percentage is not None and (
            stop_loss_percentage <= 0 or stop_loss_percentage > 1
        ):
            raise ValueError("stop_loss_percentage must be between 0 and 1")
        if max_daily_loss is not None and max_daily_loss <= 0:
            raise ValueError("max_daily_loss must be positive")
        if max_position_size_per_trade is not None and max_position_size_per_trade <= 0:
            raise ValueError("max_position_size_per_trade must be positive")

        self.max_position_size = max_position_size
        self.min_order_size = min_order_size
        self.stop_loss_percentage = stop_loss_percentage
        self.max_daily_loss = max_daily_loss
        self.max_position_size_per_trade = max_position_size_per_trade

        # State tracking
        self.current_position_value = Decimal("0")
        self.initial_balance: Decimal | None = None
        self.current_balance: Decimal | None = None
        self.daily_loss = Decimal("0")
        self.peak_balance: Decimal | None = None
        self._peak_balance_today: Decimal | None = None  # for net daily loss calculation
        self.is_halted = False
        self.halt_reason: str | None = None

        # Statistics
        self.total_trades = 0
        self.rejected_trades = 0
        self.stop_loss_triggers = 0

        logger.info(
            "RiskManager initialized",
            max_position_size=float(max_position_size),
            min_order_size=float(min_order_size),
            stop_loss_pct=float(stop_loss_percentage) if stop_loss_percentage else None,
        )

    def initialize_balance(self, balance: Decimal) -> None:
        """
        Initialize balance tracking.

        Args:
            balance: Initial account balance
        """
        self.initial_balance = balance
        self.current_balance = balance
        self.peak_balance = balance
        self._peak_balance_today = balance

        logger.info("Balance initialized", balance=float(balance))

    def update_balance(self, balance: Decimal) -> None:
        """
        Update current balance and check risk limits.

        Args:
            balance: Current account balance
        """
        self.current_balance = balance

        # Track net daily loss from today's peak (not cumulative).
        # Old logic summed every dip: daily_loss += abs(change).
        # That caused false halts: with 4 strategies × 288 bars/day,
        # normal oscillations easily exceed the daily limit.
        # Net loss = max(0, peak_today − current) only counts the
        # actual drawdown from the best point of the day.
        if self._peak_balance_today is not None:
            if balance > self._peak_balance_today:
                self._peak_balance_today = balance
            self.daily_loss = max(Decimal("0"), self._peak_balance_today - balance)
        else:
            self._peak_balance_today = balance
            self.daily_loss = Decimal("0")

        # Fix initial_balance if it was 0 at startup
        if self.initial_balance is not None and self.initial_balance == 0 and balance > 0:
            self.initial_balance = balance
            logger.info("Initial balance updated from 0", balance=float(balance))

        # Update peak balance
        if self.peak_balance is None or balance > self.peak_balance:
            self.peak_balance = balance

        # Check stop-loss
        self._check_portfolio_stop_loss()

        # Check daily loss limit
        self._check_daily_loss_limit()

        logger.debug("Balance updated", balance=float(balance))

    def check_order_size(self, order_value: Decimal) -> RiskCheckResult:
        """
        Check if order size meets minimum requirements.

        Args:
            order_value: Value of the order in quote currency

        Returns:
            RiskCheckResult indicating if order is allowed
        """
        if self.is_halted:
            return RiskCheckResult(False, self.halt_reason or "System halted")

        if order_value < self.min_order_size:
            self.rejected_trades += 1
            return RiskCheckResult(
                False,
                f"Order size {order_value} below minimum {self.min_order_size}",
            )

        return RiskCheckResult(True)

    def check_position_limit(
        self,
        current_position: Decimal,
        additional_size: Decimal,
        direction: str = "long",
    ) -> RiskCheckResult:
        """
        Check if adding to position would exceed the net exposure limit.

        ``current_position`` is the **signed** net exposure of the portfolio
        (positive = net long, negative = net short).  ``additional_size`` is
        always positive (the magnitude of the new trade).  ``direction``
        determines whether the trade adds to long or short exposure.

        Hedged portfolios benefit from this: LONG $1000 + SHORT $800 = $200
        net long exposure, not $1800 gross.

        Args:
            current_position: Signed net portfolio exposure (positive=long).
            additional_size: Size of the new order (always positive).
            direction: ``"long"`` or ``"short"`` for the new trade.

        Returns:
            RiskCheckResult indicating if addition is allowed
        """
        if self.is_halted:
            return RiskCheckResult(False, self.halt_reason or "System halted")

        if direction == "short":
            new_position = current_position - additional_size
        else:
            new_position = current_position + additional_size

        # Use absolute net exposure for the limit check so both directions
        # are bounded symmetrically by max_position_size.
        if abs(new_position) > self.max_position_size:
            self.rejected_trades += 1
            return RiskCheckResult(
                False,
                f"Net position {new_position} would exceed max {self.max_position_size}",
            )

        if self.max_position_size_per_trade is not None:
            if additional_size > self.max_position_size_per_trade:
                self.rejected_trades += 1
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Single trade size {additional_size:.2f} exceeds per-trade limit {self.max_position_size_per_trade:.2f}",
                )

        return RiskCheckResult(True)

    def check_available_balance(
        self, required_balance: Decimal, available_balance: Decimal
    ) -> RiskCheckResult:
        """
        Check if sufficient balance is available.

        Args:
            required_balance: Required balance for operation
            available_balance: Currently available balance

        Returns:
            RiskCheckResult indicating if operation is allowed
        """
        if self.is_halted:
            return RiskCheckResult(False, self.halt_reason or "System halted")

        if available_balance < required_balance:
            self.rejected_trades += 1
            return RiskCheckResult(
                False,
                f"Insufficient balance: need {required_balance}, have {available_balance}",
            )

        return RiskCheckResult(True)

    def check_trade(
        self,
        order_value: Decimal,
        current_position: Decimal,
        available_balance: Decimal,
        direction: str = "long",
    ) -> RiskCheckResult:
        """
        Comprehensive trade check combining all risk validations.

        Args:
            order_value: Value of the proposed order
            current_position: Signed net portfolio exposure (positive=long)
            available_balance: Available balance
            direction: ``"long"`` or ``"short"`` for the new trade

        Returns:
            RiskCheckResult indicating if trade is allowed
        """
        # Check system halt
        if self.is_halted:
            return RiskCheckResult(False, self.halt_reason or "System halted")

        # Check order size
        size_check = self.check_order_size(order_value)
        if not size_check:
            return size_check

        # Check position limit (direction-aware net exposure check)
        position_check = self.check_position_limit(current_position, order_value, direction)
        if not position_check:
            return position_check

        # Check available balance
        balance_check = self.check_available_balance(order_value, available_balance)
        if not balance_check:
            return balance_check

        self.total_trades += 1
        return RiskCheckResult(True)

    def _check_portfolio_stop_loss(self) -> None:
        """Check portfolio-level stop-loss"""
        if (
            self.stop_loss_percentage is None
            or self.initial_balance is None
            or self.current_balance is None
            or self.initial_balance == 0
        ):
            return

        if self.is_halted:
            return

        loss_percentage = (self.initial_balance - self.current_balance) / self.initial_balance

        if loss_percentage >= self.stop_loss_percentage:
            self.is_halted = True
            self.halt_reason = f"Portfolio stop-loss triggered: {float(loss_percentage):.2%} loss"
            self.stop_loss_triggers += 1

            logger.critical(
                "PORTFOLIO STOP-LOSS TRIGGERED",
                loss_percentage=float(loss_percentage),
                current_balance=float(self.current_balance),
                initial_balance=float(self.initial_balance),
            )

    def _check_daily_loss_limit(self) -> None:
        """Check daily loss limit"""
        if self.max_daily_loss is None:
            return

        if self.is_halted:
            return

        if self.daily_loss >= self.max_daily_loss:
            self.is_halted = True
            self.halt_reason = (
                f"Daily loss limit reached: {float(self.daily_loss)} / {float(self.max_daily_loss)}"
            )

            logger.critical(
                "DAILY LOSS LIMIT REACHED",
                daily_loss=float(self.daily_loss),
                max_daily_loss=float(self.max_daily_loss),
            )

    def get_drawdown(self) -> Decimal | None:
        """
        Calculate current drawdown from peak balance.

        Returns:
            Drawdown percentage, or None if not enough data
        """
        if self.peak_balance is None or self.current_balance is None:
            return None

        if self.peak_balance == 0:
            return Decimal("0")

        drawdown = (self.peak_balance - self.current_balance) / self.peak_balance
        return drawdown

    def get_pnl_percentage(self) -> Decimal | None:
        """
        Calculate total PnL percentage from initial balance.

        Returns:
            PnL percentage, or None if not initialized
        """
        if self.initial_balance is None or self.current_balance is None:
            return None

        if self.initial_balance == 0:
            return Decimal("0")

        pnl_pct = (self.current_balance - self.initial_balance) / self.initial_balance
        return pnl_pct

    def reset_daily_loss(self) -> None:
        """Reset daily loss counter (call at start of new day).

        If the system was halted due to daily loss limit (not portfolio stop-loss),
        also clears the halt so trading can resume on the next trading day.
        """
        # Clear daily-loss-specific halt so trading resumes each new day.
        # Portfolio stop-loss halts (halt_reason contains "stop-loss") are permanent
        # and must be reset manually via resume().
        if self.is_halted and self.halt_reason and "daily" in self.halt_reason.lower():
            self.is_halted = False
            self.halt_reason = None
            logger.info("Daily loss halt cleared — new trading day starts")
        self.daily_loss = Decimal("0")
        self._peak_balance_today = self.current_balance
        logger.info("Daily loss counter reset")

    def resume(self) -> None:
        """Resume trading after halt (use with caution)"""
        if not self.is_halted:
            logger.warning("Cannot resume - system is not halted")
            return

        self.is_halted = False
        old_reason = self.halt_reason
        self.halt_reason = None

        logger.warning("System resumed from halt", previous_reason=old_reason)

    def get_risk_status(self) -> dict:
        """
        Get current risk management status.

        Returns:
            Dictionary with risk status information
        """
        return {
            "is_halted": self.is_halted,
            "halt_reason": self.halt_reason,
            "current_balance": float(self.current_balance) if self.current_balance else None,
            "initial_balance": float(self.initial_balance) if self.initial_balance else None,
            "peak_balance": float(self.peak_balance) if self.peak_balance else None,
            "drawdown": (float(dd) if (dd := self.get_drawdown()) is not None else None),
            "pnl_percentage": (
                float(pnl) if (pnl := self.get_pnl_percentage()) is not None else None
            ),
            "daily_loss": float(self.daily_loss),
            "max_daily_loss": float(self.max_daily_loss) if self.max_daily_loss else None,
            "total_trades": self.total_trades,
            "rejected_trades": self.rejected_trades,
            "stop_loss_triggers": self.stop_loss_triggers,
            "max_position_size": float(self.max_position_size),
            "min_order_size": float(self.min_order_size),
            "max_position_size_per_trade": (
                float(self.max_position_size_per_trade)
                if self.max_position_size_per_trade is not None
                else None
            ),
        }

    def update_position_value(self, value: Decimal) -> None:
        """
        Update current position value.

        Args:
            value: Current total position value
        """
        self.current_position_value = value
        logger.debug("Position value updated", value=float(value))
