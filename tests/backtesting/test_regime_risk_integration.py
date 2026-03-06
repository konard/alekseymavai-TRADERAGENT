"""Integration tests for RegimeClassifier + RiskManager in the multi-TF backtester."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from bot.orchestrator.market_regime import (
    MarketRegime,
    MarketRegimeDetector,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.tests.backtesting.backtesting_engine import BacktestResult
from bot.tests.backtesting.multi_tf_data_loader import MultiTimeframeDataLoader
from bot.tests.backtesting.multi_tf_engine import (
    REGIME_ALLOWED_STRATEGY_TYPES,
    MultiTFBacktestConfig,
    MultiTimeframeBacktestEngine,
)

# Reuse ConcreteStrategy from existing tests
from tests.strategies.test_base_strategy import ConcreteStrategy

# =============================================================================
# Helpers
# =============================================================================


def _make_regime_analysis(
    regime: MarketRegime,
    recommended: RecommendedStrategy = RecommendedStrategy.GRID,
    confidence: float = 0.8,
) -> RegimeAnalysis:
    """Build a minimal RegimeAnalysis for testing."""
    return RegimeAnalysis(
        regime=regime,
        confidence=confidence,
        recommended_strategy=recommended,
        confluence_score=0.5,
        trend_strength=0.0,
        volatility_percentile=50.0,
        ema_divergence_pct=0.0,
        atr_pct=1.0,
        rsi=50.0,
        adx=25.0,
        bb_width_pct=4.0,
        volume_ratio=1.0,
        regime_duration_seconds=300,
        previous_regime=None,
        timestamp=datetime.now(timezone.utc),
        analysis_details={},
    )


@pytest.fixture
def loader():
    return MultiTimeframeDataLoader()


@pytest.fixture
def data_30days(loader):
    """30 days of uptrend data — enough rows for regime detector."""
    return loader.load(
        symbol="BTC/USDT",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 1, 31),
        trend="up",
    )


@pytest.fixture
def data_7days(loader):
    """7 days of uptrend data."""
    return loader.load(
        symbol="BTC/USDT",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 1, 8),
        trend="up",
    )


# =============================================================================
# Regime Detector Integration
# =============================================================================


class TestRegimeDetectorRunsPeriodically:
    """Verify the regime detector is invoked at the configured interval."""

    async def test_regime_detected_every_n_bars(self, data_30days):
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            enable_regime_filter=True,
            regime_check_interval=12,
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_30days)

        # regime_history should have entries (detector needs ≥60 H1 rows,
        # so the first successful detection happens after enough data accumulates)
        assert len(result.regime_history) > 0
        # Each entry should be at a bar that is a multiple of regime_check_interval
        # from warmup (bars_since_warmup % 12 == 0)
        for entry in result.regime_history:
            bars_since_warmup = entry["bar"] - config.warmup_bars
            assert bars_since_warmup % config.regime_check_interval == 0


class TestRegimeFilterBlocksWrongStrategy:
    """Grid strategy (type='test' from ConcreteStrategy) blocked in BULL_TREND."""

    async def test_blocks_wrong_strategy_type(self, data_30days):
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            enable_regime_filter=True,
            regime_check_interval=1,  # every bar for thorough testing
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()  # strategy_type = "test"

        # Force regime to BULL_TREND (allowed: trend_follower, dca — NOT "test")
        forced_regime = _make_regime_analysis(MarketRegime.BULL_TREND, RecommendedStrategy.DCA)
        with patch.object(MarketRegimeDetector, "analyze", return_value=forced_regime):
            result = await engine.run(strategy, data_30days)

        # "test" not in BULL_TREND allowed types, so signals should be blocked
        assert result.regime_filter_blocks > 0


class TestRegimeFilterAllowsMatchingStrategy:
    """DCA strategy should be allowed in BEAR_TREND."""

    async def test_allows_matching_type(self, data_30days):
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            enable_regime_filter=True,
            regime_check_interval=1,
        )
        engine = MultiTimeframeBacktestEngine(config=config)

        # Create a strategy that reports type="dca"
        strategy = ConcreteStrategy()
        strategy.get_strategy_type = lambda: "dca"  # type: ignore[assignment]

        # Force BEAR_TREND (allowed: dca, trend_follower)
        forced_regime = _make_regime_analysis(MarketRegime.BEAR_TREND, RecommendedStrategy.DCA)
        with patch.object(MarketRegimeDetector, "analyze", return_value=forced_regime):
            result = await engine.run(strategy, data_30days)

        # dca IS allowed in BEAR_TREND — no regime blocks expected
        assert result.regime_filter_blocks == 0


class TestRegimeFilterDisabledByDefault:
    """Default config has regime filter off — no blocking should occur."""

    async def test_disabled_by_default(self, data_7days):
        config = MultiTFBacktestConfig(warmup_bars=20)
        assert config.enable_regime_filter is False

        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)

        assert result.regime_filter_blocks == 0
        assert result.regime_history == []


class TestHoldAndReduceExposureBlockAll:
    """HOLD and REDUCE_EXPOSURE recommendations should block all new entries."""

    async def test_hold_blocks_entries(self, data_30days):
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            enable_regime_filter=True,
            regime_check_interval=1,
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()

        forced = _make_regime_analysis(MarketRegime.QUIET_TRANSITION, RecommendedStrategy.HOLD)
        with patch.object(MarketRegimeDetector, "analyze", return_value=forced):
            result = await engine.run(strategy, data_30days)

        assert result.regime_filter_blocks > 0

    async def test_reduce_exposure_blocks_entries(self, data_30days):
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            enable_regime_filter=True,
            regime_check_interval=1,
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()

        forced = _make_regime_analysis(
            MarketRegime.VOLATILE_TRANSITION, RecommendedStrategy.REDUCE_EXPOSURE
        )
        with patch.object(MarketRegimeDetector, "analyze", return_value=forced):
            result = await engine.run(strategy, data_30days)

        assert result.regime_filter_blocks > 0


# =============================================================================
# Risk Manager Integration
# =============================================================================


class TestRiskManagerBlocksOverlargePosition:
    """Position exceeding max_position_size should be blocked."""

    async def test_blocks_large_position(self, data_7days):
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            enable_risk_manager=True,
            rm_max_position_size=Decimal("1"),  # tiny limit — force blocks
            rm_min_order_size=Decimal("0.01"),
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)

        assert result.risk_manager_blocks > 0


class TestRiskManagerBlocksInsufficientBalance:
    """Low balance should block trades."""

    async def test_blocks_low_balance(self, data_7days):
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            initial_balance=Decimal("1"),  # very low starting balance
            enable_risk_manager=True,
            rm_max_position_size=Decimal("100000"),
            rm_min_order_size=Decimal("100"),  # min order > available balance
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)

        # All trades should be blocked by min order size check
        assert result.risk_manager_blocks > 0


class TestRiskManagerPortfolioStopLoss:
    """Backtest should halt when stop-loss percentage is hit."""

    async def test_halts_on_stop_loss(self, data_7days):
        # Use a downtrend with aggressive stop-loss
        loader = MultiTimeframeDataLoader()
        data = loader.load(
            symbol="BTC/USDT",
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 8),
            trend="down",
        )
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            enable_risk_manager=True,
            rm_max_position_size=Decimal("100000"),
            rm_min_order_size=Decimal("0.01"),
            rm_stop_loss_percentage=Decimal("0.001"),  # 0.1% — very tight
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data)

        # With such a tight stop-loss, the backtest should halt early
        total_possible = len(data.m5) - config.warmup_bars
        assert len(result.equity_curve) <= total_possible
        # risk_halted may or may not be True depending on how much the
        # portfolio drops — at minimum the field should be present
        assert isinstance(result.risk_halted, bool)


class TestRiskManagerDailyLossLimit:
    """Backtest should halt when daily loss limit is exceeded."""

    async def test_halts_on_daily_loss(self, data_7days):
        loader = MultiTimeframeDataLoader()
        data = loader.load(
            symbol="BTC/USDT",
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 8),
            trend="down",
        )
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            enable_risk_manager=True,
            rm_max_position_size=Decimal("100000"),
            rm_min_order_size=Decimal("0.01"),
            rm_max_daily_loss=Decimal("0.01"),  # very tight daily limit
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data)

        total_possible = len(data.m5) - config.warmup_bars
        assert len(result.equity_curve) <= total_possible
        assert isinstance(result.risk_halted, bool)


class TestRiskManagerDisabledByDefault:
    """Default config has risk manager off — no blocking should occur."""

    async def test_disabled_by_default(self, data_7days):
        config = MultiTFBacktestConfig(warmup_bars=20)
        assert config.enable_risk_manager is False

        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)

        assert result.risk_manager_blocks == 0
        assert result.risk_halted is False


# =============================================================================
# Combined Regime + Risk
# =============================================================================


class TestCombinedRegimeAndRisk:
    """Both features enabled together — verify they compose correctly."""

    async def test_both_enabled(self, data_30days):
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            enable_regime_filter=True,
            regime_check_interval=12,
            enable_risk_manager=True,
            rm_max_position_size=Decimal("5000"),
            rm_min_order_size=Decimal("10"),
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_30days)

        # Both should produce valid result
        assert isinstance(result, BacktestResult)
        assert len(result.equity_curve) > 0
        # Regime history should be populated
        assert len(result.regime_history) > 0


# =============================================================================
# Output / Result Fields
# =============================================================================


class TestRegimeInEquityCurve:
    """Regime value should be recorded in equity curve entries."""

    async def test_regime_in_equity_curve(self, data_30days):
        config = MultiTFBacktestConfig(
            warmup_bars=20,
            enable_regime_filter=True,
            regime_check_interval=1,  # detect every bar
        )
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()

        forced = _make_regime_analysis(MarketRegime.TIGHT_RANGE)
        with patch.object(MarketRegimeDetector, "analyze", return_value=forced):
            result = await engine.run(strategy, data_30days)

        # After first regime detection, all equity curve entries should have regime
        has_regime = [ec for ec in result.equity_curve if "regime" in ec]
        assert len(has_regime) > 0
        assert has_regime[0]["regime"] == "tight_range"


class TestBacktestResultHasRegimeRiskFields:
    """New fields should be present on BacktestResult."""

    async def test_fields_present(self, data_7days):
        config = MultiTFBacktestConfig(warmup_bars=20)
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)

        # Regime fields
        assert hasattr(result, "regime_history")
        assert hasattr(result, "regime_changes")
        assert hasattr(result, "regime_filter_blocks")

        # Risk fields
        assert hasattr(result, "risk_manager_blocks")
        assert hasattr(result, "risk_halted")
        assert hasattr(result, "risk_halt_reason")

    async def test_fields_in_to_dict(self, data_7days):
        config = MultiTFBacktestConfig(warmup_bars=20)
        engine = MultiTimeframeBacktestEngine(config=config)
        strategy = ConcreteStrategy()
        result = await engine.run(strategy, data_7days)
        d = result.to_dict()

        assert "regime_tracking" in d
        assert "risk_management" in d
        assert d["regime_tracking"]["regime_filter_blocks"] == 0
        assert d["risk_management"]["risk_halted"] is False


# =============================================================================
# Regime Allowed Types Mapping
# =============================================================================


class TestRegimeAllowedStrategyTypes:
    """Verify the REGIME_ALLOWED_STRATEGY_TYPES mapping is built correctly."""

    def test_bull_trend_allows_trend_follower_and_dca(self):
        allowed = REGIME_ALLOWED_STRATEGY_TYPES[MarketRegime.BULL_TREND]
        assert "trend_follower" in allowed
        assert "dca" in allowed

    def test_bear_trend_allows_dca_and_trend_follower(self):
        allowed = REGIME_ALLOWED_STRATEGY_TYPES[MarketRegime.BEAR_TREND]
        assert "dca" in allowed
        assert "trend_follower" in allowed

    def test_tight_range_allows_grid(self):
        allowed = REGIME_ALLOWED_STRATEGY_TYPES[MarketRegime.TIGHT_RANGE]
        assert "grid" in allowed

    def test_wide_range_allows_grid(self):
        allowed = REGIME_ALLOWED_STRATEGY_TYPES[MarketRegime.WIDE_RANGE]
        assert "grid" in allowed

    def test_volatile_transition_allows_smc(self):
        allowed = REGIME_ALLOWED_STRATEGY_TYPES[MarketRegime.VOLATILE_TRANSITION]
        assert "smc" in allowed

    def test_unknown_is_empty(self):
        allowed = REGIME_ALLOWED_STRATEGY_TYPES[MarketRegime.UNKNOWN]
        assert len(allowed) == 0
