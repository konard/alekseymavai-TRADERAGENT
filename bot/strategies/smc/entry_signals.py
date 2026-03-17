"""
Entry Signal Generator Module

Detects price action patterns for precise entry signals:
- Engulfing patterns (bullish & bearish)
- Pin Bars (Hammer & Shooting Star)
- Inside Bars (consolidation patterns)
- Confluence checking with Order Blocks and Fair Value Gaps
- Signal generation with entry, SL, TP levels
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from bot.strategies.smc.confluence_zones import ConfluenceZoneAnalyzer
from bot.strategies.smc.market_structure import MarketStructureAnalyzer, TrendDirection
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class PatternType(str, Enum):
    """Type of price action pattern"""

    ENGULFING = "engulfing"
    PIN_BAR = "pin_bar"
    INSIDE_BAR = "inside_bar"


class SignalDirection(str, Enum):
    """Trade signal direction"""

    LONG = "long"
    SHORT = "short"


@dataclass
class PriceActionPattern:
    """
    Represents a detected price action pattern
    """

    pattern_type: PatternType
    is_bullish: bool

    # Candle info
    index: int
    timestamp: pd.Timestamp
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    # Pattern quality
    quality_score: float = 0.0  # 0-100
    confidence: float = 0.0  # 0-1.0

    # Context
    previous_candle: Optional[dict] = None
    volume: float = 0.0

    def __repr__(self) -> str:
        direction = "Bullish" if self.is_bullish else "Bearish"
        return f"{direction} {self.pattern_type.value} (quality={self.quality_score:.1f}, confidence={self.confidence:.2f})"


@dataclass
class SMCSignal:
    """
    Complete trading signal with entry, SL, TP
    """

    timestamp: pd.Timestamp
    direction: SignalDirection

    # Price levels
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal

    # Signal info
    pattern: PriceActionPattern
    confidence: float  # 0-1.0
    risk_reward_ratio: float

    # Confluence
    confluence_zones: list[str] = field(default_factory=list)
    confluence_score: float = 0.0

    # Market context
    trend_direction: TrendDirection = TrendDirection.RANGING
    trend_aligned: bool = False

    # Structural event fields (populated by detect_level_break)
    event_type: str = "entry"  # "entry" | "support_break" | "resistance_break"
    broken_level: Optional[Decimal] = None  # The broken support/resistance level
    break_extreme: Optional[Decimal] = None  # break_low / break_high (base for +1% trigger)
    next_level: Optional[Decimal] = None  # Next SMC level (target for surviving positions)
    structure_confidence: float = 0.0  # Confidence 0.0–1.0

    def get_risk_amount(self) -> Decimal:
        """Calculate risk amount (entry - SL)"""
        return abs(self.entry_price - self.stop_loss)

    def get_reward_amount(self) -> Decimal:
        """Calculate reward amount (TP - entry)"""
        return abs(self.take_profit - self.entry_price)

    def __repr__(self) -> str:
        return (
            f"SMCSignal({self.direction.value.upper()}, "
            f"entry={float(self.entry_price):.2f}, "
            f"SL={float(self.stop_loss):.2f}, "
            f"TP={float(self.take_profit):.2f}, "
            f"RR={self.risk_reward_ratio:.1f}, "
            f"confidence={self.confidence:.2f})"
        )


class EntrySignalGenerator:
    """
    Generates trading signals based on price action patterns
    """

    def __init__(
        self,
        market_structure: MarketStructureAnalyzer,
        confluence_analyzer: ConfluenceZoneAnalyzer,
        min_risk_reward: float = 2.5,
        sl_buffer_pct: float = 0.5,
    ):
        """
        Initialize Entry Signal Generator

        Args:
            market_structure: MarketStructureAnalyzer instance
            confluence_analyzer: ConfluenceZoneAnalyzer instance
            min_risk_reward: Minimum risk:reward ratio for signals
            sl_buffer_pct: Stop loss buffer percentage
        """
        self.market_structure = market_structure
        self.confluence_analyzer = confluence_analyzer
        self.min_risk_reward = min_risk_reward
        self.sl_buffer_pct = sl_buffer_pct

        self.detected_patterns: list[PriceActionPattern] = []
        self.generated_signals: list[SMCSignal] = []

        # Log-spam suppression
        self._insufficient_data_count: int = 0

        logger.info(
            "EntrySignalGenerator initialized", min_rr=min_risk_reward, sl_buffer=sl_buffer_pct
        )

    def analyze(self, df: pd.DataFrame) -> list[SMCSignal]:
        """
        Analyze price data and generate trading signals

        Args:
            df: DataFrame with OHLCV data

        Returns:
            List of SMCSignal objects
        """
        if len(df) < 2:
            self._insufficient_data_count += 1
            if self._insufficient_data_count == 1:
                logger.warning("Insufficient data for pattern detection")
            return []

        self.detected_patterns.clear()
        self.generated_signals.clear()

        # Detect all patterns
        self._detect_engulfing_patterns(df)
        self._detect_pin_bar_patterns(df)
        self._detect_inside_bar_patterns(df)

        # Generate signals from patterns
        for pattern in self.detected_patterns:
            signal = self._generate_signal_from_pattern(df, pattern)
            if signal and signal.confidence >= 0.5:  # Min confidence threshold
                self.generated_signals.append(signal)

        logger.info(
            "Entry signals generated",
            patterns_detected=len(self.detected_patterns),
            signals_generated=len(self.generated_signals),
        )

        return self.generated_signals

    def _detect_engulfing_patterns(self, df: pd.DataFrame) -> None:
        """
        Detect Engulfing patterns (bullish and bearish)

        Engulfing: Current candle body fully engulfs previous candle body
        """
        for i in range(1, len(df)):
            curr = df.iloc[i]
            prev = df.iloc[i - 1]

            curr_body_size = abs(curr["close"] - curr["open"])
            prev_body_size = abs(prev["close"] - prev["open"])
            curr_range = curr["high"] - curr["low"]

            # Skip if body is too small (doji-like)
            if curr_body_size < curr_range * 0.3:
                continue

            # Bullish Engulfing
            if (
                prev["close"] < prev["open"]
                and curr["close"] > curr["open"]  # Previous bearish
                and curr["open"] <= prev["close"]  # Current bullish
                and curr["close"] >= prev["open"]  # Opens at/below prev close
            ):  # Closes at/above prev open
                quality = self._calculate_engulfing_quality(curr, prev, True)

                if quality >= 50:  # Min quality threshold
                    pattern = PriceActionPattern(
                        pattern_type=PatternType.ENGULFING,
                        is_bullish=True,
                        index=i,
                        timestamp=curr.name,
                        open=Decimal(str(curr["open"])),
                        high=Decimal(str(curr["high"])),
                        low=Decimal(str(curr["low"])),
                        close=Decimal(str(curr["close"])),
                        quality_score=quality,
                        previous_candle={
                            "open": prev["open"],
                            "high": prev["high"],
                            "low": prev["low"],
                            "close": prev["close"],
                        },
                        volume=float(curr["volume"]) if "volume" in curr else 0.0,
                    )
                    self.detected_patterns.append(pattern)

                    logger.debug(f"Bullish Engulfing detected at index {i}, quality={quality:.1f}")

            # Bearish Engulfing
            elif (
                prev["close"] > prev["open"]
                and curr["close"] < curr["open"]  # Previous bullish
                and curr["open"] >= prev["close"]  # Current bearish
                and curr["close"] <= prev["open"]  # Opens at/above prev close
            ):  # Closes at/below prev open
                quality = self._calculate_engulfing_quality(curr, prev, False)

                if quality >= 50:
                    pattern = PriceActionPattern(
                        pattern_type=PatternType.ENGULFING,
                        is_bullish=False,
                        index=i,
                        timestamp=curr.name,
                        open=Decimal(str(curr["open"])),
                        high=Decimal(str(curr["high"])),
                        low=Decimal(str(curr["low"])),
                        close=Decimal(str(curr["close"])),
                        quality_score=quality,
                        previous_candle={
                            "open": prev["open"],
                            "high": prev["high"],
                            "low": prev["low"],
                            "close": prev["close"],
                        },
                        volume=float(curr["volume"]) if "volume" in curr else 0.0,
                    )
                    self.detected_patterns.append(pattern)

                    logger.debug(f"Bearish Engulfing detected at index {i}, quality={quality:.1f}")

    def _calculate_engulfing_quality(self, curr, prev, is_bullish: bool) -> float:
        """
        Calculate quality score for engulfing pattern (0-100)

        Factors:
        - Body size ratio (0-40)
        - Body dominance in candle (0-30)
        - Volume (0-30)
        """
        score = 0.0

        curr_body = abs(curr["close"] - curr["open"])
        prev_body = abs(prev["close"] - prev["open"])
        curr_range = curr["high"] - curr["low"]

        # Body size ratio (bigger engulfing = better)
        if prev_body > 0:
            body_ratio = min(curr_body / prev_body, 3.0)  # Cap at 3x
            score += (body_ratio / 3.0) * 40

        # Body dominance (body should be large part of range)
        if curr_range > 0:
            body_dominance = curr_body / curr_range
            score += body_dominance * 30

        # Volume (higher volume = more conviction)
        if "volume" in curr and "volume" in prev:
            if prev["volume"] > 0:
                volume_ratio = min(curr["volume"] / prev["volume"], 2.0)
                score += (volume_ratio / 2.0) * 30

        return min(100.0, score)

    def _detect_pin_bar_patterns(self, df: pd.DataFrame) -> None:
        """
        Detect Pin Bar patterns (Hammer and Shooting Star)

        Pin Bar: Long wick with small body
        - Bullish: Long lower wick (rejection of lows)
        - Bearish: Long upper wick (rejection of highs)
        """
        for i in range(len(df)):
            candle = df.iloc[i]

            candle_range = candle["high"] - candle["low"]
            if candle_range == 0:
                continue

            body_size = abs(candle["close"] - candle["open"])
            upper_wick = candle["high"] - max(candle["open"], candle["close"])
            lower_wick = min(candle["open"], candle["close"]) - candle["low"]

            # Bullish Pin Bar (Hammer)
            if (
                lower_wick > candle_range * 0.6
                and body_size < candle_range * 0.4  # Long lower wick
                and upper_wick < candle_range * 0.2  # Small body
            ):  # Small upper wick
                quality = self._calculate_pin_bar_quality(candle, True)

                if quality >= 50:
                    pattern = PriceActionPattern(
                        pattern_type=PatternType.PIN_BAR,
                        is_bullish=True,
                        index=i,
                        timestamp=candle.name,
                        open=Decimal(str(candle["open"])),
                        high=Decimal(str(candle["high"])),
                        low=Decimal(str(candle["low"])),
                        close=Decimal(str(candle["close"])),
                        quality_score=quality,
                        volume=float(candle["volume"]) if "volume" in candle else 0.0,
                    )
                    self.detected_patterns.append(pattern)

                    logger.debug(f"Bullish Pin Bar detected at index {i}, quality={quality:.1f}")

            # Bearish Pin Bar (Shooting Star)
            elif (
                upper_wick > candle_range * 0.6
                and body_size < candle_range * 0.4  # Long upper wick
                and lower_wick < candle_range * 0.2  # Small body
            ):  # Small lower wick
                quality = self._calculate_pin_bar_quality(candle, False)

                if quality >= 50:
                    pattern = PriceActionPattern(
                        pattern_type=PatternType.PIN_BAR,
                        is_bullish=False,
                        index=i,
                        timestamp=candle.name,
                        open=Decimal(str(candle["open"])),
                        high=Decimal(str(candle["high"])),
                        low=Decimal(str(candle["low"])),
                        close=Decimal(str(candle["close"])),
                        quality_score=quality,
                        volume=float(candle["volume"]) if "volume" in candle else 0.0,
                    )
                    self.detected_patterns.append(pattern)

                    logger.debug(f"Bearish Pin Bar detected at index {i}, quality={quality:.1f}")

    def _calculate_pin_bar_quality(self, candle, is_bullish: bool) -> float:
        """Calculate quality score for pin bar (0-100)"""
        score = 0.0

        candle_range = candle["high"] - candle["low"]
        if candle_range == 0:
            return 0.0

        body_size = abs(candle["close"] - candle["open"])
        upper_wick = candle["high"] - max(candle["open"], candle["close"])
        lower_wick = min(candle["open"], candle["close"]) - candle["low"]

        if is_bullish:
            wick = lower_wick
        else:
            wick = upper_wick

        # Wick to body ratio (longer wick relative to body = better)
        if body_size > 0:
            wick_body_ratio = min(wick / body_size, 5.0)
            score += (wick_body_ratio / 5.0) * 40
        else:
            score += 40  # Perfect: no body

        # Wick to range ratio (wick should dominate)
        wick_dominance = wick / candle_range
        score += wick_dominance * 40

        # Body position (should be at opposite end)
        body_center = (
            max(candle["open"], candle["close"]) + min(candle["open"], candle["close"])
        ) / 2
        if is_bullish:
            # Body should be near high
            position_score = (body_center - candle["low"]) / candle_range
        else:
            # Body should be near low
            position_score = (candle["high"] - body_center) / candle_range
        score += position_score * 20

        return min(100.0, score)

    def _detect_inside_bar_patterns(self, df: pd.DataFrame) -> None:
        """
        Detect Inside Bar patterns

        Inside Bar: Current candle contained within previous candle range
        """
        for i in range(1, len(df)):
            curr = df.iloc[i]
            prev = df.iloc[i - 1]

            # Inside bar condition
            if curr["high"] <= prev["high"] and curr["low"] >= prev["low"]:
                # Determine bias based on close relative to previous candle
                is_bullish = curr["close"] > (prev["high"] + prev["low"]) / 2

                quality = self._calculate_inside_bar_quality(curr, prev)

                if quality >= 50:
                    pattern = PriceActionPattern(
                        pattern_type=PatternType.INSIDE_BAR,
                        is_bullish=is_bullish,
                        index=i,
                        timestamp=curr.name,
                        open=Decimal(str(curr["open"])),
                        high=Decimal(str(curr["high"])),
                        low=Decimal(str(curr["low"])),
                        close=Decimal(str(curr["close"])),
                        quality_score=quality,
                        previous_candle={
                            "open": prev["open"],
                            "high": prev["high"],
                            "low": prev["low"],
                            "close": prev["close"],
                        },
                        volume=float(curr["volume"]) if "volume" in curr else 0.0,
                    )
                    self.detected_patterns.append(pattern)

                    direction = "Bullish" if is_bullish else "Bearish"
                    logger.debug(
                        f"{direction} Inside Bar detected at index {i}, quality={quality:.1f}"
                    )

    def _calculate_inside_bar_quality(self, curr, prev) -> float:
        """Calculate quality score for inside bar (0-100)"""
        score = 50.0  # Base score for valid inside bar

        prev_range = prev["high"] - prev["low"]
        curr_range = curr["high"] - curr["low"]

        if prev_range == 0:
            return 0.0

        # Compression ratio (smaller inside bar = better compression)
        compression = 1.0 - (curr_range / prev_range)
        score += compression * 30

        # Previous candle size (larger mother bar = better)
        if prev_range > 0:
            # Normalize by typical range (assume 1.0 is typical)
            size_score = min(prev_range / 1.0, 2.0) / 2.0
            score += size_score * 20

        return min(100.0, score)

    def _generate_signal_from_pattern(
        self, df: pd.DataFrame, pattern: PriceActionPattern
    ) -> Optional[SMCSignal]:
        """
        Generate trading signal from detected pattern

        Args:
            df: DataFrame with OHLCV data
            pattern: Detected PriceActionPattern

        Returns:
            SMCSignal or None
        """
        # Calculate entry, SL, TP
        entry, sl, tp = self._calculate_entry_sl_tp(pattern)

        if not entry or not sl or not tp:
            return None

        # Calculate risk:reward ratio
        risk = abs(entry - sl)
        reward = abs(tp - entry)

        if risk == 0:
            return None

        rr_ratio = float(reward / risk)

        # Check minimum RR
        if rr_ratio < self.min_risk_reward:
            return None

        # Check confluence
        confluence_score, confluence_zones = self._check_confluence(pattern)

        # Calculate confidence
        confidence = self._calculate_signal_confidence(pattern, confluence_score, rr_ratio)

        # Determine direction
        direction = SignalDirection.LONG if pattern.is_bullish else SignalDirection.SHORT

        # Check trend alignment
        trend_aligned = self._check_trend_alignment(pattern)

        signal = SMCSignal(
            timestamp=pattern.timestamp,
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            pattern=pattern,
            confidence=confidence,
            risk_reward_ratio=rr_ratio,
            confluence_zones=confluence_zones,
            confluence_score=confluence_score,
            trend_direction=self.market_structure.current_trend,
            trend_aligned=trend_aligned,
        )

        logger.info(f"Signal generated: {signal}")

        return signal

    def _find_liquidity_tp(self, entry_price: Decimal, is_long: bool) -> Optional[Decimal]:
        """
        Find nearest un-swept liquidity level for TP targeting.

        For longs: find buy-side liquidity above entry.
        For shorts: find sell-side liquidity below entry.

        Returns:
            Liquidity level price or None
        """
        if not hasattr(self.confluence_analyzer, "get_active_liquidity_zones"):
            return None

        if is_long:
            # Buy-side liquidity above entry
            zones = self.confluence_analyzer.get_active_liquidity_zones(is_bullish=True)
            candidates = [lz for lz in zones if lz.level > entry_price]
            if candidates:
                # Nearest one above
                candidates.sort(key=lambda lz: lz.level)
                return candidates[0].level
        else:
            # Sell-side liquidity below entry
            zones = self.confluence_analyzer.get_active_liquidity_zones(is_bullish=False)
            candidates = [lz for lz in zones if lz.level < entry_price]
            if candidates:
                # Nearest one below
                candidates.sort(key=lambda lz: lz.level, reverse=True)
                return candidates[0].level

        return None

    def _calculate_entry_sl_tp(
        self, pattern: PriceActionPattern
    ) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
        """
        Calculate entry, stop loss, and take profit levels.

        After calculating standard RR-based TP, checks for nearby liquidity
        zones and uses them as TP if they provide >= min_risk_reward RR.

        Returns:
            Tuple of (entry, sl, tp) or (None, None, None)
        """
        entry = pattern.close
        buffer = Decimal(str(self.sl_buffer_pct / 100))

        if pattern.is_bullish:
            # Long setup
            sl_base = pattern.low
            sl = sl_base * (Decimal("1") - buffer)

            risk = entry - sl
            tp = entry + (risk * Decimal(str(self.min_risk_reward)))
        else:
            # Short setup
            sl_base = pattern.high
            sl = sl_base * (Decimal("1") + buffer)

            risk = sl - entry
            tp = entry - (risk * Decimal(str(self.min_risk_reward)))

        # Check for liquidity-based TP
        liq_tp = self._find_liquidity_tp(entry, is_long=pattern.is_bullish)
        if liq_tp is not None and risk > 0:
            liq_reward = abs(liq_tp - entry)
            liq_rr = float(liq_reward / risk)
            if liq_rr >= self.min_risk_reward:
                tp = liq_tp

        return entry, sl, tp

    def _check_confluence(self, pattern: PriceActionPattern) -> tuple[float, list[str]]:
        """
        Check for confluence with Order Blocks and Fair Value Gaps

        Returns:
            Tuple of (confluence_score, list of zone names)
        """
        confluence_score = 0.0
        zones = []

        # Get zones near pattern price
        result = self.confluence_analyzer.find_confluence_at_price(
            pattern.close, tolerance=Decimal("1.0")
        )

        # Check Order Blocks
        for ob in result.get("order_blocks", []):
            if ob.is_bullish == pattern.is_bullish:  # Aligned direction
                confluence_score += ob.strength_score * 0.5
                zones.append(f"OB_{ob.timeframe}")

        # Check Fair Value Gaps
        for fvg in result.get("fair_value_gaps", []):
            if fvg.is_bullish == pattern.is_bullish:  # Aligned direction
                confluence_score += fvg.strength_score * 0.5
                zones.append(f"FVG_{fvg.timeframe}")

        # Check Liquidity proximity (within 1% of price)
        if hasattr(self.confluence_analyzer, "get_active_liquidity_zones"):
            price_float = float(pattern.close)
            tolerance = price_float * 0.01
            for lz in self.confluence_analyzer.get_active_liquidity_zones():
                lz_level = float(lz.level)
                if abs(lz_level - price_float) <= tolerance:
                    confluence_score += 15
                    zones.append(f"LIQ_{lz.timeframe}")

        # Normalize score to 0-100
        confluence_score = min(100.0, confluence_score)

        return confluence_score, zones

    def _calculate_signal_confidence(
        self, pattern: PriceActionPattern, confluence_score: float, rr_ratio: float
    ) -> float:
        """
        Calculate overall signal confidence (0-1.0)

        Factors:
        - Pattern quality (40%)
        - Confluence (30%)
        - Trend alignment (20%)
        - Risk:Reward (10%)
        """
        # Pattern quality contribution
        pattern_confidence = (pattern.quality_score / 100.0) * 0.4

        # Confluence contribution
        confluence_confidence = (confluence_score / 100.0) * 0.3

        # Trend alignment contribution
        trend_aligned = self._check_trend_alignment(pattern)
        trend_confidence = 0.2 if trend_aligned else 0.05

        # RR ratio contribution (bonus for high RR)
        rr_confidence = min(rr_ratio / 5.0, 1.0) * 0.1

        total_confidence = (
            pattern_confidence + confluence_confidence + trend_confidence + rr_confidence
        )

        return min(1.0, total_confidence)

    def _check_trend_alignment(self, pattern: PriceActionPattern) -> bool:
        """Check if pattern aligns with current trend"""
        if self.market_structure.current_trend == TrendDirection.BULLISH:
            return pattern.is_bullish
        elif self.market_structure.current_trend == TrendDirection.BEARISH:
            return not pattern.is_bullish
        else:
            return False  # Ranging market

    def get_latest_signals(self, limit: int = 5) -> list[SMCSignal]:
        """Get most recent signals"""
        return self.generated_signals[-limit:] if self.generated_signals else []

    def get_signals_summary(self) -> dict:
        """Get summary of signal generation"""
        return {
            "total_patterns": len(self.detected_patterns),
            "total_signals": len(self.generated_signals),
            "engulfing": len(
                [p for p in self.detected_patterns if p.pattern_type == PatternType.ENGULFING]
            ),
            "pin_bar": len(
                [p for p in self.detected_patterns if p.pattern_type == PatternType.PIN_BAR]
            ),
            "inside_bar": len(
                [p for p in self.detected_patterns if p.pattern_type == PatternType.INSIDE_BAR]
            ),
            "long_signals": len(
                [s for s in self.generated_signals if s.direction == SignalDirection.LONG]
            ),
            "short_signals": len(
                [s for s in self.generated_signals if s.direction == SignalDirection.SHORT]
            ),
            "avg_confidence": (
                np.mean([s.confidence for s in self.generated_signals])
                if self.generated_signals
                else 0.0
            ),
            "avg_rr_ratio": (
                np.mean([s.risk_reward_ratio for s in self.generated_signals])
                if self.generated_signals
                else 0.0
            ),
        }
