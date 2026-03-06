"""
Integration tests for bot/core/smc/analyzer.py
"""

import numpy as np
import pandas as pd

from bot.core.smc.analyzer import SMCAnalyzer
from bot.core.smc.models import SMCContext, SMCPhase

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_df(n: int, trend: str = "bull") -> pd.DataFrame:
    """
    Generate a synthetic OHLCV DataFrame.

    trend='bull'  → slow upward drift with noise
    trend='bear'  → slow downward drift
    trend='flat'  → sideways
    """
    rng = np.random.default_rng(42)
    base = 1000.0

    if trend == "bull":
        close = base + np.arange(n) * 0.5 + rng.normal(0, 3, n).cumsum()
    elif trend == "bear":
        close = base + np.arange(n) * (-0.5) + rng.normal(0, 3, n).cumsum()
    else:
        close = base + rng.normal(0, 3, n).cumsum()

    close = np.clip(close, 10.0, None)
    high = close + rng.uniform(1, 5, n)
    low = close - rng.uniform(1, 5, n)
    low = np.clip(low, 1.0, None)
    open_ = close + rng.normal(0, 2, n)

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1000, 5000, n),
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSMCAnalyzer:
    def setup_method(self):
        self.analyzer = SMCAnalyzer(swing_strength=5, min_warmup_bars=50)

    def test_returns_smc_context(self):
        df = _make_df(100)
        ctx = self.analyzer.analyze(df)
        assert isinstance(ctx, SMCContext)

    def test_warmup_false_when_insufficient_bars(self):
        df = _make_df(30)
        ctx = self.analyzer.analyze(df)
        assert not ctx.warmup_complete

    def test_warmup_true_after_enough_bars(self):
        df = _make_df(200)
        ctx = self.analyzer.analyze(df)
        assert ctx.warmup_complete

    def test_current_price_matches_last_close(self):
        df = _make_df(100)
        ctx = self.analyzer.analyze(df)
        assert abs(ctx.current_price - float(df["close"].iloc[-1])) < 1e-6

    def test_bar_index_is_last(self):
        df = _make_df(100)
        ctx = self.analyzer.analyze(df)
        assert ctx.bar_index == 99

    def test_phase_is_valid_enum(self):
        df = _make_df(200)
        ctx = self.analyzer.analyze(df)
        assert ctx.phase in list(SMCPhase)

    def test_trend_bias_in_range(self):
        df = _make_df(200)
        ctx = self.analyzer.analyze(df)
        assert -1.0 <= ctx.trend_bias <= 1.0

    def test_bull_trend_has_positive_bias(self):
        df = _make_df(500, trend="bull")
        ctx = self.analyzer.analyze(df)
        # A clear bull trend should produce a non-negative bias
        assert ctx.trend_bias >= 0.0

    def test_bear_trend_has_negative_bias(self):
        df = _make_df(500, trend="bear")
        ctx = self.analyzer.analyze(df)
        assert ctx.trend_bias <= 0.0

    def test_empty_df_returns_default_context(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        ctx = self.analyzer.analyze(df)
        assert not ctx.warmup_complete
        assert ctx.current_price == 0.0

    def test_swing_highs_and_lows_populated(self):
        df = _make_df(200)
        ctx = self.analyzer.analyze(df)
        assert len(ctx.swing_highs) > 0 or len(ctx.swing_lows) > 0

    def test_structure_events_most_recent_first(self):
        df = _make_df(300, trend="bull")
        ctx = self.analyzer.analyze(df)
        if len(ctx.structure_events) >= 2:
            assert ctx.structure_events[0].index >= ctx.structure_events[1].index

    def test_to_dict_has_required_keys(self):
        df = _make_df(100)
        ctx = self.analyzer.analyze(df)
        d = ctx.to_dict()
        required = [
            "phase",
            "trend_bias",
            "warmup_complete",
            "bars_analyzed",
            "swing_highs",
            "swing_lows",
            "structure_events",
            "order_blocks",
            "fair_value_gaps",
            "liquidity_levels",
        ]
        for key in required:
            assert key in d, f"Missing key: {key}"

    def test_nearest_bull_ob_below_price(self):
        df = _make_df(300, trend="bull")
        ctx = self.analyzer.analyze(df)
        price = ctx.current_price
        ob = ctx.nearest_bull_ob(price)
        if ob is not None:
            assert ob.high <= price * 1.001

    def test_nearest_bear_ob_above_price(self):
        df = _make_df(300, trend="bear")
        ctx = self.analyzer.analyze(df)
        price = ctx.current_price
        ob = ctx.nearest_bear_ob(price)
        if ob is not None:
            assert ob.low >= price * 0.999

    def test_bars_analyzed_matches_df_length(self):
        df = _make_df(150)
        ctx = self.analyzer.analyze(df)
        assert ctx.bars_analyzed == 150

    def test_precomputed_atr_used(self):
        """If df has an 'atr' column, it should be used without errors."""
        df = _make_df(200)
        df["atr"] = df["high"] - df["low"]
        ctx = self.analyzer.analyze(df)
        assert isinstance(ctx, SMCContext)
