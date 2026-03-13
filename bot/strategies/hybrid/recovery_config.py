"""
RecoveryConfig — Configuration for AdaptiveRecoveryGrid.

When Grid hits its lower boundary, instead of closing all positions at a loss,
the system launches a DCA cascade down to the nearest SMC support level,
monitors blended TP (+1%), then restarts Grid on a new range.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class RecoveryConfig:
    """Configuration for the recovery coordinator."""

    enabled: bool = False  # disabled by default — opt-in

    # Take-profit on blended average entry (Grid underwater + DCA fills)
    tp_target_pct: Decimal = Decimal("0.01")  # +1%

    # DCA cascade parameters
    max_dca_orders: int = 5
    dca_step_pct: Decimal = Decimal("0.015")  # 1.5% between levels
    dca_volume_multiplier: Decimal = Decimal("1.3")  # martingale factor per level

    # Timeout
    timeout_bars: int = 12  # 12 × 5 min = 1 hour
    timeout_action: str = "close_all"  # "close_all" | "hold"

    # SMC fallback: if no bullish OB / swing low found
    fallback_support_pct: Decimal = Decimal("0.05")  # 5% below current price

    # Risk limits
    max_recovery_capital_pct: Decimal = Decimal("0.30")  # max 30% of balance
    cooldown_after_recovery_bars: int = 6  # 30 min before Grid restart

    def validate(self) -> None:
        """Validate configuration values."""
        if self.tp_target_pct <= 0:
            raise ValueError("tp_target_pct must be positive")
        if self.max_dca_orders < 1:
            raise ValueError("max_dca_orders must be >= 1")
        if self.dca_step_pct <= 0:
            raise ValueError("dca_step_pct must be positive")
        if self.dca_volume_multiplier < Decimal("1"):
            raise ValueError("dca_volume_multiplier must be >= 1.0")
        if self.timeout_bars < 1:
            raise ValueError("timeout_bars must be >= 1")
        if self.timeout_action not in ("close_all", "hold"):
            raise ValueError("timeout_action must be 'close_all' or 'hold'")
        if self.max_recovery_capital_pct <= 0 or self.max_recovery_capital_pct > 1:
            raise ValueError("max_recovery_capital_pct must be in (0, 1]")
