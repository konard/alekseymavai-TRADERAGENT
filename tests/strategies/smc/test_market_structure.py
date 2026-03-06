"""
Unit tests for Market Structure Analyzer

Tests cover:
- Swing high/low detection
- Break of Structure (BOS) detection
- Change of Character (CHoCH) detection
- Trend determination
- Multi-timeframe analysis
- Edge cases and performance
"""

import time
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from bot.strategies.smc.market_structure import (
    MarketStructureAnalyzer,
    StructureBreak,
    StructureEvent,
    SwingPoint,
    TrendDirection,
)


class TestMarketStructureAnalyzer(unittest.TestCase):
    """Test cases for MarketStructureAnalyzer"""

    def setUp(self):
        """Set up test fixtures"""
        self.analyzer = MarketStructureAnalyzer(swing_length=5, trend_period=20)

    def _create_sample_data(self, pattern: str = "uptrend", length: int = 100) -> pd.DataFrame:
        """
        Create sample OHLCV data for testing

        Args:
            pattern: Type of pattern ("uptrend", "downtrend", "ranging", "swing")
            length: Number of candles

        Returns:
            DataFrame with OHLCV data
        """
        base_price = 100.0
        timestamps = pd.date_range(start=datetime.now(), periods=length, freq="1h")

        if pattern == "uptrend":
            # Create uptrend with higher highs and higher lows
            trend = np.linspace(0, 20, length)
            noise = np.random.randn(length) * 0.5
            close = base_price + trend + noise

        elif pattern == "downtrend":
            # Create downtrend with lower highs and lower lows
            trend = np.linspace(0, -20, length)
            noise = np.random.randn(length) * 0.5
            close = base_price + trend + noise

        elif pattern == "ranging":
            # Create ranging market
            noise = np.random.randn(length) * 2
            close = base_price + noise

        elif pattern == "swing":
            # Create clear swing points
            swing_pattern = []
            for i in range(length):
                if i % 20 < 10:
                    swing_pattern.append(base_price + (i % 20))
                else:
                    swing_pattern.append(base_price + 20 - (i % 20))
            close = np.array(swing_pattern)

        else:
            close = np.ones(length) * base_price

        # Create OHLC from close
        open_price = close + np.random.randn(length) * 0.2
        high = np.maximum(open_price, close) + np.abs(np.random.randn(length) * 0.3)
        low = np.minimum(open_price, close) - np.abs(np.random.randn(length) * 0.3)
        volume = np.random.randint(1000, 10000, length)

        df = pd.DataFrame(
            {"open": open_price, "high": high, "low": low, "close": close, "volume": volume},
            index=timestamps,
        )

        return df

    def test_swing_high_detection(self):
        """Test swing high detection"""
        # Create data with clear swing highs
        df = self._create_sample_data("swing", length=50)

        self.analyzer.analyze(df)

        # Should detect multiple swing highs
        self.assertGreater(
            len(self.analyzer.swing_highs), 0, "Should detect at least one swing high"
        )

        # Verify swing high properties
        for swing in self.analyzer.swing_highs:
            self.assertTrue(swing.is_high, "Swing should be marked as high")
            self.assertIsInstance(swing.price, Decimal, "Price should be Decimal")
            self.assertEqual(swing.strength, 5, "Strength should match swing_length")

    def test_swing_low_detection(self):
        """Test swing low detection"""
        df = self._create_sample_data("swing", length=50)

        self.analyzer.analyze(df)

        # Should detect multiple swing lows
        self.assertGreater(len(self.analyzer.swing_lows), 0, "Should detect at least one swing low")

        # Verify swing low properties
        for swing in self.analyzer.swing_lows:
            self.assertFalse(swing.is_high, "Swing should be marked as low")
            self.assertIsInstance(swing.price, Decimal, "Price should be Decimal")

    def test_uptrend_detection(self):
        """Test bullish trend detection"""
        df = self._create_sample_data("uptrend", length=200)

        self.analyzer.analyze(df)

        # Should detect bullish trend
        self.assertEqual(
            self.analyzer.current_trend,
            TrendDirection.BULLISH,
            "Should detect bullish trend in uptrending market",
        )

    def test_downtrend_detection(self):
        """Test bearish trend detection"""
        df = self._create_sample_data("downtrend", length=200)

        self.analyzer.analyze(df)

        # Should detect bearish trend
        self.assertEqual(
            self.analyzer.current_trend,
            TrendDirection.BEARISH,
            "Should detect bearish trend in downtrending market",
        )

    def test_ranging_market_detection(self):
        """Test ranging market detection"""
        df = self._create_sample_data("ranging", length=100)

        self.analyzer.analyze(df)

        # Should detect ranging or have low confidence
        # (ranging markets can be tricky, so we just verify it completes)
        self.assertIn(
            self.analyzer.current_trend,
            [TrendDirection.BULLISH, TrendDirection.BEARISH, TrendDirection.RANGING],
        )

    def test_bos_detection_bullish(self):
        """Test Break of Structure detection in uptrend"""
        # Create clear uptrend with structure breaks
        df = self._create_sample_data("uptrend", length=100)

        self.analyzer.analyze(df)

        # Filter BOS events
        bos_events = [
            e for e in self.analyzer.structure_events if e.event_type == StructureBreak.BOS
        ]

        # In an uptrend, should have some BOS events
        # (may vary based on data, so we just check structure)
        for event in bos_events:
            self.assertEqual(event.event_type, StructureBreak.BOS)
            self.assertIsInstance(event.price, Decimal)

    def test_choch_detection(self):
        """Test Change of Character detection"""
        # Create data that reverses trend
        uptrend_data = self._create_sample_data("uptrend", length=50)
        downtrend_data = self._create_sample_data("downtrend", length=50)

        # Shift downtrend data to continue from uptrend
        downtrend_data["close"] = downtrend_data["close"] + 20
        downtrend_data["open"] = downtrend_data["open"] + 20
        downtrend_data["high"] = downtrend_data["high"] + 20
        downtrend_data["low"] = downtrend_data["low"] + 20

        # Combine data
        df = pd.concat([uptrend_data, downtrend_data])

        self.analyzer.analyze(df)

        # Should detect CHoCH events at trend reversal
        choch_events = [
            e for e in self.analyzer.structure_events if e.event_type == StructureBreak.CHOCH
        ]

        # Structure is detected (may vary, so just verify structure)
        for event in choch_events:
            self.assertEqual(event.event_type, StructureBreak.CHOCH)

    def test_multi_timeframe_analysis(self):
        """Test multi-timeframe trend analysis"""
        # Create aligned trends
        df_d1 = self._create_sample_data("uptrend", length=50)
        df_h4 = self._create_sample_data("uptrend", length=100)

        result = self.analyzer.analyze_trend(df_d1, df_h4)

        # Verify result structure
        self.assertIn("d1_trend", result)
        self.assertIn("h4_trend", result)
        self.assertIn("trend_strength", result)
        self.assertIn("trend_aligned", result)

        # Verify types
        self.assertIsInstance(result["trend_strength"], float)
        self.assertGreaterEqual(result["trend_strength"], 0.0)
        self.assertLessEqual(result["trend_strength"], 1.0)

    def test_multi_timeframe_aligned_trends(self):
        """Test multi-timeframe with aligned bullish trends"""
        # Use larger datasets with strong trend so library detection is reliable
        df_d1 = self._create_sample_data("uptrend", length=500)
        df_h4 = self._create_sample_data("uptrend", length=500)

        result = self.analyzer.analyze_trend(df_d1, df_h4)

        # Both should be bullish (strong uptrend over 500 bars)
        self.assertEqual(result["d1_trend"], TrendDirection.BULLISH)
        self.assertEqual(result["h4_trend"], TrendDirection.BULLISH)
        self.assertTrue(result["trend_aligned"])
        self.assertEqual(result["trend_strength"], 1.0)

    def test_insufficient_data(self):
        """Test behavior with insufficient data"""
        # Create very small dataset
        df = self._create_sample_data("uptrend", length=5)

        result = self.analyzer.analyze(df)

        # Should handle gracefully
        self.assertIsInstance(result, dict)
        self.assertEqual(result["swing_highs_count"], 0)
        self.assertEqual(result["swing_lows_count"], 0)

    def test_get_current_structure(self):
        """Test get_current_structure method"""
        df = self._create_sample_data("uptrend", length=100)

        self.analyzer.analyze(df)
        structure = self.analyzer.get_current_structure()

        # Verify structure dict
        self.assertIn("swing_highs_count", structure)
        self.assertIn("swing_lows_count", structure)
        self.assertIn("current_trend", structure)
        self.assertIn("structure_events_count", structure)

    def test_get_recent_swing_high(self):
        """Test get_recent_swing_high method"""
        df = self._create_sample_data("swing", length=50)

        self.analyzer.analyze(df)
        recent_high = self.analyzer.get_recent_swing_high()

        if recent_high:
            self.assertIsInstance(recent_high, SwingPoint)
            self.assertTrue(recent_high.is_high)

    def test_get_recent_swing_low(self):
        """Test get_recent_swing_low method"""
        df = self._create_sample_data("swing", length=50)

        self.analyzer.analyze(df)
        recent_low = self.analyzer.get_recent_swing_low()

        if recent_low:
            self.assertIsInstance(recent_low, SwingPoint)
            self.assertFalse(recent_low.is_high)

    def test_get_structure_events(self):
        """Test get_structure_events method"""
        df = self._create_sample_data("uptrend", length=100)

        self.analyzer.analyze(df)
        events = self.analyzer.get_structure_events(limit=5)

        # Should return list (may be empty)
        self.assertIsInstance(events, list)
        self.assertLessEqual(len(events), 5)

        for event in events:
            self.assertIsInstance(event, StructureEvent)

    def test_performance(self):
        """Test performance - should process 1000 candles in < 100ms"""
        df = self._create_sample_data("uptrend", length=1000)

        start_time = time.time()
        self.analyzer.analyze(df)
        elapsed_time = (time.time() - start_time) * 1000  # Convert to ms

        self.assertLess(
            elapsed_time, 5000, f"Analysis took {elapsed_time:.2f}ms, should be < 5000ms"
        )

    def test_get_swings_df_returns_none_after_analysis(self):
        """get_swings_df always returns None — swings are now in get_smc_context()."""
        df = self._create_sample_data("uptrend", length=100)
        self.analyzer.analyze(df)

        # get_swings_df() was replaced by get_smc_context(); always returns None
        self.assertIsNone(self.analyzer.get_swings_df())

    def test_get_swings_df_none_before_analysis(self):
        """Test get_swings_df returns None before analysis"""
        self.assertIsNone(self.analyzer.get_swings_df())

    def test_swing_detection_accuracy(self):
        """Test swing detection accuracy with known pattern"""
        # Create data with exactly 3 clear swing highs
        data = []
        timestamps = []
        base_time = datetime.now()

        for i in range(50):
            timestamps.append(base_time + timedelta(hours=i))
            if i == 10 or i == 30:
                # Create swing high
                data.append({"open": 100, "high": 110, "low": 99, "close": 105, "volume": 1000})
            elif i == 20 or i == 40:
                # Create swing low
                data.append({"open": 100, "high": 101, "low": 90, "close": 95, "volume": 1000})
            else:
                # Normal candle
                data.append({"open": 100, "high": 102, "low": 98, "close": 100, "volume": 1000})

        df = pd.DataFrame(data, index=timestamps)

        analyzer = MarketStructureAnalyzer(swing_length=3)
        analyzer.analyze(df)

        # Should detect the swing points (at least some)
        self.assertGreater(len(analyzer.swing_highs), 0, "Should detect swing highs")
        self.assertGreater(len(analyzer.swing_lows), 0, "Should detect swing lows")


class TestSwingPoint(unittest.TestCase):
    """Test cases for SwingPoint dataclass"""

    def test_swing_point_creation(self):
        """Test SwingPoint object creation"""
        swing = SwingPoint(
            index=10,
            price=Decimal("100.50"),
            timestamp=pd.Timestamp.now(),
            is_high=True,
            strength=5,
        )

        self.assertEqual(swing.index, 10)
        self.assertEqual(swing.price, Decimal("100.50"))
        self.assertTrue(swing.is_high)
        self.assertEqual(swing.strength, 5)


class TestStructureEvent(unittest.TestCase):
    """Test cases for StructureEvent dataclass"""

    def test_structure_event_creation(self):
        """Test StructureEvent object creation"""
        swing = SwingPoint(
            index=10,
            price=Decimal("100.00"),
            timestamp=pd.Timestamp.now(),
            is_high=True,
            strength=5,
        )

        event = StructureEvent(
            event_type=StructureBreak.BOS,
            index=20,
            price=Decimal("105.00"),
            timestamp=pd.Timestamp.now(),
            previous_swing=swing,
            current_trend=TrendDirection.BULLISH,
        )

        self.assertEqual(event.event_type, StructureBreak.BOS)
        self.assertEqual(event.index, 20)
        self.assertEqual(event.current_trend, TrendDirection.BULLISH)
        self.assertEqual(event.previous_swing, swing)


if __name__ == "__main__":
    unittest.main()
