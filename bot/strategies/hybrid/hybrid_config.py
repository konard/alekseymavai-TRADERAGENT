"""
HybridConfig — Configuration for hybrid Grid+DCA strategy mode.

Defines operating modes and parameters for Grid↔DCA transitions:
- Capital allocation between Grid and DCA
- Breakout detection thresholds (ADX, ATR ratio)
- Return-to-grid conditions
- Transition cooldowns and minimum durations
"""

from dataclasses import dataclass, field
from enum import Enum


class HybridMode(str, Enum):
    """Operating mode of the hybrid strategy."""

    GRID_ONLY = "grid_only"  # Grid active, DCA standby
    TRANSITIONING = "transitioning"  # Mid-transition between modes
    DCA_ACTIVE = "dca_active"  # DCA active after grid breakout
    BOTH_ACTIVE = "both_active"  # Both active (intermediate)
    RECOVERY_ACTIVE = "recovery_active"  # Grid paused, DCA recovery cascade


from bot.strategies.hybrid.recovery_config import RecoveryConfig  # noqa: E402


@dataclass
class HybridConfig:
    """
    Configuration for hybrid Grid+DCA strategy.

    Capital allocation (must sum to 1.0):
        grid_capital_pct + dca_capital_pct + reserve_pct = 1.0

    Grid→DCA transition triggers when:
        - GridRiskManager.check_trend_suitability() → DEACTIVATE
        - ADX >= breakout_adx_threshold
        - Time in grid mode >= min_grid_duration_seconds
        - Cooldown since last transition elapsed

    DCA→Grid return triggers when:
        - ADX < return_adx_threshold
        - Regime detector shows SIDEWAYS
        - Time in DCA mode >= min_dca_duration_seconds
        - All DCA deals closed (if require_dca_deals_closed)
    """

    # Capital allocation
    grid_capital_pct: float = 0.6  # 60% for Grid
    dca_capital_pct: float = 0.3  # 30% for DCA
    reserve_pct: float = 0.1  # 10% reserve

    # Grid→DCA transition thresholds
    breakout_adx_threshold: float = 25.0  # ADX confirming trend
    breakout_atr_multiplier: float = 2.0  # Price move > N*ATR
    min_grid_duration_seconds: int = 300  # 5 min minimum in grid

    # DCA→Grid return thresholds
    return_adx_threshold: float = 20.0  # ADX for return to grid
    min_dca_duration_seconds: int = 600  # 10 min minimum in DCA
    require_dca_deals_closed: bool = True  # Wait for DCA deals to close

    # Transition protection
    transition_cooldown_seconds: int = 120  # 2 min between transitions
    max_transition_history: int = 50  # Max history records

    # Recovery (DCA cascade when Grid hits lower boundary)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)

    def validate(self) -> None:
        """Validate configuration values."""
        total = self.grid_capital_pct + self.dca_capital_pct + self.reserve_pct
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Capital allocation must sum to 1.0, got {total:.3f}")
        if self.grid_capital_pct < 0 or self.grid_capital_pct > 1:
            raise ValueError("grid_capital_pct must be between 0 and 1")
        if self.dca_capital_pct < 0 or self.dca_capital_pct > 1:
            raise ValueError("dca_capital_pct must be between 0 and 1")
        if self.reserve_pct < 0 or self.reserve_pct > 1:
            raise ValueError("reserve_pct must be between 0 and 1")
        if self.breakout_adx_threshold <= 0:
            raise ValueError("breakout_adx_threshold must be positive")
        if self.breakout_atr_multiplier <= 0:
            raise ValueError("breakout_atr_multiplier must be positive")
        if self.return_adx_threshold <= 0:
            raise ValueError("return_adx_threshold must be positive")
        if self.transition_cooldown_seconds < 0:
            raise ValueError("transition_cooldown_seconds must be non-negative")
