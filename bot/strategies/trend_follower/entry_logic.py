"""
Entry Logic Module

Implements entry conditions for LONG and SHORT positions:
- Trend-based entry (pullback to EMA, bounce from support/resistance)
- Sideways entry (RSI oversold/overbought, range breakout)
- Volume confirmation
- ATR filter to avoid high volatility
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

import pandas as pd

from bot.strategies.trend_follower.market_analyzer import (
    MarketAnalyzer,
    MarketConditions,
    MarketPhase,
)
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class SignalType(str, Enum):
    """Trading signal type"""

    LONG = "long"
    SHORT = "short"
    NONE = "none"


class EntryReason(str, Enum):
    """Reason for entry signal"""

    # Trend scenarios
    TREND_PULLBACK_TO_EMA = "trend_pullback_to_ema"
    TREND_RETURN_FROM_ABOVE = "trend_return_from_above"  # A+: price returns into EMA corridor from above
    TREND_BOUNCE_FROM_SUPPORT = "trend_bounce_from_support"
    TREND_BOUNCE_FROM_RESISTANCE = "trend_bounce_from_resistance"

    # Sideways scenarios
    SIDEWAYS_RSI_OVERSOLD = "sideways_rsi_oversold"
    SIDEWAYS_RSI_OVERBOUGHT = "sideways_rsi_overbought"
    SIDEWAYS_RANGE_BREAKOUT_UP = "sideways_range_breakout_up"
    SIDEWAYS_RANGE_BREAKOUT_DOWN = "sideways_range_breakout_down"


@dataclass
class EntrySignal:
    """Entry signal with all relevant information"""

    signal_type: SignalType
    entry_reason: EntryReason
    entry_price: Decimal
    confidence: Decimal  # 0.0 to 1.0
    market_conditions: MarketConditions
    volume_confirmed: bool
    timestamp: pd.Timestamp


@dataclass
class SupportResistanceLevel:
    """Support or Resistance level"""

    price: Decimal
    is_support: bool  # True for support, False for resistance
    touches: int  # Number of times price touched this level
    strength: Decimal  # 0.0 to 1.0


class EntryLogicAnalyzer:
    """
    Analyzes entry conditions for LONG and SHORT positions

    Implements entry logic from Issue #124:
    - For LONG positions:
        * Main scenario (trend): Wait for pullback to EMA(20) or support zone.
          Enter on bounce with volume confirmation.
        * Sideways scenario: Enter when RSI exits oversold (<30) or range breakout
          with increased volume.
    - For SHORT positions: Inverse logic.
    - Filter: Don't open trades if ATR > 5% of current price.
    """

    def __init__(
        self,
        market_analyzer: MarketAnalyzer,
        require_volume_confirmation: bool = True,
        volume_multiplier: Decimal = Decimal("1.5"),
        volume_lookback: int = 20,
        max_atr_filter_pct: Decimal = Decimal("0.05"),
        support_resistance_lookback: int = 50,
        support_resistance_threshold: Decimal = Decimal("0.01"),
        min_sr_touches: int = 1,
        max_distance_from_ema_pct: Decimal = Decimal("0.03"),
        rsi_oversold: Decimal = Decimal("30"),
        rsi_overbought: Decimal = Decimal("70"),
    ):
        """
        Initialize Entry Logic Analyzer

        Args:
            market_analyzer: MarketAnalyzer instance
            require_volume_confirmation: Whether to require volume confirmation
            volume_multiplier: Required volume multiplier (e.g., 1.5x average)
            volume_lookback: Periods for average volume calculation
            max_atr_filter_pct: Max ATR as % of price (filter)
            support_resistance_lookback: Candles for S/R identification
            support_resistance_threshold: % threshold for S/R zones
            min_sr_touches: Min price touches to validate S/R level (default 1 for M5)
            max_distance_from_ema_pct: Max distance from EMA for trend entry (default 3%)
            rsi_oversold: RSI oversold threshold
            rsi_overbought: RSI overbought threshold
        """
        self.market_analyzer = market_analyzer
        self.require_volume_confirmation = require_volume_confirmation
        self.volume_multiplier = volume_multiplier
        self.volume_lookback = volume_lookback
        self.max_atr_filter_pct = max_atr_filter_pct
        self.support_resistance_lookback = support_resistance_lookback
        self.support_resistance_threshold = support_resistance_threshold
        self.min_sr_touches = min_sr_touches
        self.max_distance_from_ema_pct = max_distance_from_ema_pct
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought

        logger.info(
            "EntryLogicAnalyzer initialized",
            volume_confirmation=require_volume_confirmation,
            volume_multiplier=float(volume_multiplier),
            max_atr_filter=float(max_atr_filter_pct),
        )

    def analyze_entry(self, df: pd.DataFrame) -> Optional[EntrySignal]:
        """
        Analyze entry conditions on given dataframe

        Args:
            df: DataFrame with OHLCV data

        Returns:
            EntrySignal if valid entry found, None otherwise
        """
        # Get market conditions
        market_conditions = self.market_analyzer.analyze(df)

        # Apply ATR filter (don't trade if volatility too high)
        if market_conditions.atr_pct > self.max_atr_filter_pct:
            logger.debug(
                "ATR filter triggered - volatility too high",
                atr_pct=float(market_conditions.atr_pct),
                max_atr=float(self.max_atr_filter_pct),
            )
            return None

        # Check volume confirmation if required
        volume_confirmed = True
        if self.require_volume_confirmation:
            volume_confirmed = self._check_volume_confirmation(df)
            if not volume_confirmed:
                logger.debug("Volume confirmation failed")
                return None

        # Find support/resistance levels
        sr_levels = self._find_support_resistance_levels(df)

        # Analyze entry based on market phase
        if market_conditions.phase == MarketPhase.BULLISH_TREND:
            signal = self._analyze_bullish_trend_entry(
                df, market_conditions, sr_levels, volume_confirmed
            )
        elif market_conditions.phase == MarketPhase.BEARISH_TREND:
            signal = self._analyze_bearish_trend_entry(
                df, market_conditions, sr_levels, volume_confirmed
            )
        elif market_conditions.phase == MarketPhase.SIDEWAYS:
            signal = self._analyze_sideways_entry(df, market_conditions, volume_confirmed)
        else:
            logger.debug("Market phase unknown - no entry signal")
            return None

        if signal:
            logger.info(
                "Entry signal generated",
                type=signal.signal_type,
                reason=signal.entry_reason,
                price=float(signal.entry_price),
                confidence=float(signal.confidence),
            )

        return signal

    def _check_volume_confirmation(self, df: pd.DataFrame) -> bool:
        """Check if current volume is above average"""
        avg_volume = df["volume"].tail(self.volume_lookback).mean()
        current_volume = df["volume"].iloc[-1]

        return bool(current_volume >= (avg_volume * float(self.volume_multiplier)))

    def _find_support_resistance_levels(self, df: pd.DataFrame) -> list[SupportResistanceLevel]:
        """
        Identify support and resistance levels

        Uses local highs and lows from recent price action
        """
        levels: list[SupportResistanceLevel] = []
        lookback_data = df.tail(self.support_resistance_lookback)

        # Find local highs (potential resistance)
        for i in range(2, len(lookback_data) - 2):
            high = lookback_data["high"].iloc[i]
            if (
                high > lookback_data["high"].iloc[i - 2 : i].max()
                and high > lookback_data["high"].iloc[i + 1 : i + 3].max()
            ):
                # Count touches near this level
                touches = self._count_touches(df, Decimal(str(high)), is_high=True)
                if touches >= self.min_sr_touches:
                    levels.append(
                        SupportResistanceLevel(
                            price=Decimal(str(high)),
                            is_support=False,
                            touches=touches,
                            strength=Decimal(str(min(touches / 5.0, 1.0))),  # Normalize to 0-1
                        )
                    )

        # Find local lows (potential support)
        for i in range(2, len(lookback_data) - 2):
            low = lookback_data["low"].iloc[i]
            if (
                low < lookback_data["low"].iloc[i - 2 : i].min()
                and low < lookback_data["low"].iloc[i + 1 : i + 3].min()
            ):
                touches = self._count_touches(df, Decimal(str(low)), is_high=False)
                if touches >= self.min_sr_touches:
                    levels.append(
                        SupportResistanceLevel(
                            price=Decimal(str(low)),
                            is_support=True,
                            touches=touches,
                            strength=Decimal(str(min(touches / 5.0, 1.0))),
                        )
                    )

        return levels

    def _count_touches(self, df: pd.DataFrame, level: Decimal, is_high: bool) -> int:
        """Count how many times price touched a level"""
        threshold = level * self.support_resistance_threshold
        touches = 0

        price_series = df["high"] if is_high else df["low"]
        for price in price_series:
            if abs(Decimal(str(price)) - level) <= threshold:
                touches += 1

        return touches

    def _is_near_level(self, price: Decimal, level: Decimal) -> bool:
        """Check if price is near a support/resistance level"""
        threshold = level * self.support_resistance_threshold
        return abs(price - level) <= threshold

    def _analyze_bullish_trend_entry(
        self,
        df: pd.DataFrame,
        conditions: MarketConditions,
        sr_levels: list[SupportResistanceLevel],
        volume_confirmed: bool,
    ) -> Optional[EntrySignal]:
        """
        Analyze LONG entry in bullish trend — Variant A+.

        Entry corridor: price in [EMA20, EMA20 * (1 + max_distance_from_ema_pct)].
        Three scenarios (first match wins):

        1. Pullback to EMA20: price is within the corridor and rising.
        2. Return from above (A+): price was above the corridor last bar and
           has just returned inside — pullback after overextension.
        3. Bounce from support: price near a validated S/R support and rising.
        """
        current_price = conditions.current_price
        prev_close = Decimal(str(df["close"].iloc[-2]))
        ema = conditions.ema_fast
        ema_upper = ema * (Decimal("1") + self.max_distance_from_ema_pct)

        # --- Scenario 1: classic pullback — price inside corridor and bouncing ---
        in_corridor = ema <= current_price <= ema_upper
        if in_corridor and current_price > prev_close:
            confidence = Decimal("0.8") if volume_confirmed else Decimal("0.6")
            return EntrySignal(
                signal_type=SignalType.LONG,
                entry_reason=EntryReason.TREND_PULLBACK_TO_EMA,
                entry_price=current_price,
                confidence=confidence * (Decimal("1.0") + conditions.ema_divergence_pct),
                market_conditions=conditions,
                volume_confirmed=volume_confirmed,
                timestamp=conditions.timestamp,
            )

        # --- Scenario 2 (A+): return from above — price was overbought, cools back ---
        prev_above_corridor = prev_close > ema_upper
        now_in_corridor = ema <= current_price <= ema_upper
        if prev_above_corridor and now_in_corridor:
            confidence = Decimal("0.7") if volume_confirmed else Decimal("0.5")
            return EntrySignal(
                signal_type=SignalType.LONG,
                entry_reason=EntryReason.TREND_RETURN_FROM_ABOVE,
                entry_price=current_price,
                confidence=confidence * (Decimal("1.0") + conditions.ema_divergence_pct),
                market_conditions=conditions,
                volume_confirmed=volume_confirmed,
                timestamp=conditions.timestamp,
            )

        # --- Scenario 3: bounce from support level ---
        support_levels = [lvl for lvl in sr_levels if lvl.is_support]
        for support in support_levels:
            if self._is_near_level(current_price, support.price) and current_price > prev_close:
                confidence = Decimal("0.75") * support.strength
                if volume_confirmed:
                    confidence *= Decimal("1.2")
                confidence = min(confidence, Decimal("0.95"))
                return EntrySignal(
                    signal_type=SignalType.LONG,
                    entry_reason=EntryReason.TREND_BOUNCE_FROM_SUPPORT,
                    entry_price=current_price,
                    confidence=confidence,
                    market_conditions=conditions,
                    volume_confirmed=volume_confirmed,
                    timestamp=conditions.timestamp,
                )

        return None

    def _analyze_bearish_trend_entry(
        self,
        df: pd.DataFrame,
        conditions: MarketConditions,
        sr_levels: list[SupportResistanceLevel],
        volume_confirmed: bool,
    ) -> Optional[EntrySignal]:
        """
        Analyze SHORT entry in bearish trend — Variant A+ (symmetric to bullish).

        Entry corridor (inverse): price in [EMA20 * (1 - max_distance_from_ema_pct), EMA20].
        Three scenarios (first match wins):

        1. Pullback to EMA20: price is within the corridor and falling.
        2. Return from below (A+): price was below corridor last bar and
           has just returned inside — relief bounce after overextension.
        3. Rejection from resistance: price near a validated S/R resistance and falling.
        """
        current_price = conditions.current_price
        prev_close = Decimal(str(df["close"].iloc[-2]))
        ema = conditions.ema_fast
        ema_lower = ema * (Decimal("1") - self.max_distance_from_ema_pct)

        # --- Scenario 1: price inside corridor [ema_lower, ema] and falling ---
        in_corridor = ema_lower <= current_price <= ema
        if in_corridor and current_price < prev_close:
            confidence = Decimal("0.8") if volume_confirmed else Decimal("0.6")
            return EntrySignal(
                signal_type=SignalType.SHORT,
                entry_reason=EntryReason.TREND_PULLBACK_TO_EMA,
                entry_price=current_price,
                confidence=confidence * (Decimal("1.0") + conditions.ema_divergence_pct),
                market_conditions=conditions,
                volume_confirmed=volume_confirmed,
                timestamp=conditions.timestamp,
            )

        # --- Scenario 2 (A+): return from below — relief bounce back into corridor ---
        prev_below_corridor = prev_close < ema_lower
        now_in_corridor = ema_lower <= current_price <= ema
        if prev_below_corridor and now_in_corridor:
            confidence = Decimal("0.7") if volume_confirmed else Decimal("0.5")
            return EntrySignal(
                signal_type=SignalType.SHORT,
                entry_reason=EntryReason.TREND_RETURN_FROM_ABOVE,
                entry_price=current_price,
                confidence=confidence * (Decimal("1.0") + conditions.ema_divergence_pct),
                market_conditions=conditions,
                volume_confirmed=volume_confirmed,
                timestamp=conditions.timestamp,
            )

        # --- Scenario 3: rejection from resistance level ---
        resistance_levels = [lvl for lvl in sr_levels if not lvl.is_support]
        for resistance in resistance_levels:
            if self._is_near_level(current_price, resistance.price) and current_price < prev_close:
                confidence = Decimal("0.75") * resistance.strength
                if volume_confirmed:
                    confidence *= Decimal("1.2")
                confidence = min(confidence, Decimal("0.95"))
                return EntrySignal(
                    signal_type=SignalType.SHORT,
                    entry_reason=EntryReason.TREND_BOUNCE_FROM_RESISTANCE,
                    entry_price=current_price,
                    confidence=confidence,
                    market_conditions=conditions,
                    volume_confirmed=volume_confirmed,
                    timestamp=conditions.timestamp,
                )

        return None

    def _analyze_sideways_entry(
        self, df: pd.DataFrame, conditions: MarketConditions, volume_confirmed: bool
    ) -> Optional[EntrySignal]:
        """
        Analyze entry in sideways market

        Scenarios:
        - LONG: RSI exits oversold (<30) or range breakout upward
        - SHORT: RSI exits overbought (>70) or range breakout downward
        """
        current_price = conditions.current_price
        rsi_series = self.market_analyzer._calculate_rsi(df, self.market_analyzer.rsi_period)
        prev_rsi = Decimal(str(rsi_series.iloc[-2]))

        # Check RSI oversold exit (LONG signal)
        if prev_rsi < self.rsi_oversold and conditions.rsi >= self.rsi_oversold:
            confidence = Decimal("0.7")
            if volume_confirmed:
                confidence *= Decimal("1.3")
            confidence = min(confidence, Decimal("0.9"))

            return EntrySignal(
                signal_type=SignalType.LONG,
                entry_reason=EntryReason.SIDEWAYS_RSI_OVERSOLD,
                entry_price=current_price,
                confidence=confidence,
                market_conditions=conditions,
                volume_confirmed=volume_confirmed,
                timestamp=conditions.timestamp,
            )

        # Check RSI overbought exit (SHORT signal)
        if prev_rsi > self.rsi_overbought and conditions.rsi <= self.rsi_overbought:
            confidence = Decimal("0.7")
            if volume_confirmed:
                confidence *= Decimal("1.3")
            confidence = min(confidence, Decimal("0.9"))

            return EntrySignal(
                signal_type=SignalType.SHORT,
                entry_reason=EntryReason.SIDEWAYS_RSI_OVERBOUGHT,
                entry_price=current_price,
                confidence=confidence,
                market_conditions=conditions,
                volume_confirmed=volume_confirmed,
                timestamp=conditions.timestamp,
            )

        # Check range breakout (with volume confirmation required)
        if conditions.range_high and conditions.range_low:
            prev_price = Decimal(str(df["close"].iloc[-2]))

            # Upward breakout (LONG signal)
            if prev_price <= conditions.range_high and current_price > conditions.range_high:
                if volume_confirmed:
                    return EntrySignal(
                        signal_type=SignalType.LONG,
                        entry_reason=EntryReason.SIDEWAYS_RANGE_BREAKOUT_UP,
                        entry_price=current_price,
                        confidence=Decimal("0.85"),
                        market_conditions=conditions,
                        volume_confirmed=True,
                        timestamp=conditions.timestamp,
                    )

            # Downward breakout (SHORT signal)
            if prev_price >= conditions.range_low and current_price < conditions.range_low:
                if volume_confirmed:
                    return EntrySignal(
                        signal_type=SignalType.SHORT,
                        entry_reason=EntryReason.SIDEWAYS_RANGE_BREAKOUT_DOWN,
                        entry_price=current_price,
                        confidence=Decimal("0.85"),
                        market_conditions=conditions,
                        volume_confirmed=True,
                        timestamp=conditions.timestamp,
                    )

        return None
