"""
Unit tests for bot/core/portfolio_risk_manager.py

Tests cover:
- SharedCapitalPool: allocate/release/utilization
- PortfolioRiskManager: check_allocation, portfolio halt, correlation
"""

from decimal import Decimal

from bot.core.portfolio_risk_manager import (
    PortfolioRiskManager,
    RiskCheckStatus,
    SharedCapitalPool,
)

# ---------------------------------------------------------------------------
# SharedCapitalPool
# ---------------------------------------------------------------------------


class TestSharedCapitalPool:
    def _pool(self, total: float = 10000.0, max_util: float = 0.80) -> SharedCapitalPool:
        return SharedCapitalPool(
            total_capital=Decimal(str(total)),
            max_utilization_pct=max_util,
        )

    def test_allocate_success(self) -> None:
        pool = self._pool()
        assert pool.allocate("bot1", Decimal("1000")) is True
        assert pool.total_allocated == Decimal("1000")

    def test_allocate_respects_global_cap(self) -> None:
        pool = self._pool(total=1000.0, max_util=0.50)
        assert pool.allocate("bot1", Decimal("400")) is True
        # Second allocation would exceed 50%
        assert pool.allocate("bot2", Decimal("200")) is False

    def test_allocate_respects_individual_cap(self) -> None:
        pool = self._pool()
        pool.register_bot("bot1", max_limit=Decimal("500"))
        assert pool.allocate("bot1", Decimal("400")) is True
        assert pool.allocate("bot1", Decimal("200")) is False  # would exceed 500

    def test_release_returns_capital(self) -> None:
        pool = self._pool()
        pool.allocate("bot1", Decimal("1000"))
        pool.release("bot1", Decimal("500"))
        assert pool.total_allocated == Decimal("500")

    def test_release_clamps_at_zero(self) -> None:
        pool = self._pool()
        pool.release("nonexistent_bot", Decimal("999"))  # should not raise

    def test_get_utilization(self) -> None:
        pool = self._pool(total=10000.0)
        pool.allocate("bot1", Decimal("5000"))
        assert abs(pool.get_utilization() - 0.5) < 0.001

    def test_get_available(self) -> None:
        pool = self._pool(total=10000.0, max_util=0.80)
        pool.allocate("bot1", Decimal("2000"))
        expected = Decimal("10000") * Decimal("0.80") - Decimal("2000")
        assert pool.get_available() == expected

    def test_summary_structure(self) -> None:
        pool = self._pool()
        pool.allocate("bot1", Decimal("100"))
        summary = pool.get_summary()
        assert "total_capital" in summary
        assert "bots" in summary
        assert "bot1" in summary["bots"]

    def test_update_deployed(self) -> None:
        pool = self._pool()
        pool.allocate("bot1", Decimal("100"))
        pool.update_deployed("bot1", Decimal("80"))
        assert pool.total_deployed == Decimal("80")


# ---------------------------------------------------------------------------
# PortfolioRiskManager
# ---------------------------------------------------------------------------


class TestPortfolioRiskManager:
    def _prm(
        self,
        total: float = 10000.0,
        max_exposure: float = 0.80,
        max_pair: float = 0.25,
        stop_loss: float = 0.15,
    ) -> PortfolioRiskManager:
        return PortfolioRiskManager(
            total_capital=Decimal(str(total)),
            max_total_exposure_pct=max_exposure,
            max_single_pair_pct=max_pair,
            portfolio_stop_loss_pct=stop_loss,
        )

    def test_check_allocation_approved(self) -> None:
        prm = self._prm()
        result = prm.check_allocation("bot1", Decimal("1000"))
        assert result.approved is True
        assert result.status == RiskCheckStatus.APPROVED

    def test_check_allocation_exceeds_pair_limit(self) -> None:
        prm = self._prm(total=10000.0, max_pair=0.25)
        # 3000 > 25% of 10000 balance
        result = prm.check_allocation("bot1", Decimal("3000"), balance=Decimal("10000"))
        assert result.approved is False
        assert result.status == RiskCheckStatus.REJECTED_PAIR_LIMIT

    def test_check_allocation_exceeds_total_exposure(self) -> None:
        prm = self._prm(total=1000.0, max_exposure=0.5, max_pair=0.8)
        # First allocation fills pool
        prm.confirm_allocation("bot1", Decimal("500"))
        # Second allocation would exceed 50% pool cap
        result = prm.check_allocation("bot2", Decimal("100"))
        assert result.approved is False
        assert result.status == RiskCheckStatus.REJECTED_EXPOSURE

    def test_portfolio_halt_on_drawdown(self) -> None:
        prm = self._prm(total=10000.0, stop_loss=0.15)
        # Simulate 20% drawdown
        prm.update_all_balances({"bot1": Decimal("8000")})
        assert prm.is_portfolio_halted() is True

    def test_portfolio_halt_blocks_allocation(self) -> None:
        prm = self._prm(total=10000.0, stop_loss=0.15)
        prm.update_all_balances({"bot1": Decimal("8000")})  # triggers halt
        result = prm.check_allocation("bot2", Decimal("100"))
        assert result.approved is False
        assert result.status == RiskCheckStatus.REJECTED_PORTFOLIO_HALTED

    def test_portfolio_resume_clears_halt(self) -> None:
        prm = self._prm(total=10000.0, stop_loss=0.15)
        prm.update_all_balances({"bot1": Decimal("8000")})
        assert prm.is_portfolio_halted() is True
        prm.resume()
        assert prm.is_portfolio_halted() is False
        # After resume, allocation should succeed
        result = prm.check_allocation("bot2", Decimal("100"))
        assert result.approved is True

    def test_no_halt_on_small_drawdown(self) -> None:
        prm = self._prm(total=10000.0, stop_loss=0.15)
        prm.update_all_balances({"bot1": Decimal("9000")})  # 10% drawdown < 15%
        assert prm.is_portfolio_halted() is False

    def test_confirm_and_release(self) -> None:
        prm = self._prm()
        prm.confirm_allocation("bot1", Decimal("1000"), symbol="BTC/USDT")
        assert "BTC/USDT" in prm._active_symbols
        prm.release_allocation("bot1", Decimal("1000"), symbol="BTC/USDT")
        assert "BTC/USDT" not in prm._active_symbols

    def test_correlation_check_btc_eth(self) -> None:
        prm = self._prm(max_pair=0.50, max_exposure=0.90)
        prm.confirm_allocation("bot_btc", Decimal("1000"), symbol="BTC/USDT")
        # ETH is correlated with BTC — should be blocked
        result = prm.check_allocation("bot_eth", Decimal("500"), symbol="ETH/USDT")
        assert result.approved is False
        assert result.status == RiskCheckStatus.REJECTED_CORRELATION

    def test_correlation_override(self) -> None:
        prm = self._prm(max_pair=0.50, max_exposure=0.90)
        # Override correlation to be low
        prm.set_correlation("BTC/USDT", "ETH/USDT", 0.3)
        prm.confirm_allocation("bot_btc", Decimal("1000"), symbol="BTC/USDT")
        result = prm.check_allocation("bot_eth", Decimal("500"), symbol="ETH/USDT")
        assert result.approved is True

    def test_get_summary(self) -> None:
        prm = self._prm()
        summary = prm.get_summary()
        assert "total_capital" in summary
        assert "halted" in summary
        assert "pool" in summary

    def test_peak_updates_correctly(self) -> None:
        prm = self._prm(total=10000.0, stop_loss=0.15)
        # Balance grows — peak should update
        prm.update_all_balances({"bot1": Decimal("11000")})
        assert prm._peak_value == Decimal("11000")
        # Now drawdown from 11000 — 14% drawdown does not halt
        prm.update_all_balances({"bot1": Decimal("9500")})
        assert prm.is_portfolio_halted() is False
        # 16% drawdown from 11000 — should halt
        prm.update_all_balances({"bot1": Decimal("9240")})
        assert prm.is_portfolio_halted() is True
