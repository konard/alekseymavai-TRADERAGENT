"""
Configuration validator for TRADERAGENT production deployment.

Validates all strategy configurations, risk parameters, and deployment
settings before going live.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ValidationResult:
    """Result of a configuration validation check."""

    check_name: str
    passed: bool
    message: str
    category: str = "general"  # "risk", "strategy", "exchange", "general"


@dataclass
class ConfigValidationReport:
    """Complete configuration validation report."""

    results: list[ValidationResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[ValidationResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> dict[str, Any]:
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        categories = {}
        for r in self.results:
            if r.category not in categories:
                categories[r.category] = {"passed": 0, "failed": 0}
            if r.passed:
                categories[r.category]["passed"] += 1
            else:
                categories[r.category]["failed"] += 1
        return {
            "total_checks": total,
            "passed": passed,
            "failed": total - passed,
            "categories": categories,
            "overall_status": "PASS" if self.passed else "FAIL",
        }


class ConfigValidator:
    """Validate trading configurations for production readiness."""

    # Safe limits for production
    MAX_RISK_PER_TRADE = Decimal("0.05")  # 5%
    MAX_TOTAL_EXPOSURE = Decimal("0.50")  # 50%
    MAX_DAILY_LOSS_PCT = Decimal("0.10")  # 10%
    MIN_RISK_REWARD = Decimal("1.5")
    MAX_POSITION_SIZE_USD = Decimal("50000")
    MAX_GRID_LEVELS = 50
    MAX_SAFETY_ORDERS = 10

    def validate_risk_params(
        self,
        risk_per_trade: Decimal = Decimal("0.02"),
        max_exposure: Decimal = Decimal("0.20"),
        max_daily_loss: Decimal = Decimal("0.10"),
        min_risk_reward: Decimal = Decimal("2.5"),
    ) -> list[ValidationResult]:
        """Validate risk management parameters."""
        results = []

        if risk_per_trade > self.MAX_RISK_PER_TRADE:
            results.append(
                ValidationResult(
                    check_name="risk_per_trade",
                    passed=False,
                    message=f"Risk per trade {risk_per_trade} exceeds max {self.MAX_RISK_PER_TRADE}",
                    category="risk",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check_name="risk_per_trade",
                    passed=True,
                    message=f"Risk per trade {risk_per_trade} within limits",
                    category="risk",
                )
            )

        if max_exposure > self.MAX_TOTAL_EXPOSURE:
            results.append(
                ValidationResult(
                    check_name="max_exposure",
                    passed=False,
                    message=f"Max exposure {max_exposure} exceeds limit {self.MAX_TOTAL_EXPOSURE}",
                    category="risk",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check_name="max_exposure",
                    passed=True,
                    message=f"Max exposure {max_exposure} within limits",
                    category="risk",
                )
            )

        if max_daily_loss > self.MAX_DAILY_LOSS_PCT:
            results.append(
                ValidationResult(
                    check_name="max_daily_loss",
                    passed=False,
                    message=f"Max daily loss {max_daily_loss} exceeds limit {self.MAX_DAILY_LOSS_PCT}",
                    category="risk",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check_name="max_daily_loss",
                    passed=True,
                    message=f"Max daily loss {max_daily_loss} within limits",
                    category="risk",
                )
            )

        if min_risk_reward < self.MIN_RISK_REWARD:
            results.append(
                ValidationResult(
                    check_name="min_risk_reward",
                    passed=False,
                    message=f"Min R:R {min_risk_reward} below minimum {self.MIN_RISK_REWARD}",
                    category="risk",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check_name="min_risk_reward",
                    passed=True,
                    message=f"Min R:R {min_risk_reward} meets minimum",
                    category="risk",
                )
            )

        return results

    def validate_grid_config(
        self,
        num_levels: int = 10,
        amount_per_grid: Decimal = Decimal("100"),
        grid_range_pct: Decimal = Decimal("0.05"),
    ) -> list[ValidationResult]:
        """Validate grid strategy configuration."""
        results = []

        if num_levels > self.MAX_GRID_LEVELS:
            results.append(
                ValidationResult(
                    check_name="grid_levels",
                    passed=False,
                    message=f"Grid levels {num_levels} exceeds max {self.MAX_GRID_LEVELS}",
                    category="strategy",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check_name="grid_levels",
                    passed=True,
                    message=f"Grid levels {num_levels} within limits",
                    category="strategy",
                )
            )

        total_investment = amount_per_grid * num_levels
        if total_investment > self.MAX_POSITION_SIZE_USD:
            results.append(
                ValidationResult(
                    check_name="grid_total_investment",
                    passed=False,
                    message=f"Total grid investment ${total_investment} exceeds max ${self.MAX_POSITION_SIZE_USD}",
                    category="strategy",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check_name="grid_total_investment",
                    passed=True,
                    message=f"Total grid investment ${total_investment} within limits",
                    category="strategy",
                )
            )

        if grid_range_pct > Decimal("0.20"):
            results.append(
                ValidationResult(
                    check_name="grid_range",
                    passed=False,
                    message=f"Grid range {grid_range_pct} too wide (>20%)",
                    category="strategy",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check_name="grid_range",
                    passed=True,
                    message=f"Grid range {grid_range_pct} within acceptable bounds",
                    category="strategy",
                )
            )

        return results

    def validate_dca_config(
        self,
        base_order_size: Decimal = Decimal("100"),
        safety_order_size: Decimal = Decimal("200"),
        max_safety_orders: int = 5,
        take_profit_pct: Decimal = Decimal("0.015"),
    ) -> list[ValidationResult]:
        """Validate DCA strategy configuration."""
        results = []

        if max_safety_orders > self.MAX_SAFETY_ORDERS:
            results.append(
                ValidationResult(
                    check_name="dca_safety_orders",
                    passed=False,
                    message=f"Max safety orders {max_safety_orders} exceeds limit {self.MAX_SAFETY_ORDERS}",
                    category="strategy",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check_name="dca_safety_orders",
                    passed=True,
                    message=f"Max safety orders {max_safety_orders} within limits",
                    category="strategy",
                )
            )

        max_capital = base_order_size + (safety_order_size * max_safety_orders)
        if max_capital > self.MAX_POSITION_SIZE_USD:
            results.append(
                ValidationResult(
                    check_name="dca_max_capital",
                    passed=False,
                    message=f"Max DCA capital ${max_capital} exceeds limit",
                    category="strategy",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check_name="dca_max_capital",
                    passed=True,
                    message=f"Max DCA capital ${max_capital} within limits",
                    category="strategy",
                )
            )

        if take_profit_pct < Decimal("0.005"):
            results.append(
                ValidationResult(
                    check_name="dca_take_profit",
                    passed=False,
                    message=f"Take profit {take_profit_pct} too low (<0.5%)",
                    category="strategy",
                )
            )
        else:
            results.append(
                ValidationResult(
                    check_name="dca_take_profit",
                    passed=True,
                    message=f"Take profit {take_profit_pct} acceptable",
                    category="strategy",
                )
            )

        return results

    def run_full_validation(self, **kwargs: Any) -> ConfigValidationReport:
        """Run all validation checks."""
        report = ConfigValidationReport()
        report.results.extend(self.validate_risk_params(**kwargs.get("risk", {})))
        report.results.extend(self.validate_grid_config(**kwargs.get("grid", {})))
        report.results.extend(self.validate_dca_config(**kwargs.get("dca", {})))
        return report
