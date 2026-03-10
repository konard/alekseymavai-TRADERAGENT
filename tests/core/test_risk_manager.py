"""Tests for RiskManager"""

from decimal import Decimal

import pytest

from bot.core.risk_manager import RiskCheckResult, RiskManager


class TestRiskCheckResult:
    """Test RiskCheckResult functionality"""

    def test_init_allowed(self):
        """Test initialization with allowed=True"""
        result = RiskCheckResult(allowed=True)

        assert result.allowed is True
        assert result.reason is None
        assert bool(result) is True

    def test_init_not_allowed(self):
        """Test initialization with allowed=False"""
        result = RiskCheckResult(allowed=False, reason="Test reason")

        assert result.allowed is False
        assert result.reason == "Test reason"
        assert bool(result) is False

    def test_repr(self):
        """Test string representation"""
        result_ok = RiskCheckResult(allowed=True)
        result_fail = RiskCheckResult(allowed=False, reason="Test")

        assert "allowed=True" in repr(result_ok)
        assert "allowed=False" in repr(result_fail)
        assert "Test" in repr(result_fail)


class TestRiskManager:
    """Test RiskManager functionality"""

    def test_init_valid_params(self):
        """Test initialization with valid parameters"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            stop_loss_percentage=Decimal("0.15"),
            max_daily_loss=Decimal("500"),
        )

        assert manager.max_position_size == Decimal("10000")
        assert manager.min_order_size == Decimal("10")
        assert manager.stop_loss_percentage == Decimal("0.15")
        assert manager.max_daily_loss == Decimal("500")
        assert manager.is_halted is False

    def test_init_invalid_position_size(self):
        """Test initialization with invalid position size"""
        with pytest.raises(ValueError, match="max_position_size must be positive"):
            RiskManager(
                max_position_size=Decimal("-10000"),
                min_order_size=Decimal("10"),
            )

    def test_init_invalid_min_order_size(self):
        """Test initialization with invalid min order size"""
        with pytest.raises(ValueError, match="min_order_size must be positive"):
            RiskManager(
                max_position_size=Decimal("10000"),
                min_order_size=Decimal("-10"),
            )

    def test_init_invalid_stop_loss(self):
        """Test initialization with invalid stop loss percentage"""
        with pytest.raises(ValueError, match="stop_loss_percentage must be"):
            RiskManager(
                max_position_size=Decimal("10000"),
                min_order_size=Decimal("10"),
                stop_loss_percentage=Decimal("1.5"),
            )

    def test_initialize_balance(self):
        """Test balance initialization"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        manager.initialize_balance(Decimal("5000"))

        assert manager.initial_balance == Decimal("5000")
        assert manager.current_balance == Decimal("5000")
        assert manager.peak_balance == Decimal("5000")

    def test_update_balance(self):
        """Test balance update"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        manager.initialize_balance(Decimal("5000"))
        manager.update_balance(Decimal("5200"))

        assert manager.current_balance == Decimal("5200")
        assert manager.peak_balance == Decimal("5200")

    def test_update_balance_with_loss(self):
        """Test balance update with loss"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        manager.initialize_balance(Decimal("5000"))
        manager.update_balance(Decimal("4800"))

        assert manager.current_balance == Decimal("4800")
        assert manager.daily_loss == Decimal("200")
        assert manager.peak_balance == Decimal("5000")

    def test_check_order_size_valid(self):
        """Test order size check with valid order"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        result = manager.check_order_size(Decimal("100"))

        assert result.allowed is True

    def test_check_order_size_too_small(self):
        """Test order size check with order too small"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        result = manager.check_order_size(Decimal("5"))

        assert result.allowed is False
        assert "below minimum" in result.reason
        assert manager.rejected_trades == 1

    def test_check_position_limit_valid(self):
        """Test position limit check with valid addition"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        result = manager.check_position_limit(Decimal("8000"), Decimal("1000"))

        assert result.allowed is True

    def test_check_position_limit_exceeded(self):
        """Test position limit check when limit exceeded"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        result = manager.check_position_limit(Decimal("9500"), Decimal("1000"))

        assert result.allowed is False
        assert "exceed max" in result.reason
        assert manager.rejected_trades == 1

    def test_check_available_balance_sufficient(self):
        """Test available balance check with sufficient funds"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        result = manager.check_available_balance(Decimal("100"), Decimal("500"))

        assert result.allowed is True

    def test_check_available_balance_insufficient(self):
        """Test available balance check with insufficient funds"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        result = manager.check_available_balance(Decimal("600"), Decimal("500"))

        assert result.allowed is False
        assert "Insufficient balance" in result.reason
        assert manager.rejected_trades == 1

    def test_check_trade_all_valid(self):
        """Test comprehensive trade check with all checks passing"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        result = manager.check_trade(
            order_value=Decimal("100"),
            current_position=Decimal("5000"),
            available_balance=Decimal("500"),
        )

        assert result.allowed is True
        assert manager.total_trades == 1

    def test_check_trade_fails_order_size(self):
        """Test comprehensive trade check failing on order size"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        result = manager.check_trade(
            order_value=Decimal("5"),
            current_position=Decimal("5000"),
            available_balance=Decimal("500"),
        )

        assert result.allowed is False
        assert manager.total_trades == 0

    def test_check_trade_fails_position_limit(self):
        """Test comprehensive trade check failing on position limit"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        result = manager.check_trade(
            order_value=Decimal("2000"),
            current_position=Decimal("9000"),
            available_balance=Decimal("5000"),
        )

        assert result.allowed is False
        assert manager.total_trades == 0

    def test_portfolio_stop_loss_trigger(self):
        """Test portfolio stop-loss triggering"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            stop_loss_percentage=Decimal("0.15"),
        )

        manager.initialize_balance(Decimal("10000"))

        # Lose 15%
        manager.update_balance(Decimal("8500"))

        assert manager.is_halted is True
        assert "stop-loss" in manager.halt_reason.lower()
        assert manager.stop_loss_triggers == 1

    def test_portfolio_stop_loss_not_triggered(self):
        """Test portfolio stop-loss not triggering with smaller loss"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            stop_loss_percentage=Decimal("0.15"),
        )

        manager.initialize_balance(Decimal("10000"))

        # Lose 10% (less than 15%)
        manager.update_balance(Decimal("9000"))

        assert manager.is_halted is False

    def test_daily_loss_limit_trigger(self):
        """Test daily loss limit triggering"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            max_daily_loss=Decimal("500"),
        )

        manager.initialize_balance(Decimal("10000"))

        # Accumulate 500 in losses
        manager.update_balance(Decimal("9700"))
        manager.update_balance(Decimal("9500"))

        assert manager.is_halted is True
        assert "daily loss limit" in manager.halt_reason.lower()

    def test_daily_loss_limit_not_triggered(self):
        """Test daily loss limit not triggering with smaller loss"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            max_daily_loss=Decimal("500"),
        )

        manager.initialize_balance(Decimal("10000"))

        # Lose 300 (less than 500)
        manager.update_balance(Decimal("9700"))

        assert manager.is_halted is False

    def test_get_drawdown(self):
        """Test drawdown calculation"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        manager.initialize_balance(Decimal("10000"))
        manager.update_balance(Decimal("11000"))  # New peak
        manager.update_balance(Decimal("10500"))  # Drawdown

        drawdown = manager.get_drawdown()

        # Drawdown = (11000 - 10500) / 11000 = 0.045...
        assert drawdown is not None
        assert abs(drawdown - Decimal("0.0454545")) < Decimal("0.0001")

    def test_get_drawdown_no_data(self):
        """Test drawdown calculation with no data"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        drawdown = manager.get_drawdown()

        assert drawdown is None

    def test_get_pnl_percentage(self):
        """Test PnL percentage calculation"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        manager.initialize_balance(Decimal("10000"))
        manager.update_balance(Decimal("11000"))

        pnl_pct = manager.get_pnl_percentage()

        # PnL = (11000 - 10000) / 10000 = 0.1 (10%)
        assert pnl_pct == Decimal("0.1")

    def test_get_pnl_percentage_loss(self):
        """Test PnL percentage calculation with loss"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        manager.initialize_balance(Decimal("10000"))
        manager.update_balance(Decimal("9000"))

        pnl_pct = manager.get_pnl_percentage()

        # PnL = (9000 - 10000) / 10000 = -0.1 (-10%)
        assert pnl_pct == Decimal("-0.1")

    def test_reset_daily_loss(self):
        """Test resetting daily loss counter"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        manager.initialize_balance(Decimal("10000"))
        manager.update_balance(Decimal("9700"))

        assert manager.daily_loss > 0

        manager.reset_daily_loss()

        assert manager.daily_loss == Decimal("0")

    def test_resume_from_halt(self):
        """Test resuming trading after halt"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            stop_loss_percentage=Decimal("0.15"),
        )

        manager.initialize_balance(Decimal("10000"))
        manager.update_balance(Decimal("8500"))  # Trigger halt

        assert manager.is_halted is True

        manager.resume()

        assert manager.is_halted is False
        assert manager.halt_reason is None

    def test_resume_when_not_halted(self):
        """Test resuming when not halted does nothing"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        manager.resume()

        assert manager.is_halted is False

    def test_halted_system_rejects_trades(self):
        """Test that halted system rejects all trades"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            stop_loss_percentage=Decimal("0.15"),
        )

        manager.initialize_balance(Decimal("10000"))
        manager.update_balance(Decimal("8500"))  # Trigger halt

        result = manager.check_trade(
            order_value=Decimal("100"),
            current_position=Decimal("0"),
            available_balance=Decimal("1000"),
        )

        assert result.allowed is False
        # Reason should mention either "halted" or "stop-loss"
        assert "halted" in result.reason.lower() or "stop-loss" in result.reason.lower()

    def test_get_risk_status(self):
        """Test getting risk status"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            stop_loss_percentage=Decimal("0.15"),
            max_daily_loss=Decimal("500"),
        )

        manager.initialize_balance(Decimal("10000"))
        manager.update_balance(Decimal("10500"))

        # Execute some trades
        manager.check_trade(Decimal("100"), Decimal("0"), Decimal("500"))
        manager.check_order_size(Decimal("5"))  # This will be rejected

        status = manager.get_risk_status()

        assert status["is_halted"] is False
        assert status["current_balance"] == 10500.0
        assert status["initial_balance"] == 10000.0
        assert status["total_trades"] == 1
        assert status["rejected_trades"] == 1
        assert status["max_position_size"] == 10000.0
        assert status["min_order_size"] == 10.0

    def test_update_position_value(self):
        """Test updating position value"""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
        )

        manager.update_position_value(Decimal("5000"))

        assert manager.current_position_value == Decimal("5000")

    # ------------------------------------------------------------------
    # Net daily loss tests (P0.1 fix)
    # ------------------------------------------------------------------

    def test_daily_loss_is_net_not_cumulative(self):
        """Dip-and-recover must NOT accumulate losses cumulatively.

        Old bug: 10000 → 9700 → 10000 → 9700 gave daily_loss=600 (cumulative).
        Fix:     10000 → 9700 → 10000 → 9700 gives daily_loss=300 (net from peak).
        """
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            max_daily_loss=Decimal("500"),
        )
        manager.initialize_balance(Decimal("10000"))

        manager.update_balance(Decimal("9700"))  # dip 300
        assert manager.daily_loss == Decimal("300")

        manager.update_balance(Decimal("10000"))  # recover
        assert manager.daily_loss == Decimal("0")

        manager.update_balance(Decimal("9700"))  # dip again
        assert manager.daily_loss == Decimal("300")
        # Old cumulative would give 600 here and halt.
        assert manager.is_halted is False

    def test_net_daily_loss_still_halts_on_real_drawdown(self):
        """A genuine intraday drawdown must still trigger the daily loss halt."""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            max_daily_loss=Decimal("500"),
        )
        manager.initialize_balance(Decimal("10000"))

        manager.update_balance(Decimal("10200"))  # peak today = 10200
        manager.update_balance(Decimal("9600"))  # net loss = 10200 - 9600 = 600 > 500
        assert manager.daily_loss == Decimal("600")
        assert manager.is_halted is True

    def test_daily_loss_peak_resets_on_new_day(self):
        """After reset_daily_loss(), the daily peak restarts from current balance."""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            max_daily_loss=Decimal("500"),
        )
        manager.initialize_balance(Decimal("10000"))

        manager.update_balance(Decimal("9800"))  # loss 200
        assert manager.daily_loss == Decimal("200")

        manager.reset_daily_loss()
        assert manager.daily_loss == Decimal("0")
        # Peak today is now 9800 (current balance)

        manager.update_balance(Decimal("9600"))  # loss 200 from new peak
        assert manager.daily_loss == Decimal("200")
        assert manager.is_halted is False  # 200 < 500

    def test_daily_loss_monotone_decline_matches_net(self):
        """Monotone decline: net and cumulative give the same result."""
        manager = RiskManager(
            max_position_size=Decimal("10000"),
            min_order_size=Decimal("10"),
            max_daily_loss=Decimal("500"),
        )
        manager.initialize_balance(Decimal("10000"))

        manager.update_balance(Decimal("9700"))  # -300
        manager.update_balance(Decimal("9500"))  # -500 total from peak
        assert manager.daily_loss == Decimal("500")
        assert manager.is_halted is True
