"""
MarketRegimeDetector v2.0 - 6-regime classifier with ADX hysteresis.

Market Regimes (v2.0):
- TIGHT_RANGE: ADX<18, ATR<1% → Grid (tight spreads)
- WIDE_RANGE: ADX<18, ATR>=1% → Grid (wider spreads)
- QUIET_TRANSITION: ADX 22-32, ATR<2% → Hold
- VOLATILE_TRANSITION: ADX 22-32, ATR>=2% → Reduce exposure
- BULL_TREND: ADX>32, EMA20>EMA50 → Trend follower / DCA
- BEAR_TREND: ADX>32, EMA20<EMA50 → DCA
- ACCUMULATION: SMC CHoCH_BULL detected → SMC strategy (potential reversal up)
- DISTRIBUTION: SMC CHoCH_BEAR detected → SMC strategy (potential reversal down)

ADX Hysteresis (prevents regime oscillation):
- Enter trending: ADX must rise above 32
- Exit trending: ADX must drop below 25
- Enter ranging: ADX must drop below 18
- Exit ranging: ADX must rise above 22

Strategy Selection Logic (v2.0):
- TIGHT_RANGE / WIDE_RANGE → Grid Engine
- BULL_TREND + High Confluence (≥0.7) → Hybrid Mode
- BULL_TREND + Low Confluence → DCA Engine
- BEAR_TREND → DCA Engine
- QUIET_TRANSITION → Hold
- VOLATILE_TRANSITION → Reduce exposure
- ACCUMULATION / DISTRIBUTION → SMC Engine (set via analyze_with_smc())

Indicators:
- EMA crossover (fast/slow) for trend direction
- ADX for trend strength with hysteresis thresholds
- ATR % for volatility-based regime subdivision
- Bollinger Bands width for volatility detection
- RSI for momentum
- Volume ratio for regime confirmation
"""

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.core.smc.models import SMCContext

import numpy as np
import pandas as pd

from bot.utils.logger import get_logger

logger = get_logger(__name__)


class MarketRegime(str, Enum):
    """Detected market regime (v2.0 — 6 regimes with ADX hysteresis)."""

    TIGHT_RANGE = "tight_range"  # ADX<18, ATR<1%
    WIDE_RANGE = "wide_range"  # ADX<18, ATR>=1%
    QUIET_TRANSITION = "quiet_transition"  # ADX 22-32, ATR<2%
    VOLATILE_TRANSITION = "volatile_transition"  # ADX 22-32, ATR>=2%
    BULL_TREND = "bull_trend"  # ADX>32, EMA20>EMA50
    BEAR_TREND = "bear_trend"  # ADX>32, EMA20<EMA50
    ACCUMULATION = "accumulation"  # SMC CHoCH_BULL — smart money accumulating
    DISTRIBUTION = "distribution"  # SMC CHoCH_BEAR — smart money distributing
    UNKNOWN = "unknown"


class RecommendedStrategy(str, Enum):
    """Strategy recommendation based on market regime."""

    GRID = "grid"
    DCA = "dca"
    HYBRID = "hybrid"
    SMC = "smc"
    REDUCE_EXPOSURE = "reduce_exposure"
    HOLD = "hold"


@dataclass
class RegimeAnalysis:
    """Result of market regime detection."""

    regime: MarketRegime
    confidence: float  # 0.0 - 1.0
    recommended_strategy: RecommendedStrategy
    confluence_score: float  # 0.0 - 1.0

    # Market metrics
    trend_strength: float  # -1.0 (strong bearish) to +1.0 (strong bullish)
    volatility_percentile: float  # 0-100
    ema_divergence_pct: float  # EMA spread as %
    atr_pct: float  # ATR as % of price
    rsi: float  # RSI value

    # Enhanced indicators (v2.0)
    adx: float  # Average Directional Index (0-100)
    bb_width_pct: float  # Bollinger Bands width as % of middle band
    volume_ratio: float  # Current volume / average volume

    # Regime tracking
    regime_duration_seconds: int  # How long current regime has persisted
    previous_regime: MarketRegime | None  # Previous different regime

    timestamp: datetime
    analysis_details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "regime": self.regime.value,
            "confidence": round(self.confidence, 4),
            "recommended_strategy": self.recommended_strategy.value,
            "confluence_score": round(self.confluence_score, 4),
            "trend_strength": round(self.trend_strength, 4),
            "volatility_percentile": round(self.volatility_percentile, 2),
            "ema_divergence_pct": round(self.ema_divergence_pct, 4),
            "atr_pct": round(self.atr_pct, 4),
            "rsi": round(self.rsi, 2),
            "adx": round(self.adx, 2),
            "bb_width_pct": round(self.bb_width_pct, 4),
            "volume_ratio": round(self.volume_ratio, 4),
            "regime_duration_seconds": self.regime_duration_seconds,
            "previous_regime": self.previous_regime.value if self.previous_regime else None,
            "timestamp": self.timestamp.isoformat(),
            "analysis_details": self.analysis_details,
        }


class MarketRegimeDetector:
    """
    Detects market regime using technical indicators and recommends trading strategy.

    Uses EMA crossover, ADX, Bollinger Bands, ATR, RSI, and volume analysis
    to determine whether the market is trending, ranging, or highly volatile.
    """

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        atr_period: int = 14,
        rsi_period: int = 14,
        adx_period: int = 14,
        bb_period: int = 20,
        bb_std_dev: float = 2.0,
        volume_lookback: int = 20,
        trend_threshold: float = 0.5,
        high_volatility_percentile: float = 90.0,
        confluence_threshold: float = 0.7,
        regime_history_size: int = 10,
    ):
        """
        Args:
            ema_fast: Fast EMA period.
            ema_slow: Slow EMA period.
            atr_period: ATR calculation period.
            rsi_period: RSI calculation period.
            adx_period: ADX calculation period.
            bb_period: Bollinger Bands period.
            bb_std_dev: Bollinger Bands standard deviation multiplier.
            volume_lookback: Volume average lookback period.
            trend_threshold: EMA divergence % threshold for trend detection.
            high_volatility_percentile: ATR percentile for high-volatility regime.
            confluence_threshold: Score above which Hybrid mode is recommended.
            regime_history_size: Number of regime analyses to keep in history.
        """
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.rsi_period = rsi_period
        self.adx_period = adx_period
        self.bb_period = bb_period
        self.bb_std_dev = bb_std_dev
        self.volume_lookback = volume_lookback
        self.trend_threshold = trend_threshold
        self.high_volatility_percentile = high_volatility_percentile
        self.confluence_threshold = confluence_threshold
        self.regime_history_size = regime_history_size

        # ADX hysteresis thresholds (v2.0)
        self.adx_enter_trending = 32.0
        self.adx_exit_trending = 25.0
        self.adx_enter_ranging = 18.0
        self.adx_exit_ranging = 22.0
        self.atr_wide_threshold = 1.0  # % — splits tight/wide range
        self.atr_volatile_threshold = 2.0  # % — splits quiet/volatile transition

        self._last_analysis: RegimeAnalysis | None = None
        self._regime_history: list[RegimeAnalysis] = []

        # Log-spam suppression
        self._insufficient_data_count: int = 0

    @property
    def last_analysis(self) -> RegimeAnalysis | None:
        """Return the most recent analysis result."""
        return self._last_analysis

    @property
    def regime_history(self) -> list[RegimeAnalysis]:
        """Return regime analysis history (most recent first)."""
        return self._regime_history.copy()

    def analyze(self, df: pd.DataFrame) -> RegimeAnalysis:
        """
        Analyze market data and detect current regime.

        Args:
            df: OHLCV DataFrame with columns: open, high, low, close, volume.
                Must have sufficient rows for all indicator calculations.

        Returns:
            RegimeAnalysis with regime, confidence, and strategy recommendation.
        """
        min_rows = max(
            self.ema_slow + self.atr_period,
            self.adx_period * 2,
            self.bb_period + self.volume_lookback,
        )
        if len(df) < min_rows:
            self._insufficient_data_count += 1
            if self._insufficient_data_count == 1:
                logger.warning(
                    "insufficient_data",
                    required=min_rows,
                    received=len(df),
                )
            return self._unknown_analysis("Insufficient data")

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        # Calculate indicators
        ema_fast_vals = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema_slow_vals = close.ewm(span=self.ema_slow, adjust=False).mean()
        atr = self._calculate_atr(high, low, close, self.atr_period)
        rsi = self._calculate_rsi(close, self.rsi_period)
        adx_vals, plus_di, minus_di = self._calculate_adx(high, low, close, self.adx_period)
        bb_upper, bb_middle, bb_lower, bb_width_pct = self._calculate_bollinger_bands(
            close, self.bb_period, self.bb_std_dev
        )
        avg_volume, volume_ratio = self._calculate_volume_ratio(volume, self.volume_lookback)

        # Extract current values
        current_price = float(close.iloc[-1])
        current_ema_fast = float(ema_fast_vals.iloc[-1])
        current_ema_slow = float(ema_slow_vals.iloc[-1])
        current_atr = float(atr.iloc[-1])
        current_rsi = float(rsi.iloc[-1])

        # Safe extraction for new indicators (handle NaN)
        current_adx = float(adx_vals.iloc[-1]) if not pd.isna(adx_vals.iloc[-1]) else 20.0
        current_bb_width = (
            float(bb_width_pct.iloc[-1]) if not pd.isna(bb_width_pct.iloc[-1]) else 4.0
        )
        current_volume_ratio = (
            float(volume_ratio.iloc[-1]) if not pd.isna(volume_ratio.iloc[-1]) else 1.0
        )

        # EMA divergence as percentage
        ema_divergence_pct = (
            (current_ema_fast - current_ema_slow) / current_ema_slow * 100
            if current_ema_slow != 0
            else 0.0
        )

        # ATR as percentage of price
        atr_pct = (current_atr / current_price * 100) if current_price != 0 else 0.0

        # ATR percentile (how volatile relative to recent history)
        atr_values = atr.dropna().values
        if len(atr_values) > 0:
            vol_pctile = float((np.sum(atr_values <= current_atr) / len(atr_values)) * 100)
        else:
            vol_pctile = 50.0

        # Trend strength: blend EMA divergence with ADX confirmation
        ema_trend = max(-1.0, min(1.0, ema_divergence_pct / 2.0))
        adx_factor = min(current_adx / 50.0, 1.0)
        trend_strength = ema_trend * (0.5 + 0.5 * adx_factor)

        # Detect regime (v2.0: state-dependent hysteresis)
        current_regime = self._last_analysis.regime if self._last_analysis else None
        regime = self._classify_regime(
            adx=current_adx,
            atr_pct=atr_pct,
            ema_fast=current_ema_fast,
            ema_slow=current_ema_slow,
            current_regime=current_regime,
        )

        # Calculate confluence score
        confluence_score = self._calculate_confluence(
            trend_strength=trend_strength,
            rsi=current_rsi,
            volatility_percentile=vol_pctile,
            ema_divergence_pct=ema_divergence_pct,
            adx=current_adx,
            bb_width_pct=current_bb_width,
            volume_ratio=current_volume_ratio,
        )

        # Determine recommended strategy
        recommended = self._recommend_strategy(regime, confluence_score, current_adx)

        # Confidence based on signal clarity
        confidence = self._calculate_confidence(
            regime=regime,
            ema_divergence_pct=abs(ema_divergence_pct),
            volatility_percentile=vol_pctile,
            trend_strength=abs(trend_strength),
            adx=current_adx,
        )

        # Regime tracking
        regime_duration = self._get_regime_duration(regime)
        previous_regime = self._get_previous_regime(regime)

        analysis = RegimeAnalysis(
            regime=regime,
            confidence=confidence,
            recommended_strategy=recommended,
            confluence_score=confluence_score,
            trend_strength=trend_strength,
            volatility_percentile=vol_pctile,
            ema_divergence_pct=ema_divergence_pct,
            atr_pct=atr_pct,
            rsi=current_rsi,
            adx=current_adx,
            bb_width_pct=current_bb_width,
            volume_ratio=current_volume_ratio,
            regime_duration_seconds=regime_duration,
            previous_regime=previous_regime,
            timestamp=datetime.now(timezone.utc),
            analysis_details={
                "current_price": current_price,
                "ema_fast": current_ema_fast,
                "ema_slow": current_ema_slow,
                "atr": current_atr,
                "bb_upper": float(bb_upper.iloc[-1]) if not pd.isna(bb_upper.iloc[-1]) else 0.0,
                "bb_middle": float(bb_middle.iloc[-1]) if not pd.isna(bb_middle.iloc[-1]) else 0.0,
                "bb_lower": float(bb_lower.iloc[-1]) if not pd.isna(bb_lower.iloc[-1]) else 0.0,
                "avg_volume": (
                    float(avg_volume.iloc[-1]) if not pd.isna(avg_volume.iloc[-1]) else 0.0
                ),
                "plus_di": float(plus_di.iloc[-1]) if not pd.isna(plus_di.iloc[-1]) else 0.0,
                "minus_di": float(minus_di.iloc[-1]) if not pd.isna(minus_di.iloc[-1]) else 0.0,
                "data_points": len(df),
            },
        )

        self._last_analysis = analysis
        self._update_regime_history(analysis)

        logger.info(
            "market_regime_detected",
            regime=regime.value,
            confidence=round(confidence, 3),
            recommended=recommended.value,
            confluence=round(confluence_score, 3),
            trend_strength=round(trend_strength, 3),
            adx=round(current_adx, 2),
            bb_width=round(current_bb_width, 2),
            volume_ratio=round(current_volume_ratio, 2),
            regime_duration=regime_duration,
        )

        return analysis

    def analyze_with_smc(self, df: pd.DataFrame, smc_context: "SMCContext") -> RegimeAnalysis:
        """
        Analyze market data and refine regime classification with SMC phase data.

        Calls analyze(df) first, then overrides the regime to ACCUMULATION or
        DISTRIBUTION when the SMCContext reports those phases (CHoCH signals).
        The overridden analysis is stored in _last_analysis and history so that
        subsequent strategy routing picks up the correct recommendation.

        Args:
            df: OHLCV DataFrame (same requirements as analyze()).
            smc_context: SMCContext returned by SMCAnalyzer.analyze().

        Returns:
            RegimeAnalysis — regime may be ACCUMULATION or DISTRIBUTION if
            SMCContext indicates a phase transition.
        """
        from bot.core.smc.models import SMCPhase

        analysis = self.analyze(df)

        smc_regime: MarketRegime | None = None
        if smc_context.phase == SMCPhase.ACCUMULATION:
            smc_regime = MarketRegime.ACCUMULATION
        elif smc_context.phase == SMCPhase.DISTRIBUTION:
            smc_regime = MarketRegime.DISTRIBUTION

        if smc_regime is None:
            return analysis

        # Blend base confidence with SMC warmup quality
        smc_confidence = analysis.confidence * 0.6 + (0.4 if smc_context.warmup_complete else 0.0)

        details = dict(analysis.analysis_details)
        details["smc_phase"] = smc_context.phase.value
        details["smc_trend_bias"] = smc_context.trend_bias
        details["smc_warmup_complete"] = smc_context.warmup_complete

        overridden = dataclasses.replace(
            analysis,
            regime=smc_regime,
            recommended_strategy=RecommendedStrategy.SMC,
            confidence=smc_confidence,
            analysis_details=details,
        )

        # Replace the entry that analyze() stored so history is consistent
        self._last_analysis = overridden
        if self._regime_history:
            self._regime_history[0] = overridden

        logger.info(
            "smc_regime_override",
            base_regime=analysis.regime.value,
            smc_regime=smc_regime.value,
            smc_phase=smc_context.phase.value,
            smc_bias=round(smc_context.trend_bias, 3),
            confidence=round(smc_confidence, 3),
        )

        return overridden

    # =========================================================================
    # Regime Classification
    # =========================================================================

    def _classify_regime(
        self,
        adx: float,
        atr_pct: float,
        ema_fast: float,
        ema_slow: float,
        current_regime: MarketRegime | None,
    ) -> MarketRegime:
        """
        Classify market regime with ADX hysteresis (v2.0).

        State-dependent thresholds prevent oscillation:
        - In TREND: ADX must drop below 25 to exit (not 32)
        - In RANGE: ADX must rise above 22 to exit (not 18)
        - In TRANSITION or None: standard thresholds (32 for trend, 18 for range)

        Args:
            adx: Current ADX value.
            atr_pct: ATR as percentage of price.
            ema_fast: Current fast EMA value.
            ema_slow: Current slow EMA value.
            current_regime: Previous regime for hysteresis (None on first call).
        """
        is_in_trend = current_regime in (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND)
        is_in_range = current_regime in (MarketRegime.TIGHT_RANGE, MarketRegime.WIDE_RANGE)

        # Determine effective ADX thresholds based on current state
        if is_in_trend:
            # Stay in trend unless ADX drops below exit threshold
            if adx >= self.adx_exit_trending:
                return self._classify_trend(ema_fast, ema_slow)
            # ADX dropped below exit — fall through to transition/range check
        elif is_in_range:
            # Stay in range unless ADX rises above exit threshold
            if adx < self.adx_exit_ranging:
                return self._classify_range(atr_pct)
            # ADX rose above exit — fall through to transition/trend check

        # Standard classification (no hysteresis state or state exited)
        if adx >= self.adx_enter_trending:
            return self._classify_trend(ema_fast, ema_slow)

        if adx < self.adx_enter_ranging:
            return self._classify_range(atr_pct)

        # Transition zone: ADX between ranging-exit (22) and trending-enter (32)
        return self._classify_transition(atr_pct)

    def _classify_trend(self, ema_fast: float, ema_slow: float) -> MarketRegime:
        """Classify trend direction by EMA crossover."""
        if ema_fast >= ema_slow:
            return MarketRegime.BULL_TREND
        return MarketRegime.BEAR_TREND

    def _classify_range(self, atr_pct: float) -> MarketRegime:
        """Classify range type by ATR volatility."""
        if atr_pct < self.atr_wide_threshold:
            return MarketRegime.TIGHT_RANGE
        return MarketRegime.WIDE_RANGE

    def _classify_transition(self, atr_pct: float) -> MarketRegime:
        """Classify transition type by ATR volatility."""
        if atr_pct < self.atr_volatile_threshold:
            return MarketRegime.QUIET_TRANSITION
        return MarketRegime.VOLATILE_TRANSITION

    # =========================================================================
    # Confluence & Confidence
    # =========================================================================

    def _calculate_confluence(
        self,
        trend_strength: float,
        rsi: float,
        volatility_percentile: float,
        ema_divergence_pct: float,
        adx: float,
        bb_width_pct: float,
        volume_ratio: float,
    ) -> float:
        """
        Calculate confluence score (0.0 - 1.0).

        Higher score = multiple indicators agree on direction.

        Weights: ADX 30%, Trend 25%, RSI 20%, Volume 15%, BB width 10%.
        """
        scores = []

        # ADX component: strong ADX = high score (ADX 20-40 mapped to 0-1)
        adx_score = min(max((adx - 20.0) / 20.0, 0.0), 1.0)
        scores.append(adx_score * 0.30)

        # Trend component: strong trend = high score
        trend_score = min(abs(trend_strength), 1.0)
        scores.append(trend_score * 0.25)

        # RSI component: extreme RSI in trend direction = higher confluence
        if trend_strength > 0:
            rsi_score = max(0.0, (rsi - 50) / 50)
        elif trend_strength < 0:
            rsi_score = max(0.0, (50 - rsi) / 50)
        else:
            rsi_score = 0.0
        scores.append(rsi_score * 0.20)

        # Volume component: above-average volume confirms the regime
        if volume_ratio > 1.5:
            volume_score = 1.0
        elif volume_ratio >= 1.0:
            volume_score = (volume_ratio - 1.0) / 0.5
        elif volume_ratio < 0.8:
            volume_score = volume_ratio / 0.8
        else:
            volume_score = 1.0
        scores.append(volume_score * 0.15)

        # BB width component: moderate width is best for confluence
        if 2.0 <= bb_width_pct <= 4.0:
            bb_score = 1.0
        elif bb_width_pct < 2.0:
            bb_score = bb_width_pct / 2.0
        else:
            bb_score = max(0.0, 1.0 - (bb_width_pct - 4.0) / 4.0)
        scores.append(bb_score * 0.10)

        return min(sum(scores), 1.0)

    def _recommend_strategy(
        self, regime: MarketRegime, confluence_score: float, adx: float
    ) -> RecommendedStrategy:
        """
        Recommend trading strategy based on regime (v2.0).

        Logic:
        - TIGHT_RANGE / WIDE_RANGE → Grid
        - BULL_TREND + High Confluence (≥threshold) → Hybrid
        - BULL_TREND + Low Confluence → DCA
        - BEAR_TREND → DCA
        - QUIET_TRANSITION → Hold
        - VOLATILE_TRANSITION → Reduce exposure
        """
        if regime in (MarketRegime.TIGHT_RANGE, MarketRegime.WIDE_RANGE):
            return RecommendedStrategy.GRID

        if regime == MarketRegime.BULL_TREND:
            if confluence_score >= self.confluence_threshold:
                return RecommendedStrategy.HYBRID
            return RecommendedStrategy.DCA

        if regime == MarketRegime.BEAR_TREND:
            return RecommendedStrategy.DCA

        if regime in (MarketRegime.ACCUMULATION, MarketRegime.DISTRIBUTION):
            return RecommendedStrategy.SMC

        if regime == MarketRegime.QUIET_TRANSITION:
            return RecommendedStrategy.HOLD

        if regime == MarketRegime.VOLATILE_TRANSITION:
            return RecommendedStrategy.REDUCE_EXPOSURE

        return RecommendedStrategy.HOLD

    def _calculate_confidence(
        self,
        regime: MarketRegime,
        ema_divergence_pct: float,
        volatility_percentile: float,
        trend_strength: float,
        adx: float,
    ) -> float:
        """
        Calculate confidence in the regime classification (0.0 - 1.0).

        ADX strengthens confidence when aligned with the detected regime.
        """
        if regime == MarketRegime.UNKNOWN:
            return 0.0

        if regime in (MarketRegime.TIGHT_RANGE, MarketRegime.WIDE_RANGE):
            ema_conf = max(0.3, 1.0 - ema_divergence_pct / self.trend_threshold)
            adx_conf = max(0.0, 1.0 - adx / 25.0)
            return min(ema_conf * 0.6 + adx_conf * 0.4, 1.0)

        if regime in (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND):
            trend_conf = min(0.5 + trend_strength * 0.5, 1.0)
            adx_conf = min(adx / 50.0, 1.0)
            return min(trend_conf * 0.6 + adx_conf * 0.4, 1.0)

        if regime in (MarketRegime.ACCUMULATION, MarketRegime.DISTRIBUTION):
            # Moderate confidence — SMC phase detected but may still evolve
            adx_conf = min(adx / 40.0, 1.0)
            return min(0.4 + trend_strength * 0.4 + adx_conf * 0.2, 1.0)

        if regime == MarketRegime.VOLATILE_TRANSITION:
            return min(volatility_percentile / 100.0, 1.0)

        return 0.5  # QUIET_TRANSITION

    # =========================================================================
    # Regime History Tracking
    # =========================================================================

    def _update_regime_history(self, analysis: RegimeAnalysis) -> None:
        """Add analysis to history, maintaining max size."""
        self._regime_history.insert(0, analysis)
        if len(self._regime_history) > self.regime_history_size:
            self._regime_history = self._regime_history[: self.regime_history_size]

    def _get_regime_duration(self, current_regime: MarketRegime) -> int:
        """
        Calculate how long the current regime has persisted (seconds).

        Looks back through history to find when this regime started.
        """
        if not self._regime_history:
            return 0

        now = datetime.now(timezone.utc)
        regime_start = now

        for past_analysis in self._regime_history:
            if past_analysis.regime == current_regime:
                regime_start = past_analysis.timestamp
            else:
                break

        return int((now - regime_start).total_seconds())

    def _get_previous_regime(self, current_regime: MarketRegime) -> MarketRegime | None:
        """Get the last regime that differs from the current one."""
        if not self._regime_history:
            return None

        for past in self._regime_history:
            if past.regime != current_regime:
                return past.regime

        return None

    # =========================================================================
    # Technical Indicator Calculations
    # =========================================================================

    @staticmethod
    def _calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
        """Calculate Average True Range."""
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return true_range.rolling(window=period).mean()

    @staticmethod
    def _calculate_rsi(close: pd.Series, period: int) -> pd.Series:
        """Calculate Relative Strength Index."""
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.inf)
        rsi = 100 - (100 / (1 + rs))
        return rsi

    @staticmethod
    def _calculate_adx(
        high: pd.Series, low: pd.Series, close: pd.Series, period: int
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        Calculate Average Directional Index.

        ADX measures trend strength (0-100): >25 = strong trend, <20 = weak/no trend.

        Returns:
            Tuple of (adx, plus_di, minus_di) as pd.Series.
        """
        # Directional Movement
        high_diff = high.diff()
        low_diff = -low.diff()

        plus_dm = high_diff.where((high_diff > low_diff) & (high_diff > 0), 0.0)
        minus_dm = low_diff.where((low_diff > high_diff) & (low_diff > 0), 0.0)

        # True Range
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Wilder's smoothing (exponential with alpha=1/period)
        alpha = 1.0 / period
        atr_smooth = true_range.ewm(alpha=alpha, adjust=False).mean()
        plus_dm_smooth = plus_dm.ewm(alpha=alpha, adjust=False).mean()
        minus_dm_smooth = minus_dm.ewm(alpha=alpha, adjust=False).mean()

        # Directional Indicators
        plus_di = 100 * (plus_dm_smooth / atr_smooth.replace(0, np.inf))
        minus_di = 100 * (minus_dm_smooth / atr_smooth.replace(0, np.inf))

        # DX and ADX
        di_diff = (plus_di - minus_di).abs()
        di_sum = plus_di + minus_di
        dx = 100 * (di_diff / di_sum.replace(0, np.inf))
        adx = dx.ewm(alpha=alpha, adjust=False).mean()

        return adx, plus_di, minus_di

    @staticmethod
    def _calculate_bollinger_bands(
        close: pd.Series, period: int = 20, std_dev: float = 2.0
    ) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        """
        Calculate Bollinger Bands and width percentage.

        Returns:
            Tuple of (bb_upper, bb_middle, bb_lower, bb_width_pct).
        """
        bb_middle = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()

        bb_upper = bb_middle + (std * std_dev)
        bb_lower = bb_middle - (std * std_dev)

        # Width as percentage of middle band
        bb_width_pct = ((bb_upper - bb_lower) / bb_middle.replace(0, np.inf)) * 100

        return bb_upper, bb_middle, bb_lower, bb_width_pct

    @staticmethod
    def _calculate_volume_ratio(
        volume: pd.Series, lookback: int = 20
    ) -> tuple[pd.Series, pd.Series]:
        """
        Calculate volume ratio (current / average).

        Returns:
            Tuple of (avg_volume, volume_ratio).
        """
        avg_volume = volume.rolling(window=lookback).mean()
        volume_ratio = volume / avg_volume.replace(0, np.inf)
        return avg_volume, volume_ratio

    # =========================================================================
    # Error Handling
    # =========================================================================

    def _unknown_analysis(self, reason: str) -> RegimeAnalysis:
        """Return unknown analysis for error cases."""
        return RegimeAnalysis(
            regime=MarketRegime.UNKNOWN,
            confidence=0.0,
            recommended_strategy=RecommendedStrategy.HOLD,
            confluence_score=0.0,
            trend_strength=0.0,
            volatility_percentile=0.0,
            ema_divergence_pct=0.0,
            atr_pct=0.0,
            rsi=50.0,
            adx=20.0,
            bb_width_pct=4.0,
            volume_ratio=1.0,
            regime_duration_seconds=0,
            previous_regime=None,
            timestamp=datetime.now(timezone.utc),
            analysis_details={"reason": reason},
        )
