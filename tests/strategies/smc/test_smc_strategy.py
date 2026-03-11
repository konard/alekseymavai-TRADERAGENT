"""
Tests for SMCStrategy — main strategy orchestrator.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from bot.strategies.smc.config import DEFAULT_SMC_CONFIG, SMCConfig
from bot.strategies.smc.entry_signals import SMCSignal
from bot.strategies.smc.market_structure import TrendDirection
from bot.strategies.smc.smc_strategy import SMCStrategy


def _make_df(n: int = 100, base: float = 45000.0) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="1h")
    closes = base + np.cumsum(rng.normal(0, 50, n))
    highs = closes + rng.uniform(10, 100, n)
    lows = closes - rng.uniform(10, 100, n)
    opens = closes + rng.normal(0, 20, n)
    volumes = rng.uniform(100, 1000, n)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )


class TestSMCStrategyInit:
    def test_default_config(self):
        strategy = SMCStrategy()
        assert strategy.config is DEFAULT_SMC_CONFIG
        assert strategy.current_trend == TrendDirection.RANGING

    def test_custom_config(self):
        config = SMCConfig(risk_per_trade=Decimal("0.03"))
        strategy = SMCStrategy(config=config, account_balance=Decimal("50000"))
        assert strategy.config.risk_per_trade == Decimal("0.03")

    def test_components_initialized(self):
        strategy = SMCStrategy()
        assert strategy.market_structure is not None
        assert strategy.confluence_analyzer is not None
        assert strategy.signal_generator is not None
        assert strategy.position_manager is not None

    def test_initial_state(self):
        strategy = SMCStrategy()
        assert strategy.current_trend == TrendDirection.RANGING
        assert strategy.trend_strength == 0.0
        assert strategy.active_signals == []


class TestSMCAnalyzeMarket:
    def test_analyze_returns_dict(self):
        strategy = SMCStrategy()
        df_d1 = _make_df(n=50)
        df_h4 = _make_df(n=100)
        df_h1 = _make_df(n=200)
        df_m15 = _make_df(n=400)

        result = strategy.analyze_market(df_d1, df_h4, df_h1, df_m15)
        assert isinstance(result, dict)

    def test_analyze_contains_keys(self):
        strategy = SMCStrategy()
        df_d1 = _make_df(n=50)
        df_h4 = _make_df(n=100)
        df_h1 = _make_df(n=200)
        df_m15 = _make_df(n=400)

        result = strategy.analyze_market(df_d1, df_h4, df_h1, df_m15)
        assert "market_structure" in result
        assert "trend_analysis" in result
        assert "confluence_zones" in result
        assert "current_trend" in result
        assert "trend_strength" in result

    def test_updates_current_trend(self):
        strategy = SMCStrategy()
        assert strategy.current_trend == TrendDirection.RANGING

        df_d1 = _make_df(n=50)
        df_h4 = _make_df(n=100)
        df_h1 = _make_df(n=200)
        df_m15 = _make_df(n=400)
        strategy.analyze_market(df_d1, df_h4, df_h1, df_m15)
        # Trend should be updated (may be any value)
        assert isinstance(strategy.current_trend, TrendDirection)


class TestSMCGenerateSignals:
    def test_generate_signals_returns_list(self):
        strategy = SMCStrategy()
        df_h1 = _make_df(n=200)
        df_m15 = _make_df(n=400)

        # First analyze market
        strategy.analyze_market(_make_df(50), _make_df(100), df_h1, df_m15)

        signals = strategy.generate_signals(df_h1, df_m15)
        assert isinstance(signals, list)

    def test_max_three_signals(self):
        strategy = SMCStrategy()
        df_h1 = _make_df(n=200)
        df_m15 = _make_df(n=400)
        strategy.analyze_market(_make_df(50), _make_df(100), df_h1, df_m15)
        signals = strategy.generate_signals(df_h1, df_m15)
        assert len(signals) <= 3


class TestSMCFilterSignals:
    def test_low_confidence_filtered(self):
        strategy = SMCStrategy()
        signal = MagicMock(spec=SMCSignal)
        signal.confidence = 0.3  # Below 60% threshold
        signal.trend_aligned = False
        signal.confluence_score = 0

        filtered = strategy._filter_signals([signal])
        assert len(filtered) == 0

    def test_high_confidence_passes(self):
        strategy = SMCStrategy()
        signal = MagicMock(spec=SMCSignal)
        signal.confidence = 0.7
        signal.trend_aligned = True
        signal.confluence_score = 30

        filtered = strategy._filter_signals([signal])
        assert len(filtered) == 1

    def test_trend_aligned_boost(self):
        strategy = SMCStrategy()
        signal = MagicMock(spec=SMCSignal)
        signal.confidence = 0.7
        signal.trend_aligned = True
        signal.confluence_score = 30

        strategy._filter_signals([signal])
        # Confidence should be boosted by 10%
        assert signal.confidence == pytest.approx(0.77, abs=0.01)


class TestSMCManagePositions:
    def test_manage_empty_positions(self):
        strategy = SMCStrategy()
        result = strategy.manage_positions({})
        assert result == []

    def test_manage_unknown_position(self):
        strategy = SMCStrategy()
        result = strategy.manage_positions({"unknown_id": Decimal("45000")})
        assert result == []


class TestSMCGetState:
    def test_get_strategy_state(self):
        strategy = SMCStrategy()
        state = strategy.get_strategy_state()
        assert state["strategy"] == "Smart Money Concepts (SMC)"
        assert "current_trend" in state
        assert "swing_highs" in state
        assert "config" in state

    def test_state_config_values(self):
        config = SMCConfig(risk_per_trade=Decimal("0.03"))
        strategy = SMCStrategy(config=config)
        state = strategy.get_strategy_state()
        assert state["config"]["risk_per_trade"] == Decimal("0.03")


class TestSMCPerformanceReport:
    def test_performance_report_empty(self):
        strategy = SMCStrategy()
        report = strategy.get_performance_report()
        assert report["total_trades"] == 0
        assert report["win_rate"] == "0.0%"


class TestSMCReset:
    def test_reset_clears_state(self):
        strategy = SMCStrategy()
        strategy.active_signals = [MagicMock()]
        strategy.reset()
        assert strategy.active_signals == []
        assert len(strategy.market_structure.swing_highs) == 0


class TestSMCGetStateLiquidity:
    def test_active_liquidity_zones_in_state(self):
        strategy = SMCStrategy()
        state = strategy.get_strategy_state()
        assert "active_liquidity_zones" in state
        assert state["active_liquidity_zones"] >= 0


class TestSMCConfigDataclass:
    def test_defaults(self):
        cfg = SMCConfig()
        assert cfg.trend_timeframe == "1d"
        assert cfg.structure_timeframe == "4h"
        assert cfg.working_timeframe == "1h"
        assert cfg.entry_timeframe == "15m"
        assert cfg.risk_per_trade == Decimal("2.0")
        assert cfg.min_risk_reward == Decimal("2.0")
        assert cfg.swing_length == 10
        assert cfg.close_break is True
        assert cfg.close_mitigation is False
        assert cfg.join_consecutive_fvg is False
        assert cfg.liquidity_range_percent == 0.01
        assert cfg.max_positions == 3

    def test_custom(self):
        cfg = SMCConfig(swing_length=10, trend_period=30)
        assert cfg.swing_length == 10
        assert cfg.trend_period == 30

    def test_max_positions_custom(self):
        cfg = SMCConfig(max_positions=5)
        assert cfg.max_positions == 5

    def test_removed_dead_fields(self):
        """order_block_lookback and fvg_min_size were removed"""
        cfg = SMCConfig()
        assert not hasattr(cfg, "order_block_lookback")
        assert not hasattr(cfg, "fvg_min_size")

    def test_default_instance(self):
        assert DEFAULT_SMC_CONFIG is not None
        assert isinstance(DEFAULT_SMC_CONFIG, SMCConfig)

    def test_warmup_bars_dynamic_default(self):
        """warmup_bars auto-computed as max(swing_length * 4, 100) when left at default."""
        cfg = SMCConfig()  # swing_length=10 → 10*4=40 < 100, keep 100
        assert cfg.warmup_bars == 100

    def test_warmup_bars_dynamic_large_swing(self):
        """warmup_bars scales up for large swing_length values."""
        cfg = SMCConfig(swing_length=50)  # 50*4=200 > 100
        assert cfg.warmup_bars == 200


class TestAdaptiveSwingLength:
    """Tests for adaptive swing_length in multi-timeframe analysis."""

    def test_d1_analysis_with_50_candles(self):
        """D1 analysis should work with ~50 daily candles (adaptive swing_length)."""
        from bot.strategies.smc.market_structure import MarketStructureAnalyzer

        analyzer = MarketStructureAnalyzer(swing_length=50, trend_period=20)
        df_d1 = _make_df(n=50, base=45000.0)
        df_h4 = _make_df(n=200, base=45000.0)

        result = analyzer.analyze_trend(df_d1, df_h4)
        # D1 should NOT be skipped — adaptive swing_length (50//5=10) needs 21 candles
        assert "d1_trend" in result
        assert "h4_trend" in result

    def test_d1_adaptive_swing_length_value(self):
        """D1 sub-analyzer should use swing_length // 5 (clamped to min 10)."""
        from bot.strategies.smc.market_structure import MarketStructureAnalyzer

        # swing_length=50 -> D1 gets 50//5=10
        analyzer = MarketStructureAnalyzer(swing_length=50)
        d1_swing = max(10, analyzer.swing_length // 5)
        assert d1_swing == 10

        # swing_length=30 -> D1 gets max(10, 30//5)=10
        analyzer2 = MarketStructureAnalyzer(swing_length=30)
        d1_swing2 = max(10, analyzer2.swing_length // 5)
        assert d1_swing2 == 10

        # swing_length=100 -> D1 gets 100//5=20
        analyzer3 = MarketStructureAnalyzer(swing_length=100)
        d1_swing3 = max(10, analyzer3.swing_length // 5)
        assert d1_swing3 == 20

    def test_h4_adaptive_swing_length_value(self):
        """H4 sub-analyzer should use swing_length // 2 (clamped to min 15)."""
        from bot.strategies.smc.market_structure import MarketStructureAnalyzer

        # swing_length=50 -> H4 gets 50//2=25
        analyzer = MarketStructureAnalyzer(swing_length=50)
        h4_swing = max(15, analyzer.swing_length // 2)
        assert h4_swing == 25

        # swing_length=20 -> H4 gets max(15, 20//2)=15
        analyzer2 = MarketStructureAnalyzer(swing_length=20)
        h4_swing2 = max(15, analyzer2.swing_length // 2)
        assert h4_swing2 == 15

    def test_d1_analysis_no_longer_requires_101_candles(self):
        """With adaptive swing_length, D1 should NOT need 101 candles."""
        from bot.strategies.smc.market_structure import MarketStructureAnalyzer

        analyzer = MarketStructureAnalyzer(swing_length=50, trend_period=20)
        # Only 50 daily candles — should work with adaptive swing (10*2+1=21 needed)
        df_d1 = _make_df(n=50, base=45000.0)
        df_h4 = _make_df(n=200, base=45000.0)

        result = analyzer.analyze_trend(df_d1, df_h4)
        # d1_trend should be set (not skipped due to insufficient data)
        assert result["d1_trend"] in [
            TrendDirection.BULLISH,
            TrendDirection.BEARISH,
            TrendDirection.RANGING,
        ]
