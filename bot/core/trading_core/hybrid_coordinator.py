"""
HybridCoordinator — unified Grid+DCA coordination logic.

Extracts the decision algorithm from BotOrchestrator._process_hybrid_logic()
and HybridStrategy.evaluate() into a pure, stateless function that can be
used identically in both the live bot and the backtest engine.

The live bot delegates to this coordinator instead of calling HybridStrategy
directly; the backtest engine calls it during signal processing.

CorePosition integration (Issue #359)
--------------------------------------
HybridCoordinator owns the lifecycle of the shared *CorePosition* object:

* ``create_core_position(symbol, side)`` — call once when entering Hybrid mode
  (or when the first DCA fill occurs).  Attaches the position to DCA and Grid
  engines so they stay in sync automatically.
* ``release_core_position()`` — call when leaving Hybrid mode.  Detaches from
  engines and resets the coordinator's reference.
* ``apply_grid_constraints(grid_engine)`` — convenience wrapper that delegates
  to ``GridEngine.apply_position_constraints(core_position)``.

When no engines are wired in (stateless / backtest usage), the coordinator
still tracks the CorePosition object for inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from bot.core.dca_engine import DCAEngine
    from bot.core.grid_engine import GridEngine
    from bot.core.trading_core.core_position import CorePosition

from bot.core.trading_core.core_position import CorePosition
from bot.strategies.hybrid.hybrid_config import HybridMode


@dataclass
class CoordinatedDecision:
    """Result from HybridCoordinator.evaluate()."""

    mode: HybridMode
    run_grid: bool
    run_dca: bool
    transition_triggered: bool = False
    reason: str = ""

    @classmethod
    def grid_only(cls, reason: str = "") -> CoordinatedDecision:
        return cls(mode=HybridMode.GRID_ONLY, run_grid=True, run_dca=False, reason=reason)

    @classmethod
    def dca_active(cls, reason: str = "") -> CoordinatedDecision:
        return cls(mode=HybridMode.DCA_ACTIVE, run_grid=False, run_dca=True, reason=reason)

    @classmethod
    def both_active(cls, reason: str = "") -> CoordinatedDecision:
        return cls(mode=HybridMode.GRID_ONLY, run_grid=True, run_dca=True, reason=reason)


class HybridCoordinator:
    """
    Pure coordination logic for Grid + DCA strategies.

    Decision rules (mirrors HybridStrategy.evaluate()):
    - ADX > adx_dca_threshold  → DCA_ACTIVE  (strong trend, DCA more appropriate)
    - ADX ≤ adx_dca_threshold  → GRID_ONLY   (range-bound, grid optimal)
    - No ADX data available     → GRID_ONLY  (safe default)

    This class is intentionally stateless — call evaluate() freely from any
    context (live, backtest, unit test) without side effects.

    CorePosition lifecycle
    ~~~~~~~~~~~~~~~~~~~~~~
    An optional *CorePosition* is managed here so that DCA and Grid share a
    single, authoritative view of the accumulated position:

    1. Call ``create_core_position(symbol, side)`` when entering Hybrid mode.
    2. Optionally wire engines via ``attach_engines(dca_engine, grid_engine)``.
    3. Call ``apply_grid_constraints(grid_engine)`` each cycle to keep the grid
       aligned with the current DCA position.
    4. Call ``release_core_position()`` when leaving Hybrid mode.

    If no engines are wired in (pure stateless usage), ``evaluate()`` works
    exactly as before — full backward compatibility.

    Args:
        adx_dca_threshold:
            ADX level above which DCA takes over from Grid.
            Matches HybridConfig default (25).
        allow_both:
            If True, both strategies run simultaneously when ADX is near the
            threshold (within ``adx_tolerance``).  Default False.
        adx_tolerance:
            Band around ``adx_dca_threshold`` where both can run (only used
            when ``allow_both=True``).
    """

    def __init__(
        self,
        adx_dca_threshold: float = 25.0,
        allow_both: bool = False,
        adx_tolerance: float = 3.0,
    ) -> None:
        self.adx_dca_threshold = adx_dca_threshold
        self.allow_both = allow_both
        self.adx_tolerance = adx_tolerance

        # Shared CorePosition — None when not in Hybrid mode.
        self._core_position: Optional[CorePosition] = None

        # Wired engines (optional; only used when coordinator manages lifecycle).
        self._dca_engine: Optional[DCAEngine] = None
        self._grid_engine: Optional[GridEngine] = None

    # ------------------------------------------------------------------
    # CorePosition lifecycle
    # ------------------------------------------------------------------

    def create_core_position(self, symbol: str, side: str = "long") -> CorePosition:
        """
        Create a fresh CorePosition and attach it to any wired engines.

        Call this once when entering Hybrid mode (before the first DCA fill).

        Args:
            symbol: Trading pair (e.g. 'BTC/USDT').
            side: 'long' or 'short'.

        Returns:
            The new CorePosition instance (also accessible via
            ``coordinator.core_position``).
        """
        self._core_position = CorePosition(symbol=symbol, side=side)
        if self._dca_engine is not None:
            self._dca_engine.attach_core_position(self._core_position)
        return self._core_position

    def release_core_position(self) -> None:
        """
        Discard the current CorePosition and detach from wired engines.

        Call this when leaving Hybrid mode or closing the accumulated position.
        """
        if self._dca_engine is not None:
            self._dca_engine.detach_core_position()
        self._core_position = None

    @property
    def core_position(self) -> Optional[CorePosition]:
        """The active CorePosition, or None when not in Hybrid mode."""
        return self._core_position

    # ------------------------------------------------------------------
    # Engine wiring
    # ------------------------------------------------------------------

    def attach_engines(
        self,
        dca_engine: Optional[DCAEngine] = None,
        grid_engine: Optional[GridEngine] = None,
    ) -> None:
        """
        Wire DCA / Grid engines so the coordinator can manage their
        CorePosition reference automatically.

        Engines are optional — pass only the ones you want wired.
        If a CorePosition already exists, the DCA engine is attached
        immediately.

        Args:
            dca_engine: The DCAEngine instance used in this Hybrid session.
            grid_engine: The GridEngine instance (used in ``apply_grid_constraints``).
        """
        self._dca_engine = dca_engine
        self._grid_engine = grid_engine
        # If CorePosition was created before engines were wired, attach now.
        if self._core_position is not None and dca_engine is not None:
            dca_engine.attach_core_position(self._core_position)

    # ------------------------------------------------------------------
    # Grid constraint delegation
    # ------------------------------------------------------------------

    def apply_grid_constraints(
        self,
        grid_engine: Optional[GridEngine] = None,
        *,
        dca_protection_levels: int = 2,
        max_sell_ratio: Decimal = Decimal("0.5"),
    ) -> dict:
        """
        Apply CorePosition-aware constraints to the Grid.

        Delegates to ``GridEngine.apply_position_constraints()``.  When
        *grid_engine* is None, falls back to the wired engine (if any).
        When there is no active CorePosition, returns an empty report
        (no-op).

        Args:
            grid_engine: The GridEngine to constrain.  If None, the
                previously wired engine is used.
            dca_protection_levels: Passed through to GridEngine.
            max_sell_ratio: Passed through to GridEngine.

        Returns:
            Constraint report dict from GridEngine.apply_position_constraints().
        """
        target_grid = grid_engine or self._grid_engine
        if target_grid is None:
            return {}
        return target_grid.apply_position_constraints(
            self._core_position,
            dca_protection_levels=dca_protection_levels,
            max_sell_ratio=max_sell_ratio,
        )

    def evaluate(
        self,
        adx: float | None,
        current_price: Decimal | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CoordinatedDecision:
        """
        Determine which strategies should run this cycle.

        Args:
            adx: Current ADX value from regime analysis.  None → no trend data.
            current_price: Current market price (reserved for future extensions).
            extra: Optional extra context (unused, for forward compatibility).

        Returns:
            CoordinatedDecision with run_grid / run_dca flags.
        """
        if adx is None:
            return CoordinatedDecision.grid_only(reason="no_adx_data")

        upper = self.adx_dca_threshold + self.adx_tolerance
        lower = self.adx_dca_threshold - self.adx_tolerance

        if self.allow_both and lower <= adx <= upper:
            return CoordinatedDecision.both_active(
                reason=f"adx={adx:.1f} in tolerance band [{lower:.1f},{upper:.1f}]"
            )

        if adx > self.adx_dca_threshold:
            return CoordinatedDecision.dca_active(
                reason=f"adx={adx:.1f} > threshold={self.adx_dca_threshold}"
            )

        return CoordinatedDecision.grid_only(
            reason=f"adx={adx:.1f} <= threshold={self.adx_dca_threshold}"
        )
