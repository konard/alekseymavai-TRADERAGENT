"""
bot/core/smc/analyzer.py — SMC main orchestrator.

Orchestrates all sub-detectors and produces a single SMCContext per call.
Stateless: each call to analyze() re-scans the full OHLCV window.

Warmup requirement: at least min_warmup_bars (default 200) bars before
returning a meaningful context. Returns an empty SMCContext with
warmup_complete=False until then.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from bot.core.smc.imbalance_detector import ImbalanceDetector
from bot.core.smc.models import SMCContext, SMCPhase
from bot.core.smc.structural_detector import StructuralDetector
from bot.core.smc.supply_demand_detector import SupplyDemandDetector
from bot.core.smc.swing_detector import find_swing_points
from bot.utils.logger import get_logger

logger = get_logger(__name__)

# ATR period used when no 'atr' column is present
_DEFAULT_ATR_PERIOD = 14


def _compute_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = _DEFAULT_ATR_PERIOD
) -> np.ndarray:
    """Compute ATR using Wilder's smoothing (EWM with alpha=1/period)."""
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    # EWM smoothing
    atr = np.empty(n)
    atr[0] = tr[0]
    alpha = 1.0 / period
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i - 1]
    return atr


class SMCAnalyzer:
    """
    Main SMC analysis engine.

    Parameters
    ----------
    swing_strength : int
        Number of bars each side for swing confirmation (LuxAlgo default=5).
    min_warmup_bars : int
        Minimum bars required before context is flagged as ready.
    min_impulse_atr : float
        Structural detector filter — minimum impulse as ATR multiple.
    min_fvg_atr : float
        FVG size filter.
    tolerance_pct : float
        EQH/EQL clustering tolerance.
    min_eq_touches : int
        Minimum touches for EQH/EQL level.
    max_ob_lookback : int
        Max lookback bars when searching for OB candle.
    """

    def __init__(
        self,
        swing_strength: int = 5,
        min_warmup_bars: int = 200,
        min_impulse_atr: float = 0.3,
        min_fvg_atr: float = 0.2,
        tolerance_pct: float = 0.002,
        min_eq_touches: int = 2,
        max_ob_lookback: int = 20,
        max_ob_count: int = 10,
        max_fvg_count: int = 10,
    ) -> None:
        self.swing_strength = swing_strength
        self.min_warmup_bars = min_warmup_bars

        self._structural = StructuralDetector(min_impulse_atr=min_impulse_atr)
        self._imbalance = ImbalanceDetector(
            min_fvg_atr=min_fvg_atr,
            max_ob_lookback=max_ob_lookback,
            max_fvg_count=max_fvg_count,
            max_ob_count=max_ob_count,
        )
        self._sd = SupplyDemandDetector(
            tolerance_pct=tolerance_pct,
            min_touches=min_eq_touches,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, df: pd.DataFrame) -> SMCContext:
        """
        Full SMC analysis of the provided OHLCV DataFrame.

        Expected columns: open, high, low, close, volume.
        Optional column : atr  (pre-computed 14-period ATR).

        Returns
        -------
        SMCContext — the complete SMC picture at the last bar.
        """
        n = len(df)
        bars_analyzed = n
        warmup_complete = n >= self.min_warmup_bars

        if n < 2 * self.swing_strength + 1:
            logger.debug("smc_analyzer_insufficient_bars", bars=n)
            return SMCContext(
                warmup_complete=False,
                bars_analyzed=bars_analyzed,
                current_price=float(df["close"].iloc[-1]) if n > 0 else 0.0,
                bar_index=n - 1 if n > 0 else 0,
            )

        high = df["high"].to_numpy(dtype=np.float64)
        low = df["low"].to_numpy(dtype=np.float64)
        open_ = df["open"].to_numpy(dtype=np.float64)
        close = df["close"].to_numpy(dtype=np.float64)

        # ATR
        if "atr" in df.columns:
            atr = df["atr"].to_numpy(dtype=np.float64)
        else:
            atr = _compute_atr(high, low, close)

        # 1. Swing detection
        swing_points = find_swing_points(high, low, strength=self.swing_strength)
        swing_highs = [s for s in swing_points if s.is_high]
        swing_lows = [s for s in swing_points if s.is_low]

        # 2. Structural events
        structure_events = self._structural.detect(swing_points, close, atr)

        # 3. FVG + OB
        fvgs = self._imbalance.detect_fvg(open_, high, low, close, atr)
        obs = self._imbalance.detect_ob(open_, high, low, close, atr, structure_events)

        # 4. Liquidity levels
        liquidity = self._sd.detect(swing_highs, swing_lows, obs, close)

        # 5. Derive phase and bias
        phase, trend_bias = self._derive_phase(structure_events)

        # 6. Compute structural levels and confidence
        structural_levels = [ev.break_price for ev in reversed(structure_events)]
        confidence = self._compute_confidence(
            structure_events=structure_events,
            warmup_complete=warmup_complete,
        )

        current_price = float(close[-1])
        bar_index = n - 1

        logger.debug(
            "smc_analysis_complete",
            bars=n,
            warmup=warmup_complete,
            phase=phase.value,
            bias=round(trend_bias, 2),
            confidence=round(confidence, 2),
            swings=len(swing_points),
            events=len(structure_events),
            obs=len(obs),
            fvgs=len(fvgs),
            liquidity=len(liquidity),
        )

        return SMCContext(
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            structure_events=list(reversed(structure_events)),  # most recent first
            order_blocks=obs,
            fair_value_gaps=fvgs,
            liquidity_levels=liquidity,
            phase=phase,
            trend_bias=trend_bias,
            confidence=confidence,
            structural_levels=structural_levels,
            bar_index=bar_index,
            current_price=current_price,
            warmup_complete=warmup_complete,
            bars_analyzed=bars_analyzed,
        )

    # ------------------------------------------------------------------
    # Level helpers
    # ------------------------------------------------------------------

    def find_next_support(self, ctx: SMCContext, price: float) -> float | None:
        """
        Return the nearest support level below *price*.

        Searches (in priority order):
        1. Bullish OBs (demand zones) — high edge below price
        2. Bull FVGs — gap_low below price
        3. EQL/DEMAND liquidity levels below price

        Returns the highest qualifying level (closest support below price),
        or None if nothing is found.
        """
        candidates: list[float] = []

        # 1. Bullish order blocks — use OB high as the support ceiling
        for ob in ctx.order_blocks:
            if ob.ob_type.value == "bull" and not ob.invalidated and ob.high < price:
                candidates.append(ob.high)

        # 2. Bullish FVGs — gap_low as the nearest support within the gap
        for fvg in ctx.fair_value_gaps:
            if fvg.fvg_type.value == "bull" and not fvg.filled and fvg.gap_low < price:
                candidates.append(fvg.gap_low)

        # 3. EQL / DEMAND liquidity levels below price
        for lv in ctx.liquidity_levels:
            if (
                lv.liquidity_type.value in ("eql", "demand")
                and not lv.swept
                and lv.price < price
            ):
                candidates.append(lv.price)

        return max(candidates) if candidates else None

    def find_next_resistance(self, ctx: SMCContext, price: float) -> float | None:
        """
        Return the nearest resistance level above *price*.

        Searches (in priority order):
        1. Bearish OBs (supply zones) — low edge above price
        2. Bear FVGs — gap_high above price
        3. EQH/SUPPLY liquidity levels above price

        Returns the lowest qualifying level (closest resistance above price),
        or None if nothing is found.
        """
        candidates: list[float] = []

        # 1. Bearish order blocks — use OB low as the resistance floor
        for ob in ctx.order_blocks:
            if ob.ob_type.value == "bear" and not ob.invalidated and ob.low > price:
                candidates.append(ob.low)

        # 2. Bearish FVGs — gap_high as the nearest resistance
        for fvg in ctx.fair_value_gaps:
            if fvg.fvg_type.value == "bear" and not fvg.filled and fvg.gap_high > price:
                candidates.append(fvg.gap_high)

        # 3. EQH / SUPPLY liquidity levels above price
        for lv in ctx.liquidity_levels:
            if (
                lv.liquidity_type.value in ("eqh", "supply")
                and not lv.swept
                and lv.price > price
            ):
                candidates.append(lv.price)

        return min(candidates) if candidates else None

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _compute_confidence(
        self,
        structure_events: list,
        warmup_complete: bool,
    ) -> float:
        """
        Compute a blended confidence score (0.0 – 1.0) for the detected phase.

        Factors
        -------
        - warmup_complete: half the weight; pre-warmup contexts are unreliable.
        - Number of recent events: more events → more evidence → higher score,
          capped at 1.0 above 5 events.
        - Recency: bonus when the last event was recent (within the last 20 bars).
        """
        if not structure_events:
            return 0.0

        # Warmup factor (0 or 0.5)
        warmup_factor = 0.5 if warmup_complete else 0.0

        # Evidence factor: events in the trailing window (last 10)
        recent = structure_events[-10:]
        evidence_factor = min(len(recent) / 5.0, 1.0) * 0.35

        # Recency factor: last event within last 20 bars
        last_event = structure_events[-1]
        n_bars = structure_events[-1].index if structure_events else 0
        last_idx = last_event.index
        # How recent relative to the whole dataset (using absolute bar index)
        # We don't have total bars here, so use proximity to last_event's index
        recency_factor = 0.0
        if len(structure_events) >= 2:
            # Distance from penultimate to last event
            gap = structure_events[-1].index - structure_events[-2].index
            if gap <= 20:
                recency_factor = 0.15
        elif len(structure_events) == 1:
            recency_factor = 0.15  # Only one event — it's the most recent by definition

        return min(warmup_factor + evidence_factor + recency_factor, 1.0)

    # ------------------------------------------------------------------
    # Phase derivation
    # ------------------------------------------------------------------

    def _derive_phase(
        self,
        structure_events: list,
    ) -> tuple[SMCPhase, float]:
        """
        Derive market phase from the sequence of structural events.

        Rules:
          - Last event is BOS_BULL → BULL_TREND, bias approaches +1
          - Last event is BOS_BEAR → BEAR_TREND, bias approaches -1
          - Last event is CHOCH_BULL → ACCUMULATION (reversal attempt)
          - Last event is CHOCH_BEAR → DISTRIBUTION (reversal attempt)
          - No events → RANGING, bias 0
        """
        if not structure_events:
            return SMCPhase.RANGING, 0.0

        # Count recent bull vs bear events (last 10) for bias magnitude
        recent = structure_events[-10:]
        bull_count = sum(1 for e in recent if e.is_bullish)
        bear_count = len(recent) - bull_count
        raw_bias = (bull_count - bear_count) / len(recent)  # in [-1, 1]
        trend_bias = float(np.clip(raw_bias, -1.0, 1.0))

        last = structure_events[-1]
        from bot.core.smc.models import StructureType

        if last.structure_type == StructureType.BOS_BULL:
            return SMCPhase.BULL_TREND, trend_bias
        if last.structure_type == StructureType.BOS_BEAR:
            return SMCPhase.BEAR_TREND, trend_bias
        if last.structure_type == StructureType.CHOCH_BULL:
            return SMCPhase.ACCUMULATION, trend_bias
        if last.structure_type == StructureType.CHOCH_BEAR:
            return SMCPhase.DISTRIBUTION, trend_bias

        return SMCPhase.RANGING, 0.0
