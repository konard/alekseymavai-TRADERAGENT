"""
DCA Engine — v2.0.

Orchestrates signal-controlled DCA deal opening:
- Validates entry ONLY when signal generator confirms conditions
- Integrates confluence scoring with risk pre-checks
- Applies false signal filters (cooldown, confirmation, volatility)
- Manages active deals with trailing stop monitoring

Key principle: Orders are opened ONLY when the algorithm signals
target price conditions are met per strategy configuration.

Usage:
    engine = DCAEngine(
        symbol="BTC/USDT",
        signal_config=DCASignalConfig(...),
        order_config=DCAOrderConfig(...),
        risk_config=DCARiskConfig(...),
        trailing_config=TrailingStopConfig(...),
    )
    # On each price update:
    action = engine.on_price_update(market_state)
    if action.should_open_deal:
        # Execute base order on exchange
    for exit in action.deals_to_close:
        # Execute exit on exchange
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from bot.strategies.dca.dca_position_manager import (
    CloseResult,
    DCADeal,
    DCAOrderConfig,
    DCAPositionManager,
)
from bot.strategies.dca.dca_risk_manager import (
    DCARiskConfig,
    DCARiskManager,
)
from bot.strategies.dca.dca_signal_generator import (
    DCASignalConfig,
    DCASignalGenerator,
    MarketState,
    SignalResult,
)
from bot.strategies.dca.dca_trailing_stop import (
    DCATrailingStop,
    TrailingStopConfig,
    TrailingStopSnapshot,
)

# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class DealExitSignal:
    """Signal to close a specific deal."""

    deal_id: str
    reason: str
    exit_price: Decimal
    detail: str = ""


@dataclass
class EngineAction:
    """
    Result of a single engine evaluation cycle.

    Tells the caller what actions to take on the exchange.
    """

    should_open_deal: bool = False
    open_deal_reason: str = ""
    signal_result: SignalResult | None = None

    deals_to_close: list[DealExitSignal] = field(default_factory=list)
    safety_order_triggers: list[tuple[str, int]] = field(default_factory=list)  # (deal_id, level)

    warnings: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FalseSignalFilter:
    """
    Configurable false signal filters.

    These add extra protection beyond the base signal generator.
    """

    # Require N consecutive positive signals before opening
    confirmation_count: int = 1  # 1 = no extra confirmation needed
    # Minimum seconds since last signal rejection
    min_rejection_cooldown: int = 0
    # Maximum price change in last N seconds (spike filter)
    max_recent_price_change_pct: Decimal = Decimal("5.0")
    recent_price_window_seconds: int = 300  # 5 minutes


# =============================================================================
# DCA Engine
# =============================================================================


class DCAEngine:
    """
    Orchestrates DCA deal lifecycle with signal-controlled entry.

    Components:
    - DCASignalGenerator: Evaluates market conditions for entry
    - DCAPositionManager: Manages deal state and safety orders
    - DCARiskManager: Pre-open and ongoing risk checks
    - DCATrailingStop: Monitors active deals for exit

    The engine does NOT interact with the exchange directly.
    It returns EngineAction telling the caller what to do.
    """

    def __init__(
        self,
        symbol: str,
        signal_config: DCASignalConfig | None = None,
        order_config: DCAOrderConfig | None = None,
        risk_config: DCARiskConfig | None = None,
        trailing_config: TrailingStopConfig | None = None,
        false_signal_filter: FalseSignalFilter | None = None,
    ):
        self._symbol = symbol
        self._signal_gen = DCASignalGenerator(signal_config)
        self._position_mgr = DCAPositionManager(symbol, order_config)
        self._risk_mgr = DCARiskManager(risk_config)
        self._trailing_stop = DCATrailingStop(trailing_config)
        self._filter = false_signal_filter or FalseSignalFilter()

        # Internal tracking
        self._trailing_snapshots: dict[str, TrailingStopSnapshot] = {}
        self._consecutive_signals: int = 0
        self._last_rejection_time: datetime | None = None
        self._last_price: Decimal | None = None
        self._last_price_time: datetime | None = None

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def signal_generator(self) -> DCASignalGenerator:
        return self._signal_gen

    @property
    def position_manager(self) -> DCAPositionManager:
        return self._position_mgr

    @property
    def risk_manager(self) -> DCARiskManager:
        return self._risk_mgr

    @property
    def trailing_stop(self) -> DCATrailingStop:
        return self._trailing_stop

    # -----------------------------------------------------------------
    # Main Evaluation
    # -----------------------------------------------------------------

    def on_price_update(
        self,
        market_state: MarketState,
        portfolio_equity: Decimal = Decimal("0"),
        available_balance: Decimal = Decimal("0"),
        total_balance: Decimal = Decimal("0"),
        daily_pnl: Decimal = Decimal("0"),
    ) -> EngineAction:
        """
        Process a price update and determine actions.

        This is the main entry point called on each price tick.

        Args:
            market_state: Current market conditions.
            portfolio_equity: Total portfolio equity.
            available_balance: Free balance.
            total_balance: Total balance.
            daily_pnl: Realized PnL for today.

        Returns:
            EngineAction with decisions for the caller.
        """
        action = EngineAction()
        current_price = market_state.current_price

        # 1. Monitor active deals (trailing stop, safety orders)
        self._monitor_active_deals(current_price, action)

        # 2. Check for new deal signal
        self._evaluate_new_deal(
            market_state,
            action,
            portfolio_equity,
            available_balance,
            total_balance,
            daily_pnl,
        )

        # Track price for spike detection
        self._last_price = current_price
        self._last_price_time = market_state.current_time or datetime.now(timezone.utc)

        return action

    # -----------------------------------------------------------------
    # Deal Opening Logic
    # -----------------------------------------------------------------

    def _evaluate_new_deal(
        self,
        state: MarketState,
        action: EngineAction,
        equity: Decimal,
        available: Decimal,
        total: Decimal,
        daily_pnl: Decimal,
    ) -> None:
        """Evaluate whether to open a new DCA deal."""

        # Step 1: Signal evaluation
        signal = self._signal_gen.evaluate(state)
        action.signal_result = signal

        if not signal.should_open:
            self._consecutive_signals = 0
            self._last_rejection_time = datetime.now(timezone.utc)
            return

        # Step 2: False signal filters
        filter_result = self._apply_false_signal_filters(state)
        if filter_result is not None:
            action.warnings.append(filter_result)
            return

        # Step 3: Risk pre-check
        active_deals = self._position_mgr.get_active_deals()
        current_exposure = sum((d.total_cost for d in active_deals), Decimal(0))
        base_cost = self._position_mgr.config.base_order_volume

        risk_check = self._risk_mgr.can_open_new_deal(
            active_deal_count=len(active_deals),
            deal_cost=base_cost,
            current_exposure=current_exposure,
            available_balance=available,
            total_balance=total,
        )

        if not risk_check.is_safe:
            action.warnings.extend(risk_check.reasons)
            return

        # Step 4: Pump & dump check
        if self._last_price is not None and self._last_price > 0:
            price_check = self._risk_mgr.check_price_change(self._last_price, state.current_price)
            if not price_check.is_safe:
                action.warnings.extend(price_check.reasons)
                return

        # All checks passed — signal to open
        action.should_open_deal = True
        action.open_deal_reason = signal.reason

    def _apply_false_signal_filters(self, state: MarketState) -> str | None:
        """
        Apply false signal filters. Returns rejection reason or None.
        """
        flt = self._filter

        # Confirmation count
        self._consecutive_signals += 1
        if self._consecutive_signals < flt.confirmation_count:
            return f"Awaiting confirmation ({self._consecutive_signals}/{flt.confirmation_count})"

        # Rejection cooldown
        if flt.min_rejection_cooldown > 0 and self._last_rejection_time is not None:
            now = state.current_time or datetime.now(timezone.utc)
            elapsed = (now - self._last_rejection_time).total_seconds()
            if elapsed < flt.min_rejection_cooldown:
                return f"Rejection cooldown ({elapsed:.0f}s / {flt.min_rejection_cooldown}s)"

        # Price spike filter
        if (
            self._last_price is not None
            and self._last_price > 0
            and flt.max_recent_price_change_pct > 0
        ):
            change_pct = abs((state.current_price - self._last_price) / self._last_price) * 100
            if change_pct > flt.max_recent_price_change_pct:
                return (
                    f"Price spike detected ({change_pct:.1f}% > {flt.max_recent_price_change_pct}%)"
                )

        return None

    # -----------------------------------------------------------------
    # Active Deal Monitoring
    # -----------------------------------------------------------------

    def _monitor_active_deals(self, current_price: Decimal, action: EngineAction) -> None:
        """Monitor active deals for trailing stop and safety orders."""

        for deal in self._position_mgr.get_active_deals():
            # Update highest price
            self._position_mgr.update_highest_price(deal.id, current_price)

            # Check trailing stop
            if self._trailing_stop.enabled:
                snapshot = self._trailing_snapshots.setdefault(deal.id, TrailingStopSnapshot())
                ts_result = self._trailing_stop.evaluate(
                    current_price=current_price,
                    average_entry=deal.average_entry_price,
                    highest_price=deal.highest_price_since_entry,
                    snapshot=snapshot,
                )

                if ts_result.should_exit:
                    action.deals_to_close.append(
                        DealExitSignal(
                            deal_id=deal.id,
                            reason="trailing_stop",
                            exit_price=current_price,
                            detail=ts_result.reason,
                        )
                    )
                    continue

            # Check take profit (if trailing not enabled or not triggered)
            if not self._trailing_stop.enabled:
                tp_price = self._position_mgr.get_take_profit_price(deal.id)
                if current_price >= tp_price:
                    action.deals_to_close.append(
                        DealExitSignal(
                            deal_id=deal.id,
                            reason="take_profit",
                            exit_price=current_price,
                            detail=f"Price {current_price} >= TP {tp_price}",
                        )
                    )
                    continue

            # Check stop loss
            sl_price = self._position_mgr.get_stop_loss_price(deal.id)
            if current_price <= sl_price:
                action.deals_to_close.append(
                    DealExitSignal(
                        deal_id=deal.id,
                        reason="stop_loss",
                        exit_price=current_price,
                        detail=f"Price {current_price} <= SL {sl_price}",
                    )
                )
                continue

            # Check safety order triggers
            so_trigger = self._position_mgr.check_safety_order_trigger(deal.id, current_price)
            if so_trigger is not None:
                action.safety_order_triggers.append((deal.id, so_trigger.level))

    # -----------------------------------------------------------------
    # Deal Execution Callbacks
    # -----------------------------------------------------------------

    def open_deal(self, entry_price: Decimal) -> DCADeal:
        """
        Open a new deal (called after exchange order succeeds).

        Returns the new DCADeal.
        """
        deal = self._position_mgr.open_deal(entry_price)
        self._trailing_snapshots[deal.id] = TrailingStopSnapshot(
            highest_price_since_entry=entry_price,
        )
        self._consecutive_signals = 0
        return deal

    def fill_safety_order(self, deal_id: str, level: int, fill_price: Decimal) -> DCADeal:
        """Record safety order fill (called after exchange order succeeds)."""
        return self._position_mgr.fill_safety_order(deal_id, level, fill_price)

    def close_deal(self, deal_id: str, exit_price: Decimal, reason: str) -> CloseResult:
        """Close a deal (called after exchange sell order succeeds)."""
        result = self._position_mgr.close_deal(deal_id, exit_price, reason)
        self._risk_mgr.record_trade_result(result.realized_profit)
        # Clean up snapshot
        self._trailing_snapshots.pop(deal_id, None)
        return result

    # -----------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------

    def get_statistics(self) -> dict[str, Any]:
        """Get combined statistics from all components."""
        return {
            "symbol": self._symbol,
            "signal_generator": self._signal_gen.get_statistics(),
            "position_manager": self._position_mgr.get_statistics(),
            "risk_manager": self._risk_mgr.get_statistics(),
            "trailing_stop": self._trailing_stop.get_statistics(),
            "filter": {
                "confirmation_count": self._filter.confirmation_count,
                "consecutive_signals": self._consecutive_signals,
            },
        }
