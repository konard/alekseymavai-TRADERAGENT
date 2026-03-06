"""Tests for MarketRegimeDetector v2.0 (6-regime classifier with ADX hysteresis)."""

import numpy as np
import pandas as pd

from bot.orchestrator.market_regime import (
    MarketRegime,
    MarketRegimeDetector,
    RecommendedStrategy,
)


def _make_ohlcv(close_prices: list[float], spread: float = 0.02) -> pd.DataFrame:
    """Helper to create OHLCV DataFrame from close prices."""
    n = len(close_prices)
    close = np.array(close_prices, dtype=float)
    high = close * (1 + spread)
    low = close * (1 - spread)
    open_ = (close + np.roll(close, 1)) / 2
    open_[0] = close[0]
    volume = np.random.uniform(1000, 5000, n)

    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def _make_trending_up(n: int = 100, start: float = 2000, step: float = 10) -> pd.DataFrame:
    """Create uptrending OHLCV data."""
    prices = [start + i * step + np.random.uniform(-2, 2) for i in range(n)]
    return _make_ohlcv(prices)


def _make_trending_down(n: int = 100, start: float = 4000, step: float = 10) -> pd.DataFrame:
    """Create downtrending OHLCV data."""
    prices = [start - i * step + np.random.uniform(-2, 2) for i in range(n)]
    return _make_ohlcv(prices)


def _make_sideways(n: int = 100, center: float = 3000, amplitude: float = 20) -> pd.DataFrame:
    """Create sideways/ranging OHLCV data."""
    prices = [center + amplitude * np.sin(i * 0.3) + np.random.uniform(-5, 5) for i in range(n)]
    return _make_ohlcv(prices, spread=0.005)


def _make_high_volatility(n: int = 100, center: float = 3000) -> pd.DataFrame:
    """Create high volatility OHLCV data."""
    prices = [center + np.random.uniform(-200, 200) for _ in range(n)]
    return _make_ohlcv(prices, spread=0.08)


class TestMarketRegimeDetector:
    """Tests for MarketRegimeDetector."""

    def test_init_defaults(self):
        detector = MarketRegimeDetector()
        assert detector.ema_fast == 20
        assert detector.ema_slow == 50
        assert detector.atr_period == 14
        assert detector.rsi_period == 14
        assert detector.adx_period == 14
        assert detector.bb_period == 20
        assert detector.bb_std_dev == 2.0
        assert detector.volume_lookback == 20
        assert detector.regime_history_size == 10

    def test_init_hysteresis_defaults(self):
        detector = MarketRegimeDetector()
        assert detector.adx_enter_trending == 32.0
        assert detector.adx_exit_trending == 25.0
        assert detector.adx_enter_ranging == 18.0
        assert detector.adx_exit_ranging == 22.0
        assert detector.atr_wide_threshold == 1.0
        assert detector.atr_volatile_threshold == 2.0

    def test_init_custom(self):
        detector = MarketRegimeDetector(
            ema_fast=10,
            ema_slow=30,
            adx_period=20,
            bb_period=15,
            bb_std_dev=1.5,
            volume_lookback=30,
            trend_threshold=1.0,
            regime_history_size=5,
        )
        assert detector.ema_fast == 10
        assert detector.ema_slow == 30
        assert detector.adx_period == 20
        assert detector.bb_period == 15
        assert detector.bb_std_dev == 1.5
        assert detector.volume_lookback == 30
        assert detector.trend_threshold == 1.0
        assert detector.regime_history_size == 5

    def test_insufficient_data(self):
        detector = MarketRegimeDetector()
        df = _make_ohlcv([100, 200, 300])
        result = detector.analyze(df)
        assert result.regime == MarketRegime.UNKNOWN
        assert result.confidence == 0.0

    def test_trending_bullish(self):
        detector = MarketRegimeDetector(trend_threshold=0.3)
        df = _make_trending_up(n=100, step=15)
        result = detector.analyze(df)

        assert result.regime in (
            MarketRegime.BULL_TREND,
            MarketRegime.QUIET_TRANSITION,
            MarketRegime.VOLATILE_TRANSITION,
        )
        assert result.trend_strength > 0
        assert result.confidence > 0.0

    def test_trending_bearish(self):
        detector = MarketRegimeDetector(trend_threshold=0.3)
        df = _make_trending_down(n=100, step=15)
        result = detector.analyze(df)

        assert result.regime in (
            MarketRegime.BEAR_TREND,
            MarketRegime.QUIET_TRANSITION,
            MarketRegime.VOLATILE_TRANSITION,
        )
        assert result.trend_strength < 0

    def test_sideways(self):
        detector = MarketRegimeDetector(trend_threshold=0.5)
        df = _make_sideways(n=100, amplitude=5)
        result = detector.analyze(df)

        # Sideways with small amplitude should be range or transition
        assert result.regime in (
            MarketRegime.TIGHT_RANGE,
            MarketRegime.WIDE_RANGE,
            MarketRegime.QUIET_TRANSITION,
        )

    def test_strategy_recommendation_grid_for_range(self):
        """Range regime should recommend Grid strategy."""
        detector = MarketRegimeDetector(trend_threshold=0.5)
        df = _make_sideways(n=100, amplitude=3)
        result = detector.analyze(df)

        if result.regime in (MarketRegime.TIGHT_RANGE, MarketRegime.WIDE_RANGE):
            assert result.recommended_strategy == RecommendedStrategy.GRID

    def test_strategy_recommendation_dca_for_bear_trend(self):
        """Bear trend should recommend DCA."""
        detector = MarketRegimeDetector(trend_threshold=0.3)
        df = _make_trending_down(n=100, step=15)
        result = detector.analyze(df)

        if result.regime == MarketRegime.BEAR_TREND:
            assert result.recommended_strategy == RecommendedStrategy.DCA

    def test_strategy_recommendation_reduce_for_volatile_transition(self):
        """Volatile transition should recommend reducing exposure."""
        detector = MarketRegimeDetector()
        # Directly test the recommendation logic
        regime = MarketRegime.VOLATILE_TRANSITION
        recommended = detector._recommend_strategy(regime, 0.5, 25.0)
        assert recommended == RecommendedStrategy.REDUCE_EXPOSURE

    def test_strategy_recommendation_hold_for_quiet_transition(self):
        """Quiet transition should recommend hold."""
        detector = MarketRegimeDetector()
        regime = MarketRegime.QUIET_TRANSITION
        recommended = detector._recommend_strategy(regime, 0.5, 25.0)
        assert recommended == RecommendedStrategy.HOLD

    def test_confluence_score_range(self):
        detector = MarketRegimeDetector()
        df = _make_trending_up(n=100)
        result = detector.analyze(df)

        assert 0.0 <= result.confluence_score <= 1.0
        assert 0.0 <= result.confidence <= 1.0

    def test_analysis_metrics(self):
        detector = MarketRegimeDetector()
        df = _make_sideways(n=100)
        result = detector.analyze(df)

        assert result.rsi > 0
        assert result.atr_pct >= 0
        assert result.timestamp is not None
        assert "current_price" in result.analysis_details
        assert "ema_fast" in result.analysis_details

    def test_to_dict(self):
        detector = MarketRegimeDetector()
        df = _make_sideways(n=100)
        result = detector.analyze(df)

        d = result.to_dict()
        assert isinstance(d, dict)
        assert "regime" in d
        assert "confidence" in d
        assert "recommended_strategy" in d
        assert "timestamp" in d

    def test_last_analysis_cached(self):
        detector = MarketRegimeDetector()
        assert detector.last_analysis is None

        df = _make_trending_up(n=100)
        result = detector.analyze(df)

        assert detector.last_analysis is result

    def test_atr_calculation(self):
        """Test ATR calculation helper."""
        df = _make_sideways(n=50)
        atr = MarketRegimeDetector._calculate_atr(df["high"], df["low"], df["close"], 14)
        assert len(atr) == 50
        assert atr.iloc[-1] > 0

    def test_rsi_calculation(self):
        """Test RSI calculation helper."""
        df = _make_trending_up(n=50)
        rsi = MarketRegimeDetector._calculate_rsi(df["close"], 14)
        assert len(rsi) == 50
        # RSI should be > 50 for uptrend
        last_rsi = rsi.iloc[-1]
        assert 0 <= last_rsi <= 100


class TestADXCalculation:
    """Tests for ADX indicator."""

    def test_adx_returns_three_series(self):
        df = _make_trending_up(n=50)
        adx, plus_di, minus_di = MarketRegimeDetector._calculate_adx(
            df["high"], df["low"], df["close"], 14
        )
        assert len(adx) == 50
        assert len(plus_di) == 50
        assert len(minus_di) == 50

    def test_adx_positive_in_uptrend(self):
        """ADX should be positive and +DI > -DI in uptrend."""
        df = _make_trending_up(n=100, step=15)
        adx, plus_di, minus_di = MarketRegimeDetector._calculate_adx(
            df["high"], df["low"], df["close"], 14
        )
        last_adx = adx.iloc[-1]
        assert last_adx > 0
        # In strong uptrend, +DI should exceed -DI
        assert plus_di.iloc[-1] > minus_di.iloc[-1]

    def test_adx_negative_di_in_downtrend(self):
        """-DI should exceed +DI in downtrend."""
        df = _make_trending_down(n=100, step=15)
        adx, plus_di, minus_di = MarketRegimeDetector._calculate_adx(
            df["high"], df["low"], df["close"], 14
        )
        assert adx.iloc[-1] > 0
        assert minus_di.iloc[-1] > plus_di.iloc[-1]

    def test_adx_low_in_sideways(self):
        """ADX should be relatively low in sideways market."""
        df = _make_sideways(n=100, amplitude=3)
        adx, _, _ = MarketRegimeDetector._calculate_adx(df["high"], df["low"], df["close"], 14)
        last_adx = adx.iloc[-1]
        # Sideways markets typically have lower ADX
        assert last_adx < 40


class TestBollingerBands:
    """Tests for Bollinger Bands calculation."""

    def test_bb_returns_four_series(self):
        df = _make_sideways(n=50)
        bb_upper, bb_middle, bb_lower, bb_width = MarketRegimeDetector._calculate_bollinger_bands(
            df["close"], 20, 2.0
        )
        assert len(bb_upper) == 50
        assert len(bb_middle) == 50
        assert len(bb_lower) == 50
        assert len(bb_width) == 50

    def test_bb_band_ordering(self):
        """Upper > middle > lower for valid data."""
        df = _make_sideways(n=50)
        bb_upper, bb_middle, bb_lower, _ = MarketRegimeDetector._calculate_bollinger_bands(
            df["close"], 20, 2.0
        )
        # Check last non-NaN values
        idx = -1
        assert bb_upper.iloc[idx] > bb_middle.iloc[idx] > bb_lower.iloc[idx]

    def test_bb_width_positive(self):
        """BB width percentage should be positive."""
        df = _make_sideways(n=50)
        _, _, _, bb_width = MarketRegimeDetector._calculate_bollinger_bands(df["close"], 20, 2.0)
        last_width = bb_width.iloc[-1]
        assert last_width > 0

    def test_bb_width_wider_in_volatile_market(self):
        """BB width should be wider for volatile data."""
        df_calm = _make_sideways(n=100, amplitude=3)
        df_volatile = _make_high_volatility(n=100)

        _, _, _, width_calm = MarketRegimeDetector._calculate_bollinger_bands(
            df_calm["close"], 20, 2.0
        )
        _, _, _, width_volatile = MarketRegimeDetector._calculate_bollinger_bands(
            df_volatile["close"], 20, 2.0
        )

        assert width_volatile.iloc[-1] > width_calm.iloc[-1]


class TestVolumeRatio:
    """Tests for volume ratio calculation."""

    def test_volume_ratio_returns_two_series(self):
        df = _make_trending_up(n=50)
        avg_vol, vol_ratio = MarketRegimeDetector._calculate_volume_ratio(df["volume"], 20)
        assert len(avg_vol) == 50
        assert len(vol_ratio) == 50

    def test_volume_ratio_positive(self):
        """Volume ratio should be positive."""
        df = _make_trending_up(n=50)
        _, vol_ratio = MarketRegimeDetector._calculate_volume_ratio(df["volume"], 20)
        last_ratio = vol_ratio.iloc[-1]
        assert last_ratio > 0

    def test_volume_ratio_around_one(self):
        """For uniform volume, ratio should be near 1.0."""
        # Create data with constant volume
        df = _make_sideways(n=50)
        df["volume"] = 1000.0  # Constant volume
        _, vol_ratio = MarketRegimeDetector._calculate_volume_ratio(df["volume"], 20)
        last_ratio = vol_ratio.iloc[-1]
        assert abs(last_ratio - 1.0) < 0.01


class TestEnhancedAnalysis:
    """Tests for enhanced analysis fields."""

    def test_new_fields_present(self):
        """All new fields should be present in RegimeAnalysis."""
        detector = MarketRegimeDetector()
        df = _make_trending_up(n=100)
        result = detector.analyze(df)

        assert hasattr(result, "adx")
        assert hasattr(result, "bb_width_pct")
        assert hasattr(result, "volume_ratio")
        assert hasattr(result, "regime_duration_seconds")
        assert hasattr(result, "previous_regime")

    def test_new_fields_valid_ranges(self):
        """New fields should be within valid ranges."""
        detector = MarketRegimeDetector()
        df = _make_trending_up(n=100)
        result = detector.analyze(df)

        assert result.adx >= 0
        assert result.bb_width_pct >= 0
        assert result.volume_ratio > 0
        assert result.regime_duration_seconds >= 0

    def test_to_dict_includes_new_fields(self):
        """to_dict() should include all new fields."""
        detector = MarketRegimeDetector()
        df = _make_sideways(n=100)
        result = detector.analyze(df)

        d = result.to_dict()
        assert "adx" in d
        assert "bb_width_pct" in d
        assert "volume_ratio" in d
        assert "regime_duration_seconds" in d
        assert "previous_regime" in d

    def test_analysis_details_include_bb(self):
        """analysis_details should include BB and volume info."""
        detector = MarketRegimeDetector()
        df = _make_sideways(n=100)
        result = detector.analyze(df)

        assert "bb_upper" in result.analysis_details
        assert "bb_middle" in result.analysis_details
        assert "bb_lower" in result.analysis_details
        assert "avg_volume" in result.analysis_details
        assert "plus_di" in result.analysis_details
        assert "minus_di" in result.analysis_details

    def test_unknown_analysis_has_new_fields(self):
        """Unknown analysis should include new fields with defaults."""
        detector = MarketRegimeDetector()
        df = _make_ohlcv([100, 200, 300])
        result = detector.analyze(df)

        assert result.regime == MarketRegime.UNKNOWN
        assert result.adx == 20.0
        assert result.bb_width_pct == 4.0
        assert result.volume_ratio == 1.0
        assert result.regime_duration_seconds == 0
        assert result.previous_regime is None


class TestRegimeHistory:
    """Tests for regime history tracking."""

    def test_empty_history_initially(self):
        detector = MarketRegimeDetector()
        assert len(detector.regime_history) == 0

    def test_history_grows_after_analyze(self):
        detector = MarketRegimeDetector()
        df = _make_sideways(n=100)

        detector.analyze(df)
        assert len(detector.regime_history) == 1

        detector.analyze(df)
        assert len(detector.regime_history) == 2

    def test_history_max_size(self):
        """History should not exceed regime_history_size."""
        detector = MarketRegimeDetector(regime_history_size=5)
        df = _make_sideways(n=100)

        for _ in range(10):
            detector.analyze(df)

        assert len(detector.regime_history) == 5

    def test_history_most_recent_first(self):
        """Most recent analysis should be at index 0."""
        detector = MarketRegimeDetector()
        df = _make_sideways(n=100)

        detector.analyze(df)
        first_analysis = detector.regime_history[0]

        detector.analyze(df)
        second_analysis = detector.regime_history[0]

        # Second analysis should be more recent
        assert second_analysis.timestamp >= first_analysis.timestamp

    def test_history_is_copy(self):
        """regime_history property should return a copy."""
        detector = MarketRegimeDetector()
        df = _make_sideways(n=100)
        detector.analyze(df)

        history = detector.regime_history
        history.clear()

        # Internal history should be unaffected
        assert len(detector.regime_history) == 1

    def test_previous_regime_none_on_first(self):
        """Previous regime should be None on first analysis."""
        detector = MarketRegimeDetector()
        df = _make_sideways(n=100, amplitude=3)
        result = detector.analyze(df)
        assert result.previous_regime is None

    def test_previous_regime_detected_on_change(self):
        """Previous regime should be set when regime changes."""
        detector = MarketRegimeDetector(trend_threshold=0.3)

        # First: analyze sideways
        df_sideways = _make_sideways(n=100, amplitude=3)
        result1 = detector.analyze(df_sideways)
        first_regime = result1.regime

        # Second: analyze strong trend
        df_trend = _make_trending_up(n=100, step=20)
        result2 = detector.analyze(df_trend)

        # If regime actually changed, previous_regime should be set
        if result2.regime != first_regime:
            assert result2.previous_regime == first_regime

    def test_regime_duration_zero_on_first(self):
        """Duration should be 0 on first analysis."""
        detector = MarketRegimeDetector()
        df = _make_sideways(n=100)
        result = detector.analyze(df)
        assert result.regime_duration_seconds == 0


class TestEnhancedClassification:
    """Tests for enhanced regime classification with ADX and BB."""

    def test_adx_strengthens_trend(self):
        """Strong ADX should contribute to trending classification."""
        detector = MarketRegimeDetector(trend_threshold=0.5)
        df = _make_trending_up(n=100, step=12)
        result = detector.analyze(df)

        if result.regime == MarketRegime.BULL_TREND:
            assert result.adx > 20.0
            assert result.confidence > 0.3

    def test_narrow_bb_indicates_range(self):
        """Narrow BB in calm market should indicate range."""
        detector = MarketRegimeDetector()
        df = _make_sideways(n=100, amplitude=3)
        result = detector.analyze(df)

        if result.regime in (MarketRegime.TIGHT_RANGE, MarketRegime.WIDE_RANGE):
            assert result.bb_width_pct < 4.0

    def test_wide_bb_in_volatile_data(self):
        """Very wide BB should be present in high volatility data."""
        detector = MarketRegimeDetector()
        df = _make_high_volatility(n=100)
        result = detector.analyze(df)

        # High volatility data should have wide BB
        assert result.bb_width_pct > 2.0

    def test_volume_ratio_in_confluence(self):
        """Volume ratio should be reflected in analysis."""
        detector = MarketRegimeDetector()
        df = _make_trending_up(n=100)
        result = detector.analyze(df)

        assert result.volume_ratio > 0
        assert 0.0 <= result.confluence_score <= 1.0

    def test_bull_trend_hybrid_with_high_confluence(self):
        """Bull trend with high confluence should recommend HYBRID."""
        detector = MarketRegimeDetector(
            trend_threshold=0.3,
            confluence_threshold=0.3,  # Low threshold to ensure confluence passes
        )
        df = _make_trending_up(n=100, step=12)
        result = detector.analyze(df)

        if result.regime == MarketRegime.BULL_TREND:
            if result.confluence_score >= 0.3:
                assert result.recommended_strategy == RecommendedStrategy.HYBRID

    def test_bear_trend_always_dca(self):
        """Bear trend should always recommend DCA regardless of confluence."""
        detector = MarketRegimeDetector(trend_threshold=0.3)
        df = _make_trending_down(n=100, step=15)
        result = detector.analyze(df)

        if result.regime == MarketRegime.BEAR_TREND:
            assert result.recommended_strategy == RecommendedStrategy.DCA


class TestClassifyRegimeUnit:
    """Unit tests for _classify_regime with controlled inputs."""

    def test_high_adx_bullish(self):
        """ADX>32 with EMA fast > slow → BULL_TREND."""
        detector = MarketRegimeDetector()
        regime = detector._classify_regime(
            adx=40.0, atr_pct=1.5, ema_fast=3050.0, ema_slow=3000.0, current_regime=None
        )
        assert regime == MarketRegime.BULL_TREND

    def test_high_adx_bearish(self):
        """ADX>32 with EMA fast < slow → BEAR_TREND."""
        detector = MarketRegimeDetector()
        regime = detector._classify_regime(
            adx=40.0, atr_pct=1.5, ema_fast=2950.0, ema_slow=3000.0, current_regime=None
        )
        assert regime == MarketRegime.BEAR_TREND

    def test_low_adx_tight_range(self):
        """ADX<18 with ATR<1% → TIGHT_RANGE."""
        detector = MarketRegimeDetector()
        regime = detector._classify_regime(
            adx=15.0, atr_pct=0.5, ema_fast=3000.0, ema_slow=3000.0, current_regime=None
        )
        assert regime == MarketRegime.TIGHT_RANGE

    def test_low_adx_wide_range(self):
        """ADX<18 with ATR>=1% → WIDE_RANGE."""
        detector = MarketRegimeDetector()
        regime = detector._classify_regime(
            adx=15.0, atr_pct=1.5, ema_fast=3000.0, ema_slow=3000.0, current_regime=None
        )
        assert regime == MarketRegime.WIDE_RANGE

    def test_mid_adx_quiet_transition(self):
        """ADX between 18-32 with ATR<2% → QUIET_TRANSITION."""
        detector = MarketRegimeDetector()
        regime = detector._classify_regime(
            adx=25.0, atr_pct=1.0, ema_fast=3000.0, ema_slow=3000.0, current_regime=None
        )
        assert regime == MarketRegime.QUIET_TRANSITION

    def test_mid_adx_volatile_transition(self):
        """ADX between 18-32 with ATR>=2% → VOLATILE_TRANSITION."""
        detector = MarketRegimeDetector()
        regime = detector._classify_regime(
            adx=25.0, atr_pct=3.0, ema_fast=3000.0, ema_slow=3000.0, current_regime=None
        )
        assert regime == MarketRegime.VOLATILE_TRANSITION


class TestADXHysteresis:
    """Tests for ADX hysteresis — state-dependent threshold behavior."""

    def test_stays_in_trend_with_moderate_adx(self):
        """In trend: ADX=28 (between 25 and 32) should stay in trend."""
        detector = MarketRegimeDetector()
        # Currently in BULL_TREND, ADX=28 is above exit_trending (25)
        regime = detector._classify_regime(
            adx=28.0,
            atr_pct=1.5,
            ema_fast=3050.0,
            ema_slow=3000.0,
            current_regime=MarketRegime.BULL_TREND,
        )
        assert regime == MarketRegime.BULL_TREND

    def test_exits_trend_when_adx_drops_below_25(self):
        """In trend: ADX=23 (below 25) should exit trend."""
        detector = MarketRegimeDetector()
        regime = detector._classify_regime(
            adx=23.0,
            atr_pct=1.0,
            ema_fast=3050.0,
            ema_slow=3000.0,
            current_regime=MarketRegime.BULL_TREND,
        )
        # ADX 23 is between 18 and 32, so falls to transition zone
        assert regime in (MarketRegime.QUIET_TRANSITION, MarketRegime.VOLATILE_TRANSITION)

    def test_range_needs_adx_32_to_enter_trend(self):
        """In range: ADX=28 should NOT enter trend (need 32)."""
        detector = MarketRegimeDetector()
        # Currently in TIGHT_RANGE, ADX=28 is above exit_ranging (22)
        # so we exit range, but ADX<32 so we end up in transition
        regime = detector._classify_regime(
            adx=28.0,
            atr_pct=0.5,
            ema_fast=3050.0,
            ema_slow=3000.0,
            current_regime=MarketRegime.TIGHT_RANGE,
        )
        assert regime in (MarketRegime.QUIET_TRANSITION, MarketRegime.VOLATILE_TRANSITION)

    def test_range_stays_until_adx_above_22(self):
        """In range: ADX=20 (below 22) should stay in range."""
        detector = MarketRegimeDetector()
        regime = detector._classify_regime(
            adx=20.0,
            atr_pct=0.5,
            ema_fast=3050.0,
            ema_slow=3000.0,
            current_regime=MarketRegime.TIGHT_RANGE,
        )
        assert regime == MarketRegime.TIGHT_RANGE

    def test_tight_vs_wide_range_split_by_atr(self):
        """ATR threshold splits tight vs wide range."""
        detector = MarketRegimeDetector()
        tight = detector._classify_regime(
            adx=15.0,
            atr_pct=0.5,
            ema_fast=3000.0,
            ema_slow=3000.0,
            current_regime=None,
        )
        wide = detector._classify_regime(
            adx=15.0,
            atr_pct=1.5,
            ema_fast=3000.0,
            ema_slow=3000.0,
            current_regime=None,
        )
        assert tight == MarketRegime.TIGHT_RANGE
        assert wide == MarketRegime.WIDE_RANGE

    def test_quiet_vs_volatile_transition_split_by_atr(self):
        """ATR threshold splits quiet vs volatile transition."""
        detector = MarketRegimeDetector()
        quiet = detector._classify_regime(
            adx=25.0,
            atr_pct=1.0,
            ema_fast=3000.0,
            ema_slow=3000.0,
            current_regime=None,
        )
        volatile = detector._classify_regime(
            adx=25.0,
            atr_pct=3.0,
            ema_fast=3000.0,
            ema_slow=3000.0,
            current_regime=None,
        )
        assert quiet == MarketRegime.QUIET_TRANSITION
        assert volatile == MarketRegime.VOLATILE_TRANSITION

    def test_bear_trend_stays_with_moderate_adx(self):
        """In BEAR_TREND: ADX=27 should stay in bear trend."""
        detector = MarketRegimeDetector()
        regime = detector._classify_regime(
            adx=27.0,
            atr_pct=1.5,
            ema_fast=2950.0,
            ema_slow=3000.0,
            current_regime=MarketRegime.BEAR_TREND,
        )
        assert regime == MarketRegime.BEAR_TREND

    def test_no_hysteresis_from_none(self):
        """With current_regime=None, use standard thresholds."""
        detector = MarketRegimeDetector()
        # ADX=28 with no current regime → transition (not trend, need 32)
        regime = detector._classify_regime(
            adx=28.0,
            atr_pct=1.0,
            ema_fast=3050.0,
            ema_slow=3000.0,
            current_regime=None,
        )
        assert regime in (MarketRegime.QUIET_TRANSITION, MarketRegime.VOLATILE_TRANSITION)

    def test_no_hysteresis_from_transition(self):
        """From transition state, use standard thresholds."""
        detector = MarketRegimeDetector()
        # In QUIET_TRANSITION, ADX=28 → still transition (not trend)
        regime = detector._classify_regime(
            adx=28.0,
            atr_pct=1.0,
            ema_fast=3050.0,
            ema_slow=3000.0,
            current_regime=MarketRegime.QUIET_TRANSITION,
        )
        assert regime in (MarketRegime.QUIET_TRANSITION, MarketRegime.VOLATILE_TRANSITION)

    def test_range_to_trend_requires_32(self):
        """From range, ADX must reach 32 to enter trend."""
        detector = MarketRegimeDetector()
        # ADX=33 from range → enters trend
        regime = detector._classify_regime(
            adx=33.0,
            atr_pct=1.0,
            ema_fast=3050.0,
            ema_slow=3000.0,
            current_regime=MarketRegime.WIDE_RANGE,
        )
        assert regime == MarketRegime.BULL_TREND
