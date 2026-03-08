"""
StrategyConductor — Hierarchical strategy coordinator.

Sits **above** StrategySelector and distributes a shared context (SMC levels,
liquidity zones, working price range, capital allocation) to every active
strategy via the ``set_directive()`` interface.

Responsibilities
----------------
1. Map :class:`~bot.orchestrator.market_regime.RegimeAnalysis` →
   :class:`~bot.strategies.base.TradingMode`.
2. Build per-strategy :class:`~bot.strategies.base.StrategyDirective` objects
   that contain the *unified* SMC picture instead of letting each strategy
   recompute its own levels.
3. Push directives to all registered strategy instances whenever a regime
   change or SMC level update occurs.
4. Remain backward-compatible: when no conductor is wired into
   ``BotOrchestrator`` the bot behaves exactly as before.

Architecture
------------
``BotOrchestrator`` creates and owns a ``StrategyConductor`` instance.  After
``_update_active_strategies()`` decides *which* strategies to run via
``StrategySelector``, it calls ``conductor.on_regime_change()`` to push the
shared context to every active strategy.

  BotOrchestrator
       │
       ├── StrategySelector  ← decides which strategy types are active
       │
       └── StrategyConductor ← decides *how* active strategies should behave
               │
               ├── StrategyConductor.on_regime_change(regime, smc_context)
               │       → build StrategyDirective per strategy_type
               │       → call strategy.set_directive(directive) on each instance
               │
               └── StrategyConductor.create_directives(mode, smc_context, price)
                       → returns dict[strategy_type → StrategyDirective]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from bot.orchestrator.market_regime import MarketRegime, RegimeAnalysis
from bot.orchestrator.strategy_registry import StrategyRegistry
from bot.strategies.base import StrategyDirective, TradingMode
from bot.utils.logger import get_logger

if TYPE_CHECKING:
    from bot.core.smc.models import LiquidityLevel, SMCContext
    from bot.strategies.base import BaseStrategy

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Regime → TradingMode mapping (public for testing / customisation)
# ---------------------------------------------------------------------------

#: Default mapping from a detected :class:`MarketRegime` to the high-level
#: :class:`TradingMode` that governs strategy behaviour in that regime.
DEFAULT_REGIME_TO_MODE: dict[MarketRegime, TradingMode] = {
    MarketRegime.TIGHT_RANGE: TradingMode.MEAN_REVERSION,
    MarketRegime.WIDE_RANGE: TradingMode.MEAN_REVERSION,
    MarketRegime.QUIET_TRANSITION: TradingMode.MEAN_REVERSION,
    MarketRegime.VOLATILE_TRANSITION: TradingMode.TREND_FOLLOWING,
    MarketRegime.BULL_TREND: TradingMode.TREND_FOLLOWING,
    MarketRegime.BEAR_TREND: TradingMode.TREND_FOLLOWING,
    MarketRegime.ACCUMULATION: TradingMode.ACCUMULATION,
    MarketRegime.DISTRIBUTION: TradingMode.DISTRIBUTION,
    MarketRegime.UNKNOWN: TradingMode.MEAN_REVERSION,
}


# ---------------------------------------------------------------------------
# Per-strategy capital allocation per mode (public for customisation)
# ---------------------------------------------------------------------------

#: How much of the total available capital each strategy type receives per mode.
#: Values sum to ≤ 1.0.  Strategies absent from a mode get 0.0.
DEFAULT_MODE_ALLOCATION: dict[TradingMode, dict[str, float]] = {
    TradingMode.ACCUMULATION: {
        "dca": 0.6,
        "grid": 0.3,
        "smc": 0.1,
        "trend_follower": 0.0,
    },
    TradingMode.DISTRIBUTION: {
        "dca": 0.0,
        "grid": 0.5,
        "smc": 0.5,
        "trend_follower": 0.0,
    },
    TradingMode.TREND_FOLLOWING: {
        "dca": 0.3,
        "grid": 0.1,
        "smc": 0.2,
        "trend_follower": 0.4,
    },
    TradingMode.MEAN_REVERSION: {
        "dca": 0.2,
        "grid": 0.7,
        "smc": 0.1,
        "trend_follower": 0.0,
    },
}


# ---------------------------------------------------------------------------
# Directive snapshot (for status reporting)
# ---------------------------------------------------------------------------


@dataclass
class DirectiveSnapshot:
    """Record of a directive dispatch event."""

    timestamp: datetime
    mode: TradingMode
    regime: MarketRegime
    price_range: tuple[float, float]
    key_levels: list[float]
    strategies_notified: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "mode": self.mode.value,
            "regime": self.regime.value,
            "price_range": list(self.price_range),
            "key_levels": self.key_levels,
            "strategies_notified": self.strategies_notified,
        }


# ---------------------------------------------------------------------------
# StrategyConductor
# ---------------------------------------------------------------------------


class StrategyConductor:
    """Hierarchical strategy coordinator.

    Creates and distributes :class:`~bot.strategies.base.StrategyDirective`
    instances to all active strategies when the market regime or SMC context
    changes.

    Parameters
    ----------
    registry:
        The shared :class:`~bot.orchestrator.strategy_registry.StrategyRegistry`
        used to look up active strategy instances and call
        ``instance.strategy.set_directive()`` when available.
    regime_to_mode:
        Custom regime → mode mapping.  Defaults to
        :data:`DEFAULT_REGIME_TO_MODE`.
    mode_allocation:
        Custom capital allocation per mode.  Defaults to
        :data:`DEFAULT_MODE_ALLOCATION`.
    max_history:
        How many :class:`DirectiveSnapshot` records to keep.
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        regime_to_mode: dict[MarketRegime, TradingMode] | None = None,
        mode_allocation: dict[TradingMode, dict[str, float]] | None = None,
        max_history: int = 50,
    ) -> None:
        self._registry = registry
        self._regime_to_mode = regime_to_mode or DEFAULT_REGIME_TO_MODE
        self._mode_allocation = mode_allocation or DEFAULT_MODE_ALLOCATION
        self._max_history = max_history

        self._current_mode: TradingMode | None = None
        self._last_regime: MarketRegime | None = None
        self._history: list[DirectiveSnapshot] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def current_mode(self) -> TradingMode | None:
        """Currently active trading mode, or *None* before first dispatch."""
        return self._current_mode

    @property
    def last_regime(self) -> MarketRegime | None:
        """The most recently processed regime."""
        return self._last_regime

    @property
    def history(self) -> list[DirectiveSnapshot]:
        """Directive dispatch history, most recent first."""
        return list(self._history)

    def determine_mode(self, regime: MarketRegime) -> TradingMode:
        """Translate a detected ``MarketRegime`` into a high-level ``TradingMode``.

        Args:
            regime: The regime returned by :class:`MarketRegimeDetector`.

        Returns:
            The corresponding :class:`TradingMode`.
        """
        return self._regime_to_mode.get(regime, TradingMode.MEAN_REVERSION)

    def create_directives(
        self,
        mode: TradingMode,
        smc_context: SMCContext | None,
        current_price: float = 0.0,
    ) -> dict[str, StrategyDirective]:
        """Build one :class:`StrategyDirective` per strategy type.

        The directive encapsulates the *shared* SMC picture so that each
        strategy uses the same levels rather than independently recomputing
        their own.

        Args:
            mode: The trading mode derived from the current regime.
            smc_context: Latest :class:`~bot.core.smc.models.SMCContext` or
                *None* when SMC analysis is unavailable.
            current_price: Latest market price (used for fallback range when
                SMC context is unavailable).

        Returns:
            Mapping ``strategy_type`` → :class:`StrategyDirective`.
        """
        key_levels = _extract_key_levels(smc_context)
        liquidity_zones = _extract_liquidity_zones(smc_context)
        price_range = _compute_price_range(smc_context, current_price, mode)
        allocations = self._mode_allocation.get(mode, {})

        directives: dict[str, StrategyDirective] = {}
        for strategy_type, allocation in allocations.items():
            restrictions = _build_restrictions(strategy_type, mode, smc_context, current_price)
            directives[strategy_type] = StrategyDirective(
                mode=mode,
                price_range=price_range,
                key_levels=list(key_levels),
                liquidity_zones=list(liquidity_zones),
                capital_allocation=allocation,
                restrictions=restrictions,
            )

        return directives

    def on_regime_change(
        self,
        regime_analysis: RegimeAnalysis,
        strategy_instances: dict[str, BaseStrategy] | None = None,
    ) -> DirectiveSnapshot | None:
        """Handle a regime change: build directives and notify active strategies.

        Called by ``BotOrchestrator._update_active_strategies()`` after the
        regime has been updated and strategy transitions have been applied.

        If *strategy_instances* is provided it takes priority over the
        registry (useful for testing without a full registry setup).

        Args:
            regime_analysis: Latest :class:`RegimeAnalysis` from
                :class:`MarketRegimeDetector`.
            strategy_instances: Optional mapping of ``strategy_type`` →
                :class:`~bot.strategies.base.BaseStrategy` that bypasses the
                registry lookup.

        Returns:
            A :class:`DirectiveSnapshot` summarising what was dispatched, or
            *None* if no strategies were notified.
        """
        regime = regime_analysis.regime
        smc_context = regime_analysis.smc_context
        current_price = float(regime_analysis.analysis_details.get("current_price", 0.0))

        mode = self.determine_mode(regime)
        directives = self.create_directives(mode, smc_context, current_price)

        # Collect all notified strategy types for the snapshot
        notified: list[str] = []

        if strategy_instances is not None:
            # Use caller-provided mapping (testing / explicit wiring)
            for strategy_type, strategy in strategy_instances.items():
                if strategy_type in directives:
                    directive = directives[strategy_type]
                    strategy.set_directive(directive)
                    notified.append(strategy_type)
                    logger.debug(
                        "conductor_directive_dispatched",
                        strategy_type=strategy_type,
                        mode=mode.value,
                        regime=regime.value,
                    )
        else:
            # Push to all ACTIVE registry instances
            active_instances = self._registry.get_active()
            for instance in active_instances:
                s_type = instance.strategy_type
                if s_type not in directives:
                    continue
                directive = directives[s_type]
                # The StrategyInstance carries an optional ``strategy`` field
                # (set after the concrete strategy object is created).
                strategy_obj: BaseStrategy | None = getattr(instance, "strategy", None)
                if strategy_obj is not None and callable(
                    getattr(strategy_obj, "set_directive", None)
                ):
                    strategy_obj.set_directive(directive)
                    notified.append(s_type)
                    logger.debug(
                        "conductor_directive_dispatched",
                        strategy_id=instance.strategy_id,
                        strategy_type=s_type,
                        mode=mode.value,
                        regime=regime.value,
                    )

        self._current_mode = mode
        self._last_regime = regime

        if not notified:
            logger.debug(
                "conductor_no_strategies_notified",
                mode=mode.value,
                regime=regime.value,
            )
            # Still update state even if nothing was notified so that
            # get_status() reflects the current regime.
            return None

        price_range = _compute_price_range(smc_context, current_price, mode)
        key_levels = _extract_key_levels(smc_context)

        snapshot = DirectiveSnapshot(
            timestamp=datetime.now(timezone.utc),
            mode=mode,
            regime=regime,
            price_range=price_range,
            key_levels=key_levels,
            strategies_notified=notified,
        )
        self._add_history(snapshot)

        logger.info(
            "conductor_regime_change_processed",
            regime=regime.value,
            mode=mode.value,
            strategies_notified=notified,
            key_levels_count=len(key_levels),
        )

        return snapshot

    def get_status(self) -> dict[str, Any]:
        """Return a JSON-safe status dict for monitoring / Telegram reporting."""
        last = self._history[0] if self._history else None
        return {
            "current_mode": self._current_mode.value if self._current_mode else None,
            "last_regime": self._last_regime.value if self._last_regime else None,
            "last_dispatch": last.to_dict() if last else None,
            "history_count": len(self._history),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add_history(self, snapshot: DirectiveSnapshot) -> None:
        self._history.insert(0, snapshot)
        if len(self._history) > self._max_history:
            self._history = self._history[: self._max_history]


# ---------------------------------------------------------------------------
# Private helpers (module-level for easy unit testing)
# ---------------------------------------------------------------------------


def _extract_key_levels(smc_context: SMCContext | None) -> list[float]:
    """Return structural break-prices from *smc_context*, or empty list."""
    if smc_context is None:
        return []
    return list(smc_context.structural_levels)


def _extract_liquidity_zones(smc_context: SMCContext | None) -> list[LiquidityLevel]:
    """Return active (non-swept) liquidity levels from *smc_context*."""
    if smc_context is None:
        return []
    return [lz for lz in smc_context.liquidity_levels if not lz.swept]


def _compute_price_range(
    smc_context: SMCContext | None,
    current_price: float,
    mode: TradingMode,
) -> tuple[float, float]:
    """Derive the working price range from SMC swing points or a fallback heuristic.

    If *smc_context* contains swing points the range spans from the most
    recent swing low to the most recent swing high.  Otherwise a symmetric
    ±5 % band around *current_price* is used.
    """
    if smc_context is not None and smc_context.swing_highs and smc_context.swing_lows:
        recent_high = max(sp.price for sp in smc_context.swing_highs)
        recent_low = min(sp.price for sp in smc_context.swing_lows)
        if recent_high > recent_low:
            return (recent_low, recent_high)

    # Fallback: ±5 % band
    if current_price > 0:
        return (current_price * 0.95, current_price * 1.05)

    return (0.0, 0.0)


def _build_restrictions(
    strategy_type: str,
    mode: TradingMode,
    smc_context: SMCContext | None,
    current_price: float,
) -> dict[str, Any]:
    """Build mode-specific trade restrictions for *strategy_type*.

    Restrictions act as guard-rails to prevent strategies from opening
    positions against the dominant market structure.

    Examples
    --------
    In ``DISTRIBUTION`` mode DCA should not place fresh buy orders above the
    current price (no accumulation at the top of the range).

    In ``ACCUMULATION`` mode SMC should not enter short above the demand zone.
    """
    restrictions: dict[str, Any] = {}

    if mode == TradingMode.DISTRIBUTION:
        if strategy_type == "dca":
            # Prevent buying above current price in a distribution phase
            restrictions["no_buy_above"] = current_price
        if strategy_type == "trend_follower":
            restrictions["long_entries_disabled"] = True

    elif mode == TradingMode.ACCUMULATION:
        if strategy_type == "smc":
            restrictions["short_entries_disabled"] = True
        if strategy_type == "trend_follower":
            restrictions["short_entries_disabled"] = True

    elif mode == TradingMode.TREND_FOLLOWING:
        # Use nearest structural level as the minimum SL anchor
        if smc_context is not None and smc_context.structural_levels:
            restrictions["min_sl_anchor"] = smc_context.structural_levels[0]

    return restrictions
