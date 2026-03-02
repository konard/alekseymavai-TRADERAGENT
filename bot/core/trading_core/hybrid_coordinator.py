"""
HybridCoordinator — unified Grid+DCA coordination logic.

Extracts the decision algorithm from BotOrchestrator._process_hybrid_logic()
and HybridStrategy.evaluate() into a pure, stateless function that can be
used identically in both the live bot and the backtest engine.

The live bot delegates to this coordinator instead of calling HybridStrategy
directly; the backtest engine calls it during signal processing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

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
    def grid_only(cls, reason: str = "") -> "CoordinatedDecision":
        return cls(mode=HybridMode.GRID_ONLY, run_grid=True, run_dca=False, reason=reason)

    @classmethod
    def dca_active(cls, reason: str = "") -> "CoordinatedDecision":
        return cls(mode=HybridMode.DCA_ACTIVE, run_grid=False, run_dca=True, reason=reason)

    @classmethod
    def both_active(cls, reason: str = "") -> "CoordinatedDecision":
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
