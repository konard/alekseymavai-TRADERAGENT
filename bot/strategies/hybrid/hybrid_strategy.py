"""
HybridStrategy — Coordinates Grid+DCA in a single adaptive strategy.

Operates in two primary modes:
1. GRID_ONLY: Grid trading on sideways markets (default)
2. DCA_ACTIVE: DCA accumulation after grid breakout (trend detected)

Transitions:
- Grid→DCA: GridRiskManager detects trend (DEACTIVATE), ADX confirms
- DCA→Grid: ADX drops, regime returns to SIDEWAYS, DCA deals closed

Usage:
    hybrid = HybridStrategy(
        config=HybridConfig(),
        grid_risk_manager=GridRiskManager(),
        dca_engine=DCAEngine("BTC/USDT"),
        regime_detector=MarketRegimeDetectorV2(),
    )

    # On each price update:
    action = hybrid.evaluate(market_state, atr, price_move, adx)
    if action.transition_triggered:
        # Handle mode switch
    if action.dca_action and action.dca_action.should_open_deal:
        # Open DCA deal
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from bot.strategies.dca.dca_engine import DCAEngine, EngineAction
from bot.strategies.dca.dca_signal_generator import MarketState
from bot.strategies.grid.grid_risk_manager import (
    GridRiskAction,
    GridRiskManager,
    RiskCheckResult,
)
from bot.strategies.hybrid.hybrid_config import HybridConfig, HybridMode
from bot.strategies.hybrid.recovery_coordinator import (
    RecoveryAction,
    RecoveryCoordinator,
    RecoveryState,
)
from bot.strategies.hybrid.market_regime_detector import (
    MarketIndicators,
    MarketRegimeDetectorV2,
    RegimeResult,
    RegimeType,
)
from bot.utils.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class TransitionEvent:
    """Records a mode transition."""

    from_mode: HybridMode
    to_mode: HybridMode
    reason: str
    timestamp: datetime
    trigger_adx: float | None = None
    trigger_atr_ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_mode": self.from_mode.value,
            "to_mode": self.to_mode.value,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
            "trigger_adx": self.trigger_adx,
            "trigger_atr_ratio": self.trigger_atr_ratio,
        }


@dataclass
class HybridAction:
    """
    Result of a single hybrid evaluation cycle.

    Contains the current mode, any transition that occurred,
    and actions from the active sub-strategy.
    """

    mode: HybridMode
    transition_triggered: bool = False
    transition_event: TransitionEvent | None = None

    # Grid risk check result (when grid active)
    grid_risk_result: RiskCheckResult | None = None

    # DCA engine action (when DCA active)
    dca_action: EngineAction | None = None

    # Regime analysis (always present)
    regime_result: RegimeResult | None = None

    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mode": self.mode.value,
            "transition_triggered": self.transition_triggered,
            "warnings": self.warnings,
        }
        if self.transition_event:
            result["transition_event"] = self.transition_event.to_dict()
        if self.grid_risk_result:
            result["grid_risk"] = self.grid_risk_result.to_dict()
        if self.regime_result:
            result["regime"] = self.regime_result.to_dict()
        return result


# =============================================================================
# HybridStrategy
# =============================================================================


class HybridStrategy:
    """
    Coordinates Grid and DCA strategies based on market regime.

    Grid operates in sideways markets. When a breakout is detected
    (via GridRiskManager + ADX), the strategy transitions to DCA mode.
    When the market returns to sideways (low ADX + regime confirmation),
    it transitions back to Grid.
    """

    def __init__(
        self,
        config: HybridConfig | None = None,
        grid_risk_manager: GridRiskManager | None = None,
        dca_engine: DCAEngine | None = None,
        regime_detector: MarketRegimeDetectorV2 | None = None,
        recovery_coordinator: RecoveryCoordinator | None = None,
    ):
        self._config = config or HybridConfig()
        self._config.validate()

        self._grid_risk = grid_risk_manager or GridRiskManager()
        self._dca_engine = dca_engine
        self._regime_detector = regime_detector or MarketRegimeDetectorV2()
        self._recovery_coordinator = recovery_coordinator
        self._recovery_state: RecoveryState | None = None

        # State
        self._mode = HybridMode.GRID_ONLY
        self._mode_since = datetime.now(timezone.utc)
        self._last_transition: datetime | None = None
        self._transition_history: list[TransitionEvent] = []

        # Statistics
        self._total_transitions = 0
        self._grid_to_dca_count = 0
        self._dca_to_grid_count = 0
        self._recovery_count = 0

        logger.info(
            "HybridStrategy initialized",
            mode=self._mode.value,
            grid_capital_pct=self._config.grid_capital_pct,
            dca_capital_pct=self._config.dca_capital_pct,
            recovery_enabled=self._config.recovery.enabled,
        )

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def mode(self) -> HybridMode:
        """Current operating mode."""
        return self._mode

    @property
    def mode_since(self) -> datetime:
        """When current mode started."""
        return self._mode_since

    @property
    def transition_history(self) -> list[TransitionEvent]:
        """Copy of transition history (most recent first)."""
        return self._transition_history.copy()

    @property
    def config(self) -> HybridConfig:
        return self._config

    # =========================================================================
    # Main Evaluation
    # =========================================================================

    def evaluate(
        self,
        market_state: MarketState,
        atr: Decimal = Decimal("0"),
        price_move: Decimal = Decimal("0"),
        adx: float | None = None,
        regime_result: RegimeResult | None = None,
    ) -> HybridAction:
        """
        Evaluate market conditions and determine hybrid action.

        Called on each price update. Checks for mode transitions,
        then delegates to the active sub-strategy.

        Args:
            market_state: Current market conditions (for DCA engine).
            atr: Current ATR value (for grid risk check).
            price_move: Absolute price change over ATR period.
            adx: ADX value if available.
            regime_result: Pre-computed regime result (optional).

        Returns:
            HybridAction with mode, transitions, and sub-strategy actions.
        """
        action = HybridAction(mode=self._mode)

        # Compute regime if not provided
        if regime_result is None and adx is not None:
            indicators = MarketIndicators(
                current_price=market_state.current_price,
                adx=adx,
                ema_fast=market_state.ema_fast,
                ema_slow=market_state.ema_slow,
                rsi=market_state.rsi if hasattr(market_state, "rsi") else None,
                atr=atr if atr > 0 else None,
            )
            regime_result = self._regime_detector.evaluate(indicators)
        action.regime_result = regime_result

        # Check for transitions
        if self._mode == HybridMode.GRID_ONLY:
            self._evaluate_grid_mode(action, atr, price_move, adx, regime_result)
        elif self._mode == HybridMode.DCA_ACTIVE:
            self._evaluate_dca_mode(action, market_state, adx, regime_result)
        elif self._mode == HybridMode.RECOVERY_ACTIVE:
            self._evaluate_recovery_mode(action, market_state.current_price)

        return action

    # =========================================================================
    # Grid Mode Evaluation
    # =========================================================================

    def _evaluate_grid_mode(
        self,
        action: HybridAction,
        atr: Decimal,
        price_move: Decimal,
        adx: float | None,
        regime_result: RegimeResult | None,
    ) -> None:
        """Evaluate while in GRID_ONLY mode."""
        # Run grid risk check
        risk_result = self._grid_risk.check_trend_suitability(
            atr=atr, price_move=price_move, adx=adx
        )
        action.grid_risk_result = risk_result

        # Check if breakout should trigger Grid→DCA
        if risk_result.action == GridRiskAction.DEACTIVATE:
            if self._can_transition_to_dca(adx):
                event = self._execute_transition(
                    to_mode=HybridMode.DCA_ACTIVE,
                    reason=f"Grid breakout detected: {'; '.join(risk_result.reasons)}",
                    adx=adx,
                    atr_ratio=float(price_move / atr) if atr > 0 else None,
                )
                action.transition_triggered = True
                action.transition_event = event
                action.mode = self._mode

                logger.info(
                    "hybrid_grid_to_dca",
                    reasons=risk_result.reasons,
                    adx=adx,
                )
            else:
                action.warnings.append("Grid breakout detected but transition blocked")

    # =========================================================================
    # DCA Mode Evaluation
    # =========================================================================

    def _evaluate_dca_mode(
        self,
        action: HybridAction,
        market_state: MarketState,
        adx: float | None,
        regime_result: RegimeResult | None,
    ) -> None:
        """Evaluate while in DCA_ACTIVE mode."""
        # Run DCA engine if available
        if self._dca_engine is not None:
            dca_action = self._dca_engine.on_price_update(market_state)
            action.dca_action = dca_action

        # Check if should return to Grid
        if self._can_transition_to_grid(adx, regime_result):
            event = self._execute_transition(
                to_mode=HybridMode.GRID_ONLY,
                reason="Market returned to sideways, ADX below threshold",
                adx=adx,
            )
            action.transition_triggered = True
            action.transition_event = event
            action.mode = self._mode

            logger.info(
                "hybrid_dca_to_grid",
                adx=adx,
                regime=regime_result.regime.value if regime_result else None,
            )

    # =========================================================================
    # Recovery Mode
    # =========================================================================

    def enter_recovery(
        self,
        grid_positions: list,
        current_price: Decimal,
        current_bar: int,
        smc_support: Decimal | None = None,
        base_order_size: Decimal = Decimal("150"),
        direction: "SignalDirection | None" = None,
    ) -> None:
        """Transition to RECOVERY_ACTIVE mode."""
        if self._recovery_coordinator is None:
            return
        from bot.strategies.base import SignalDirection as _SD
        from bot.strategies.hybrid.recovery_coordinator import UnderwaterPosition

        if direction is None:
            direction = _SD.LONG

        underwater = [
            UnderwaterPosition(
                pos_id=p["pos_id"],
                entry_price=p["entry_price"],
                size=p["size"],
            )
            for p in grid_positions
        ]
        self._recovery_state = self._recovery_coordinator.enter_recovery(
            grid_positions=underwater,
            current_price=current_price,
            current_bar=current_bar,
            smc_support=smc_support,
            base_order_size=base_order_size,
            direction=direction,
        )
        event = self._execute_transition(
            to_mode=HybridMode.RECOVERY_ACTIVE,
            reason="Grid lower boundary breached — DCA recovery started",
        )
        self._recovery_count += 1
        logger.info(
            "hybrid_grid_to_recovery",
            grid_positions=len(underwater),
            smc_support=float(smc_support) if smc_support else None,
        )

    def _evaluate_recovery_mode(
        self,
        action: HybridAction,
        current_price: Decimal,
    ) -> None:
        """Evaluate while in RECOVERY_ACTIVE mode."""
        if self._recovery_coordinator is None or self._recovery_state is None:
            # Shouldn't happen — fallback to GRID_ONLY
            self._execute_transition(
                to_mode=HybridMode.GRID_ONLY,
                reason="Recovery state lost — fallback",
            )
            action.mode = self._mode
            return

        recovery_action = self._recovery_coordinator.on_price_update(
            current_price=current_price,
        )

        if recovery_action.should_close_all:
            # Recovery finished — transition back to GRID_ONLY
            event = self._execute_transition(
                to_mode=HybridMode.GRID_ONLY,
                reason=recovery_action.close_reason,
            )
            action.transition_triggered = True
            action.transition_event = event
            action.mode = self._mode
            self._recovery_state = None

            logger.info(
                "hybrid_recovery_to_grid",
                reason=recovery_action.close_reason,
                new_range=recovery_action.new_grid_range,
            )

        # Store recovery action in HybridAction metadata for orchestrator
        action.warnings.append(f"recovery_dca_signals={len(recovery_action.dca_signals)}")

    @property
    def recovery_coordinator(self) -> RecoveryCoordinator | None:
        return self._recovery_coordinator

    @property
    def recovery_state(self) -> RecoveryState | None:
        return self._recovery_state

    # =========================================================================
    # Transition Checks
    # =========================================================================

    def _can_transition_to_dca(self, adx: float | None) -> bool:
        """Check if Grid→DCA transition is allowed."""
        now = datetime.now(timezone.utc)

        # Check cooldown
        if self._last_transition is not None:
            elapsed = (now - self._last_transition).total_seconds()
            if elapsed < self._config.transition_cooldown_seconds:
                return False

        # Check minimum grid duration
        mode_elapsed = (now - self._mode_since).total_seconds()
        if mode_elapsed < self._config.min_grid_duration_seconds:
            return False

        # ADX must confirm trend
        if adx is not None and adx < self._config.breakout_adx_threshold:
            return False

        return True

    def _can_transition_to_grid(
        self, adx: float | None, regime_result: RegimeResult | None
    ) -> bool:
        """Check if DCA→Grid transition is allowed."""
        now = datetime.now(timezone.utc)

        # Check cooldown
        if self._last_transition is not None:
            elapsed = (now - self._last_transition).total_seconds()
            if elapsed < self._config.transition_cooldown_seconds:
                return False

        # Check minimum DCA duration
        mode_elapsed = (now - self._mode_since).total_seconds()
        if mode_elapsed < self._config.min_dca_duration_seconds:
            return False

        # ADX must be below return threshold
        if adx is not None and adx >= self._config.return_adx_threshold:
            return False

        # Regime should be sideways
        if regime_result is not None and regime_result.regime != RegimeType.SIDEWAYS:
            return False

        # Check if DCA deals are closed (if required)
        if self._config.require_dca_deals_closed and self._dca_engine is not None:
            active_deals = self._dca_engine.position_manager.get_active_deals()
            if len(active_deals) > 0:
                return False

        return True

    # =========================================================================
    # Transition Execution
    # =========================================================================

    def _execute_transition(
        self,
        to_mode: HybridMode,
        reason: str,
        adx: float | None = None,
        atr_ratio: float | None = None,
    ) -> TransitionEvent:
        """Execute a mode transition."""
        now = datetime.now(timezone.utc)
        from_mode = self._mode

        event = TransitionEvent(
            from_mode=from_mode,
            to_mode=to_mode,
            reason=reason,
            timestamp=now,
            trigger_adx=adx,
            trigger_atr_ratio=atr_ratio,
        )

        # Update state
        self._mode = to_mode
        self._mode_since = now
        self._last_transition = now
        self._total_transitions += 1

        if from_mode == HybridMode.GRID_ONLY and to_mode == HybridMode.DCA_ACTIVE:
            self._grid_to_dca_count += 1
        elif from_mode == HybridMode.DCA_ACTIVE and to_mode == HybridMode.GRID_ONLY:
            self._dca_to_grid_count += 1

        # Record history
        self._transition_history.insert(0, event)
        if len(self._transition_history) > self._config.max_transition_history:
            self._transition_history = self._transition_history[
                : self._config.max_transition_history
            ]

        logger.info(
            "hybrid_transition",
            from_mode=from_mode.value,
            to_mode=to_mode.value,
            reason=reason,
            adx=adx,
        )

        return event

    # =========================================================================
    # Status & Statistics
    # =========================================================================

    def get_status(self) -> dict[str, Any]:
        """Get current hybrid strategy status."""
        now = datetime.now(timezone.utc)
        mode_duration = (now - self._mode_since).total_seconds()

        return {
            "mode": self._mode.value,
            "mode_since": self._mode_since.isoformat(),
            "mode_duration_seconds": round(mode_duration, 1),
            "last_transition": (
                self._last_transition.isoformat() if self._last_transition else None
            ),
            "config": {
                "grid_capital_pct": self._config.grid_capital_pct,
                "dca_capital_pct": self._config.dca_capital_pct,
                "breakout_adx_threshold": self._config.breakout_adx_threshold,
                "return_adx_threshold": self._config.return_adx_threshold,
                "transition_cooldown_seconds": self._config.transition_cooldown_seconds,
            },
        }

    def get_statistics(self) -> dict[str, Any]:
        """Get transition statistics."""
        stats: dict[str, Any] = {
            "total_transitions": self._total_transitions,
            "grid_to_dca_count": self._grid_to_dca_count,
            "dca_to_grid_count": self._dca_to_grid_count,
            "recovery_count": self._recovery_count,
            "history_count": len(self._transition_history),
            "current_mode": self._mode.value,
        }
        if self._recovery_coordinator:
            stats["recovery"] = self._recovery_coordinator.get_statistics()
        return stats
