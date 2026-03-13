"""
RecoveryCoordinator — Manages DCA recovery when Grid hits its lower boundary.

State machine:
    IDLE → [price < grid_lower] → DCA_ACTIVE → [blended TP / timeout] → EXITING → IDLE

Instead of closing all Grid positions at STOP_LOSS, the coordinator:
1. Finds the nearest SMC support (bullish OB / swing low / fallback)
2. Launches a DCA cascade from current_price down to that support
3. Monitors blended TP (Grid underwater + DCA fills) at +1%
4. On exit: signals to close all, then restart Grid on a new range
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from bot.strategies.base import BaseSignal, SignalDirection
from bot.strategies.hybrid.recovery_config import RecoveryConfig
from bot.utils.logger import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from bot.core.smc.models import OrderBlock, SMCContext, SwingPoint

logger = get_logger(__name__)


# =============================================================================
# Data Structures
# =============================================================================


class RecoveryPhase(str, Enum):
    IDLE = "idle"
    DCA_ACTIVE = "dca_active"
    EXITING = "exiting"


@dataclass
class UnderwaterPosition:
    """A position that is underwater (below entry) during recovery."""

    pos_id: str
    entry_price: Decimal
    size: Decimal


@dataclass
class RecoveryState:
    """Full state of an active recovery session."""

    phase: RecoveryPhase
    entered_bar: int
    grid_positions: list[UnderwaterPosition]
    smc_support: Decimal
    dca_levels: list[Decimal]  # planned DCA price levels
    dca_fills: list[UnderwaterPosition] = field(default_factory=list)
    blended_avg_entry: Decimal = Decimal("0")
    blended_total_size: Decimal = Decimal("0")
    tp_target: Decimal = Decimal("0")
    bars_elapsed: int = 0
    next_dca_index: int = 0  # index into dca_levels for next pending order
    base_order_size: Decimal = Decimal("0")  # size of first DCA order

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "entered_bar": self.entered_bar,
            "grid_positions": len(self.grid_positions),
            "smc_support": float(self.smc_support),
            "dca_levels": [float(l) for l in self.dca_levels],
            "dca_fills": len(self.dca_fills),
            "blended_avg_entry": float(self.blended_avg_entry),
            "blended_total_size": float(self.blended_total_size),
            "tp_target": float(self.tp_target),
            "bars_elapsed": self.bars_elapsed,
            "next_dca_index": self.next_dca_index,
        }


@dataclass
class RecoveryAction:
    """Action to take this bar during recovery."""

    dca_signals: list[BaseSignal] = field(default_factory=list)
    should_close_all: bool = False
    close_reason: str = ""
    new_grid_range: tuple[Decimal, Decimal] | None = None


# =============================================================================
# RecoveryCoordinator
# =============================================================================


class RecoveryCoordinator:
    """Coordinates Grid→DCA recovery when price breaks below grid lower boundary."""

    def __init__(self, config: RecoveryConfig) -> None:
        self._config = config
        self._state: RecoveryState | None = None
        self._cooldown_remaining: int = 0
        self._total_recoveries: int = 0
        self._successful_recoveries: int = 0

    @property
    def config(self) -> RecoveryConfig:
        return self._config

    @property
    def state(self) -> RecoveryState | None:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state is not None and self._state.phase == RecoveryPhase.DCA_ACTIVE

    @property
    def is_cooling_down(self) -> bool:
        return self._cooldown_remaining > 0

    # =========================================================================
    # Trigger Check
    # =========================================================================

    def should_trigger(self, grid_lower: Decimal, current_price: Decimal) -> bool:
        """Check if recovery should be triggered (price below grid lower)."""
        if not self._config.enabled:
            return False
        if self._state is not None:
            return False  # already in recovery
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return False
        return current_price < grid_lower and grid_lower > 0

    # =========================================================================
    # Enter Recovery
    # =========================================================================

    def enter_recovery(
        self,
        grid_positions: list[UnderwaterPosition],
        current_price: Decimal,
        current_bar: int,
        smc_support: Decimal | None = None,
        base_order_size: Decimal = Decimal("150"),
    ) -> RecoveryState:
        """
        Initialize a recovery session.

        Args:
            grid_positions: Currently open Grid positions (underwater).
            current_price: Price that triggered recovery.
            current_bar: Current bar index.
            smc_support: Nearest SMC support level, or None for fallback.
            base_order_size: Quote-currency size for the first DCA order.

        Returns:
            New RecoveryState.
        """
        # Determine support level
        if smc_support is None or smc_support >= current_price:
            smc_support = current_price * (Decimal("1") - self._config.fallback_support_pct)

        # Build DCA cascade levels from current_price down to smc_support
        dca_levels = self._build_dca_levels(current_price, smc_support)

        # Compute initial blended entry from Grid positions
        blended_avg, total_size = self._compute_blended(grid_positions, [])

        state = RecoveryState(
            phase=RecoveryPhase.DCA_ACTIVE,
            entered_bar=current_bar,
            grid_positions=grid_positions,
            smc_support=smc_support,
            dca_levels=dca_levels,
            blended_avg_entry=blended_avg,
            blended_total_size=total_size,
            tp_target=blended_avg * (Decimal("1") + self._config.tp_target_pct)
            if blended_avg > 0
            else current_price * (Decimal("1") + self._config.tp_target_pct),
            base_order_size=base_order_size,
        )

        self._state = state
        self._total_recoveries += 1

        logger.info(
            "recovery_entered",
            current_price=float(current_price),
            smc_support=float(smc_support),
            grid_positions=len(grid_positions),
            dca_levels=len(dca_levels),
            blended_avg=float(blended_avg),
            tp_target=float(state.tp_target),
        )

        return state

    # =========================================================================
    # Per-Bar Update
    # =========================================================================

    def on_price_update(
        self,
        current_price: Decimal,
        new_fills: list[UnderwaterPosition] | None = None,
    ) -> RecoveryAction:
        """
        Called every bar while recovery is active.

        Args:
            current_price: Latest price.
            new_fills: Any DCA orders that filled since last bar.

        Returns:
            RecoveryAction with DCA signals to place, or close instructions.
        """
        if self._state is None or self._state.phase != RecoveryPhase.DCA_ACTIVE:
            return RecoveryAction()

        state = self._state
        state.bars_elapsed += 1

        # Register new DCA fills
        if new_fills:
            state.dca_fills.extend(new_fills)
            state.blended_avg_entry, state.blended_total_size = self._compute_blended(
                state.grid_positions, state.dca_fills
            )
            state.tp_target = state.blended_avg_entry * (
                Decimal("1") + self._config.tp_target_pct
            )

        action = RecoveryAction()

        # Check TP hit
        if state.blended_total_size > 0 and current_price >= state.tp_target:
            action.should_close_all = True
            action.close_reason = (
                f"recovery_tp_hit: blended_avg={state.blended_avg_entry:.2f}, "
                f"tp={state.tp_target:.2f}, price={current_price:.2f}"
            )
            action.new_grid_range = self._compute_new_grid_range(current_price)
            self._finalize_recovery(success=True)
            return action

        # Check timeout
        if state.bars_elapsed >= self._config.timeout_bars:
            if self._config.timeout_action == "close_all":
                action.should_close_all = True
                action.close_reason = (
                    f"recovery_timeout: {state.bars_elapsed} bars elapsed"
                )
                action.new_grid_range = self._compute_new_grid_range(current_price)
                self._finalize_recovery(success=False)
                return action
            # timeout_action == "hold": keep waiting, don't generate new DCA orders
            return action

        # Generate pending DCA signals
        action.dca_signals = self._generate_dca_signals(state, current_price)

        return action

    # =========================================================================
    # SMC Support Discovery
    # =========================================================================

    @staticmethod
    def find_smc_support(
        market_structure: SMCContext | None,
        current_price: Decimal,
    ) -> Decimal | None:
        """
        Find nearest SMC support level below current_price.

        Looks at:
        1. Bullish Order Blocks (demand zones) below price
        2. Swing lows below price
        Returns the closest one, or None if nothing found.
        """
        if market_structure is None:
            return None

        candidates: list[tuple[Decimal, float]] = []  # (price, score)

        # 1. Bullish OBs below current price
        for ob in market_structure.order_blocks:
            if ob.ob_type.value == "bull" and not ob.invalidated:
                ob_top = Decimal(str(ob.high))
                if ob_top < current_price:
                    # Score: closer to price = higher priority
                    distance = float(current_price - ob_top)
                    score = 1.0 / (distance + 0.01)
                    candidates.append((ob_top, score))

        # 2. Swing lows below current price
        for sw in market_structure.swing_lows:
            sw_price = Decimal(str(sw.price))
            if sw_price < current_price:
                distance = float(current_price - sw_price)
                score = 0.8 / (distance + 0.01)  # slightly lower priority than OB
                candidates.append((sw_price, score))

        if not candidates:
            return None

        # Return the candidate with highest score (closest + best type)
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_statistics(self) -> dict:
        return {
            "total_recoveries": self._total_recoveries,
            "successful_recoveries": self._successful_recoveries,
            "success_rate": (
                self._successful_recoveries / self._total_recoveries
                if self._total_recoveries > 0
                else 0.0
            ),
            "cooldown_remaining": self._cooldown_remaining,
            "is_active": self.is_active,
        }

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _build_dca_levels(
        self, current_price: Decimal, support: Decimal
    ) -> list[Decimal]:
        """Build DCA cascade price levels from current_price down to support."""
        levels: list[Decimal] = []
        step = self._config.dca_step_pct
        price = current_price * (Decimal("1") - step)

        for _ in range(self._config.max_dca_orders):
            if price < support:
                # Don't go below support, place last level at support
                if not levels or levels[-1] != support:
                    levels.append(support)
                break
            levels.append(price)
            price = price * (Decimal("1") - step)

        return levels

    @staticmethod
    def _compute_blended(
        grid_positions: list[UnderwaterPosition],
        dca_fills: list[UnderwaterPosition],
    ) -> tuple[Decimal, Decimal]:
        """Compute blended average entry and total size."""
        all_positions = grid_positions + dca_fills
        if not all_positions:
            return Decimal("0"), Decimal("0")

        total_cost = sum(p.entry_price * p.size for p in all_positions)
        total_size = sum(p.size for p in all_positions)

        if total_size == 0:
            return Decimal("0"), Decimal("0")

        return total_cost / total_size, total_size

    def _generate_dca_signals(
        self, state: RecoveryState, current_price: Decimal
    ) -> list[BaseSignal]:
        """Generate DCA buy signals for levels that current_price has reached."""
        signals: list[BaseSignal] = []

        while state.next_dca_index < len(state.dca_levels):
            level = state.dca_levels[state.next_dca_index]
            if current_price <= level:
                # Calculate order size with martingale multiplier
                order_num = state.next_dca_index
                multiplier = self._config.dca_volume_multiplier ** order_num
                size = state.base_order_size * multiplier

                from datetime import datetime, timezone

                signal = BaseSignal(
                    direction=SignalDirection.LONG,
                    entry_price=current_price,
                    stop_loss=state.smc_support * Decimal("0.98"),  # 2% below support
                    take_profit=state.tp_target,
                    confidence=0.7,
                    timestamp=datetime.now(timezone.utc),
                    strategy_type="recovery_dca",
                    signal_reason=f"recovery_dca_level_{order_num}",
                    risk_reward_ratio=1.0,
                    metadata={
                        "recovery": True,
                        "dca_level": float(level),
                        "dca_order_num": order_num,
                        "multiplier": float(multiplier),
                    },
                )
                signals.append(signal)
                state.next_dca_index += 1
            else:
                break

        return signals

    def _compute_new_grid_range(
        self, current_price: Decimal
    ) -> tuple[Decimal, Decimal]:
        """Compute new grid range centered on current price after recovery."""
        half_range = current_price * Decimal("0.05")  # 5% range
        return (current_price - half_range, current_price + half_range)

    def _finalize_recovery(self, success: bool) -> None:
        """Clean up after recovery ends."""
        if success:
            self._successful_recoveries += 1

        if self._state:
            logger.info(
                "recovery_finalized",
                success=success,
                bars_elapsed=self._state.bars_elapsed,
                dca_fills=len(self._state.dca_fills),
                blended_avg=float(self._state.blended_avg_entry),
            )

        self._state = None
        self._cooldown_remaining = self._config.cooldown_after_recovery_bars
