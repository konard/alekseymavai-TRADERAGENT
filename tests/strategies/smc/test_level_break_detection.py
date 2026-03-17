"""
Tests for detect_level_break() — issue #398.

Covers:
  - support_break when price closes below a bull OB (support)
  - resistance_break when price closes above a bear OB (resistance)
  - no signal when price is within the current range
  - correctness of next_level (next OB/FVG below / above the broken level)
  - find_next_support / find_next_resistance helpers on SMCAnalyzer
  - structure_confidence scoring
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from bot.core.smc.analyzer import SMCAnalyzer
from bot.core.smc.models import (
    FVGType,
    FairValueGap,
    LiquidityLevel,
    LiquidityType,
    OBType,
    OrderBlock,
    SMCContext,
    SMCPhase,
    StructureEvent,
    StructureType,
    SwingPoint,
    SwingType,
)
from bot.strategies.smc.entry_signals import SMCSignal, SignalDirection
from bot.strategies.smc.smc_strategy import SMCStrategy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_df(
    n: int = 100,
    base: float = 45_000.0,
    seed: int = 42,
    volume: float = 500.0,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="5min")
    closes = base + np.cumsum(rng.normal(0, 50, n))
    highs = closes + rng.uniform(10, 100, n)
    lows = closes - rng.uniform(10, 100, n)
    opens = closes + rng.normal(0, 20, n)
    volumes = np.full(n, volume)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )


def _make_swing(index: int, price: float, is_high: bool) -> SwingPoint:
    return SwingPoint(
        index=index,
        price=price,
        swing_type=SwingType.HIGH if is_high else SwingType.LOW,
        strength=5,
    )


def _make_bull_ob(index: int, low: float, high: float) -> OrderBlock:
    return OrderBlock(
        index=index,
        ob_type=OBType.BULL,
        high=high,
        low=low,
        open=low,
        close=high,
        atr_at_formation=100.0,
        invalidated=False,
    )


def _make_bear_ob(index: int, low: float, high: float) -> OrderBlock:
    return OrderBlock(
        index=index,
        ob_type=OBType.BEAR,
        high=high,
        low=low,
        open=high,
        close=low,
        atr_at_formation=100.0,
        invalidated=False,
    )


def _make_bull_fvg(index: int, gap_low: float, gap_high: float) -> FairValueGap:
    return FairValueGap(
        index=index,
        fvg_type=FVGType.BULL,
        gap_high=gap_high,
        gap_low=gap_low,
        size=gap_high - gap_low,
        filled=False,
    )


def _make_bear_fvg(index: int, gap_low: float, gap_high: float) -> FairValueGap:
    return FairValueGap(
        index=index,
        fvg_type=FVGType.BEAR,
        gap_high=gap_high,
        gap_low=gap_low,
        size=gap_high - gap_low,
        filled=False,
    )


def _make_structure_event(index: int, bullish: bool) -> StructureEvent:
    stype = StructureType.BOS_BULL if bullish else StructureType.BOS_BEAR
    broken = _make_swing(index - 5, 44_000.0 if bullish else 46_000.0, is_high=not bullish)
    return StructureEvent(
        index=index,
        structure_type=stype,
        break_price=44_500.0 if bullish else 45_500.0,
        broken_swing=broken,
        impulse_size=500.0,
    )


def _make_context(
    order_blocks=None,
    fair_value_gaps=None,
    liquidity_levels=None,
    structure_events=None,
    current_price: float = 45_000.0,
    warmup_complete: bool = True,
) -> SMCContext:
    return SMCContext(
        order_blocks=order_blocks or [],
        fair_value_gaps=fair_value_gaps or [],
        liquidity_levels=liquidity_levels or [],
        structure_events=structure_events or [],
        phase=SMCPhase.BULL_TREND,
        trend_bias=0.5,
        current_price=current_price,
        warmup_complete=warmup_complete,
        bars_analyzed=200,
        bar_index=199,
    )


def _inject_context(strategy: SMCStrategy, ctx: SMCContext) -> None:
    """Inject a pre-built SMCContext into the strategy's market-structure analyser."""
    strategy.market_structure._smc_context = ctx


# ---------------------------------------------------------------------------
# Tests: find_next_support / find_next_resistance (SMCAnalyzer helpers)
# ---------------------------------------------------------------------------


class TestFindNextSupport:
    """Unit tests for SMCAnalyzer.find_next_support."""

    def setup_method(self):
        self.analyzer = SMCAnalyzer()

    def test_returns_bull_ob_high_below_price(self):
        ob = _make_bull_ob(10, low=44_000.0, high=44_500.0)
        ctx = _make_context(order_blocks=[ob], current_price=45_000.0)
        result = self.analyzer.find_next_support(ctx, 45_000.0)
        assert result == pytest.approx(44_500.0)

    def test_returns_nearest_when_multiple_obs(self):
        ob1 = _make_bull_ob(5, low=43_000.0, high=43_500.0)
        ob2 = _make_bull_ob(10, low=44_000.0, high=44_500.0)
        ctx = _make_context(order_blocks=[ob1, ob2], current_price=45_000.0)
        result = self.analyzer.find_next_support(ctx, 45_000.0)
        # ob2 is closer (highest high below price)
        assert result == pytest.approx(44_500.0)

    def test_ignores_invalidated_obs(self):
        ob_invalid = OrderBlock(
            index=10,
            ob_type=OBType.BULL,
            high=44_500.0,
            low=44_000.0,
            open=44_000.0,
            close=44_500.0,
            invalidated=True,
        )
        ctx = _make_context(order_blocks=[ob_invalid], current_price=45_000.0)
        result = self.analyzer.find_next_support(ctx, 45_000.0)
        assert result is None

    def test_returns_bull_fvg_gap_low(self):
        fvg = _make_bull_fvg(10, gap_low=44_200.0, gap_high=44_800.0)
        ctx = _make_context(fair_value_gaps=[fvg], current_price=45_000.0)
        result = self.analyzer.find_next_support(ctx, 45_000.0)
        assert result == pytest.approx(44_200.0)

    def test_returns_demand_liquidity_level(self):
        lv = LiquidityLevel(
            price=44_100.0,
            liquidity_type=LiquidityType.DEMAND,
            strength=0.7,
            swept=False,
        )
        ctx = _make_context(liquidity_levels=[lv], current_price=45_000.0)
        result = self.analyzer.find_next_support(ctx, 45_000.0)
        assert result == pytest.approx(44_100.0)

    def test_returns_none_when_no_levels(self):
        ctx = _make_context(current_price=45_000.0)
        result = self.analyzer.find_next_support(ctx, 45_000.0)
        assert result is None

    def test_ignores_swept_liquidity(self):
        lv = LiquidityLevel(
            price=44_100.0,
            liquidity_type=LiquidityType.EQL,
            strength=0.5,
            swept=True,
        )
        ctx = _make_context(liquidity_levels=[lv], current_price=45_000.0)
        result = self.analyzer.find_next_support(ctx, 45_000.0)
        assert result is None


class TestFindNextResistance:
    """Unit tests for SMCAnalyzer.find_next_resistance."""

    def setup_method(self):
        self.analyzer = SMCAnalyzer()

    def test_returns_bear_ob_low_above_price(self):
        ob = _make_bear_ob(10, low=45_500.0, high=46_000.0)
        ctx = _make_context(order_blocks=[ob], current_price=45_000.0)
        result = self.analyzer.find_next_resistance(ctx, 45_000.0)
        assert result == pytest.approx(45_500.0)

    def test_returns_nearest_when_multiple_obs(self):
        ob1 = _make_bear_ob(5, low=45_500.0, high=46_000.0)
        ob2 = _make_bear_ob(10, low=46_500.0, high=47_000.0)
        ctx = _make_context(order_blocks=[ob1, ob2], current_price=45_000.0)
        result = self.analyzer.find_next_resistance(ctx, 45_000.0)
        # ob1 is closer (lowest low above price)
        assert result == pytest.approx(45_500.0)

    def test_ignores_invalidated_obs(self):
        ob_invalid = OrderBlock(
            index=10,
            ob_type=OBType.BEAR,
            high=46_000.0,
            low=45_500.0,
            open=46_000.0,
            close=45_500.0,
            invalidated=True,
        )
        ctx = _make_context(order_blocks=[ob_invalid], current_price=45_000.0)
        result = self.analyzer.find_next_resistance(ctx, 45_000.0)
        assert result is None

    def test_returns_bear_fvg_gap_high(self):
        fvg = _make_bear_fvg(10, gap_low=45_200.0, gap_high=45_800.0)
        ctx = _make_context(fair_value_gaps=[fvg], current_price=45_000.0)
        result = self.analyzer.find_next_resistance(ctx, 45_000.0)
        assert result == pytest.approx(45_800.0)

    def test_returns_supply_liquidity_level(self):
        lv = LiquidityLevel(
            price=45_900.0,
            liquidity_type=LiquidityType.SUPPLY,
            strength=0.7,
            swept=False,
        )
        ctx = _make_context(liquidity_levels=[lv], current_price=45_000.0)
        result = self.analyzer.find_next_resistance(ctx, 45_000.0)
        assert result == pytest.approx(45_900.0)

    def test_returns_none_when_no_levels(self):
        ctx = _make_context(current_price=45_000.0)
        result = self.analyzer.find_next_resistance(ctx, 45_000.0)
        assert result is None

    def test_ignores_swept_liquidity(self):
        lv = LiquidityLevel(
            price=45_900.0,
            liquidity_type=LiquidityType.EQH,
            strength=0.5,
            swept=True,
        )
        ctx = _make_context(liquidity_levels=[lv], current_price=45_000.0)
        result = self.analyzer.find_next_resistance(ctx, 45_000.0)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: detect_level_break
# ---------------------------------------------------------------------------


class TestDetectLevelBreakSupportBreak:
    """Price closes below a bull OB → support_break."""

    def _strategy_with_support(self, support_high: float = 45_000.0) -> SMCStrategy:
        strategy = SMCStrategy()
        ob = _make_bull_ob(10, low=support_high - 500.0, high=support_high)
        ctx = _make_context(order_blocks=[ob], current_price=support_high + 200.0)
        _inject_context(strategy, ctx)
        return strategy, support_high

    def _df_closing_below(self, support_high: float, close_delta: float = 100.0) -> pd.DataFrame:
        """Return a 3-bar M5 dataframe whose last candle closes below support_high."""
        n = 3
        dates = pd.date_range("2024-01-01", periods=n, freq="5min")
        close_val = support_high - close_delta
        df = pd.DataFrame(
            {
                "open": [support_high + 50] * n,
                "high": [support_high + 100] * n,
                "low": [support_high - 200, support_high - 200, support_high - 150],
                "close": [support_high + 20, support_high - 10, close_val],
                "volume": [500.0] * n,
            },
            index=dates,
        )
        return df

    def test_returns_signal_on_support_break(self):
        strategy, support_high = self._strategy_with_support(45_000.0)
        df = self._df_closing_below(support_high)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert signal.event_type == "support_break"

    def test_broken_level_equals_support(self):
        strategy, support_high = self._strategy_with_support(45_000.0)
        df = self._df_closing_below(support_high)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert float(signal.broken_level) == pytest.approx(support_high)

    def test_break_extreme_is_last_candle_low(self):
        strategy, support_high = self._strategy_with_support(45_000.0)
        df = self._df_closing_below(support_high)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert float(signal.break_extreme) == pytest.approx(float(df["low"].iloc[-1]))

    def test_direction_is_short(self):
        strategy, support_high = self._strategy_with_support(45_000.0)
        df = self._df_closing_below(support_high)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert signal.direction == SignalDirection.SHORT

    def test_next_level_below_broken_level(self):
        """If a second bull OB sits below the broken level, next_level should point to it."""
        strategy = SMCStrategy()
        ob_support = _make_bull_ob(10, low=44_500.0, high=45_000.0)  # broken support
        ob_next = _make_bull_ob(5, low=43_000.0, high=43_500.0)       # next support below
        ctx = _make_context(order_blocks=[ob_support, ob_next], current_price=45_200.0)
        _inject_context(strategy, ctx)

        df = self._df_closing_below(45_000.0)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert signal.next_level is not None
        assert float(signal.next_level) < 45_000.0

    def test_structure_confidence_is_float_in_range(self):
        strategy, support_high = self._strategy_with_support(45_000.0)
        df = self._df_closing_below(support_high)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert 0.0 <= signal.structure_confidence <= 1.0


class TestDetectLevelBreakResistanceBreak:
    """Price closes above a bear OB → resistance_break."""

    def _strategy_with_resistance(self, resistance_low: float = 45_000.0) -> SMCStrategy:
        strategy = SMCStrategy()
        ob = _make_bear_ob(10, low=resistance_low, high=resistance_low + 500.0)
        ctx = _make_context(order_blocks=[ob], current_price=resistance_low - 200.0)
        _inject_context(strategy, ctx)
        return strategy, resistance_low

    def _df_closing_above(self, resistance_low: float, close_delta: float = 100.0) -> pd.DataFrame:
        """Return a 3-bar M5 dataframe whose last candle closes above resistance_low."""
        n = 3
        dates = pd.date_range("2024-01-01", periods=n, freq="5min")
        close_val = resistance_low + close_delta
        df = pd.DataFrame(
            {
                "open": [resistance_low - 50] * n,
                "high": [resistance_low + 200, resistance_low + 200, resistance_low + 150],
                "low": [resistance_low - 100] * n,
                "close": [resistance_low - 20, resistance_low + 10, close_val],
                "volume": [500.0] * n,
            },
            index=dates,
        )
        return df

    def test_returns_signal_on_resistance_break(self):
        strategy, resistance_low = self._strategy_with_resistance(45_000.0)
        df = self._df_closing_above(resistance_low)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert signal.event_type == "resistance_break"

    def test_broken_level_equals_resistance(self):
        strategy, resistance_low = self._strategy_with_resistance(45_000.0)
        df = self._df_closing_above(resistance_low)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert float(signal.broken_level) == pytest.approx(resistance_low)

    def test_break_extreme_is_last_candle_high(self):
        strategy, resistance_low = self._strategy_with_resistance(45_000.0)
        df = self._df_closing_above(resistance_low)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert float(signal.break_extreme) == pytest.approx(float(df["high"].iloc[-1]))

    def test_direction_is_long(self):
        strategy, resistance_low = self._strategy_with_resistance(45_000.0)
        df = self._df_closing_above(resistance_low)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    def test_next_level_above_broken_level(self):
        """If a second bear OB sits above the broken level, next_level should point to it."""
        strategy = SMCStrategy()
        ob_resistance = _make_bear_ob(10, low=45_000.0, high=45_500.0)  # broken resistance
        ob_next = _make_bear_ob(5, low=46_500.0, high=47_000.0)          # next resistance above
        ctx = _make_context(order_blocks=[ob_resistance, ob_next], current_price=44_800.0)
        _inject_context(strategy, ctx)

        df = self._df_closing_above(45_000.0)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        assert signal.next_level is not None
        assert float(signal.next_level) > 45_000.0

    def test_structure_confidence_boosted_by_bull_event(self):
        """Confidence should increase when last structural event is bullish."""
        strategy = SMCStrategy()
        ob = _make_bear_ob(10, low=45_000.0, high=45_500.0)
        bull_event = _make_structure_event(50, bullish=True)
        ctx = _make_context(
            order_blocks=[ob],
            structure_events=[bull_event],
            current_price=44_800.0,
        )
        _inject_context(strategy, ctx)

        df = self._df_closing_above(45_000.0, close_delta=200.0)
        signal = strategy.detect_level_break(df, Decimal(str(df["close"].iloc[-1])))
        assert signal is not None
        # At minimum the structural event contributed 0.3 to confidence
        assert signal.structure_confidence >= 0.3


class TestDetectLevelBreakNoSignal:
    """Price is within the current range → no signal returned."""

    def test_no_signal_when_price_inside_range(self):
        strategy = SMCStrategy()
        # Support at 44_000, resistance at 46_000 — current price 45_000 (inside)
        ob_support = _make_bull_ob(5, low=43_500.0, high=44_000.0)
        ob_resist = _make_bear_ob(10, low=46_000.0, high=46_500.0)
        ctx = _make_context(
            order_blocks=[ob_support, ob_resist],
            current_price=45_000.0,
        )
        _inject_context(strategy, ctx)

        # Build a dataframe whose last close is between support and resistance
        n = 3
        dates = pd.date_range("2024-01-01", periods=n, freq="5min")
        df = pd.DataFrame(
            {
                "open": [45_050.0] * n,
                "high": [45_200.0] * n,
                "low": [44_800.0] * n,
                "close": [45_000.0, 45_100.0, 45_050.0],
                "volume": [500.0] * n,
            },
            index=dates,
        )
        signal = strategy.detect_level_break(df, Decimal("45050"))
        assert signal is None

    def test_no_signal_when_no_levels_exist(self):
        strategy = SMCStrategy()
        ctx = _make_context(current_price=45_000.0)
        _inject_context(strategy, ctx)
        df = _make_df(n=10, base=45_000.0)
        signal = strategy.detect_level_break(df, Decimal("45000"))
        assert signal is None

    def test_no_signal_on_empty_dataframe(self):
        strategy = SMCStrategy()
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        signal = strategy.detect_level_break(df, Decimal("45000"))
        assert signal is None

    def test_no_signal_on_single_row_dataframe(self):
        strategy = SMCStrategy()
        dates = pd.date_range("2024-01-01", periods=1, freq="5min")
        df = pd.DataFrame(
            {"open": [45_000.0], "high": [45_100.0], "low": [44_900.0], "close": [45_000.0], "volume": [500.0]},
            index=dates,
        )
        signal = strategy.detect_level_break(df, Decimal("45000"))
        assert signal is None


class TestSMCSignalStructuralFields:
    """Verify the new SMCSignal fields exist and have correct defaults."""

    def _make_minimal_signal(self, **overrides) -> SMCSignal:
        from bot.strategies.smc.entry_signals import PatternType, PriceActionPattern

        pattern = PriceActionPattern(
            pattern_type=PatternType.ENGULFING,
            is_bullish=True,
            index=0,
            timestamp=pd.Timestamp("2024-01-01"),
            open=Decimal("45000"),
            high=Decimal("45100"),
            low=Decimal("44900"),
            close=Decimal("45050"),
            quality_score=75.0,
            confidence=0.75,
        )
        kwargs = dict(
            timestamp=pd.Timestamp("2024-01-01"),
            direction=SignalDirection.LONG,
            entry_price=Decimal("45050"),
            stop_loss=Decimal("44900"),
            take_profit=Decimal("45500"),
            pattern=pattern,
            confidence=0.75,
            risk_reward_ratio=3.0,
        )
        kwargs.update(overrides)
        return SMCSignal(**kwargs)

    def test_default_event_type_is_entry(self):
        sig = self._make_minimal_signal()
        assert sig.event_type == "entry"

    def test_default_broken_level_is_none(self):
        sig = self._make_minimal_signal()
        assert sig.broken_level is None

    def test_default_break_extreme_is_none(self):
        sig = self._make_minimal_signal()
        assert sig.break_extreme is None

    def test_default_next_level_is_none(self):
        sig = self._make_minimal_signal()
        assert sig.next_level is None

    def test_default_structure_confidence_is_zero(self):
        sig = self._make_minimal_signal()
        assert sig.structure_confidence == 0.0

    def test_structural_fields_can_be_set(self):
        sig = self._make_minimal_signal(
            event_type="support_break",
            broken_level=Decimal("44500"),
            break_extreme=Decimal("44400"),
            next_level=Decimal("43000"),
            structure_confidence=0.72,
        )
        assert sig.event_type == "support_break"
        assert sig.broken_level == Decimal("44500")
        assert sig.break_extreme == Decimal("44400")
        assert sig.next_level == Decimal("43000")
        assert sig.structure_confidence == pytest.approx(0.72)
