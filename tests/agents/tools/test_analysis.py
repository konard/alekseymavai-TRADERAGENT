from __future__ import annotations

import math
import random

import pytest

from bot.agents.tools.analysis import (
    BacktestSkill,
    CorrelationSkill,
    SentimentSkill,
    VolatilityForecastSkill,
)


# ======================================================================
# BacktestSkill
# ======================================================================

class TestBacktestSkill:
    def setup_method(self):
        self.skill = BacktestSkill()

    @pytest.mark.asyncio
    async def test_long_win(self):
        """Price rises through take-profit before hitting stop-loss."""
        result = await self.skill.execute({
            "strategy": "long",
            "entry_price": 3500,
            "stop_loss": 3450,
            "take_profit": 3600,
            "historical_prices": [3510, 3530, 3560, 3600, 3620],
        })
        assert result.success
        assert result.data["result"] == "win"
        assert result.data["pnl"] == 100.0
        assert result.data["bars_to_exit"] == 4

    @pytest.mark.asyncio
    async def test_long_loss(self):
        """Price falls through stop-loss."""
        result = await self.skill.execute({
            "strategy": "long",
            "entry_price": 3500,
            "stop_loss": 3450,
            "take_profit": 3600,
            "historical_prices": [3490, 3470, 3450, 3440],
        })
        assert result.success
        assert result.data["result"] == "loss"
        assert result.data["pnl"] == -50.0
        assert result.data["bars_to_exit"] == 3

    @pytest.mark.asyncio
    async def test_short_win(self):
        """Short trade hits take-profit below entry."""
        result = await self.skill.execute({
            "strategy": "short",
            "entry_price": 3500,
            "stop_loss": 3550,
            "take_profit": 3400,
            "historical_prices": [3490, 3460, 3400, 3380],
        })
        assert result.success
        assert result.data["result"] == "win"
        assert result.data["pnl"] == 100.0
        assert result.data["bars_to_exit"] == 3

    @pytest.mark.asyncio
    async def test_short_loss(self):
        """Short trade hits stop-loss above entry."""
        result = await self.skill.execute({
            "strategy": "short",
            "entry_price": 3500,
            "stop_loss": 3550,
            "take_profit": 3400,
            "historical_prices": [3510, 3530, 3550, 3560],
        })
        assert result.success
        assert result.data["result"] == "loss"
        assert result.data["pnl"] == -50.0

    @pytest.mark.asyncio
    async def test_risk_reward_calculation(self):
        """risk_reward = reward / risk."""
        result = await self.skill.execute({
            "strategy": "long",
            "entry_price": 100,
            "stop_loss": 90,
            "take_profit": 130,
            "historical_prices": [105, 130],
        })
        assert result.success
        # risk=10, reward=30 -> R:R=3.0
        assert result.data["risk_reward"] == 3.0

    @pytest.mark.asyncio
    async def test_monte_carlo_win_pct_range(self):
        """Monte Carlo win percentage is between 0 and 1."""
        random.seed(42)
        result = await self.skill.execute({
            "strategy": "long",
            "entry_price": 100,
            "stop_loss": 95,
            "take_profit": 110,
            "historical_prices": [98, 99, 101, 103, 105, 108, 110, 112],
        })
        assert result.success
        mc = result.data["monte_carlo_win_pct"]
        assert 0.0 <= mc <= 1.0

    @pytest.mark.asyncio
    async def test_open_result_when_neither_hit(self):
        """If neither SL nor TP is hit, result is 'open'."""
        result = await self.skill.execute({
            "strategy": "long",
            "entry_price": 100,
            "stop_loss": 80,
            "take_profit": 120,
            "historical_prices": [99, 101, 100, 102],
        })
        assert result.success
        assert result.data["result"] == "open"
        assert result.data["bars_to_exit"] == 4


# ======================================================================
# CorrelationSkill
# ======================================================================

class TestCorrelationSkill:
    def setup_method(self):
        self.skill = CorrelationSkill()

    @pytest.mark.asyncio
    async def test_perfect_positive(self):
        a = [0.01, 0.02, 0.03, 0.04, 0.05]
        b = [0.02, 0.04, 0.06, 0.08, 0.10]
        result = await self.skill.execute({"returns_a": a, "returns_b": b})
        assert result.success
        assert abs(result.data["correlation"] - 1.0) < 1e-4

    @pytest.mark.asyncio
    async def test_perfect_negative(self):
        a = [0.01, 0.02, 0.03, 0.04, 0.05]
        b = [-0.01, -0.02, -0.03, -0.04, -0.05]
        result = await self.skill.execute({"returns_a": a, "returns_b": b})
        assert result.success
        assert abs(result.data["correlation"] - (-1.0)) < 1e-4

    @pytest.mark.asyncio
    async def test_uncorrelated(self):
        """Alternating pattern gives near-zero correlation."""
        a = [0.01, -0.01, 0.01, -0.01, 0.01, -0.01]
        b = [0.01, 0.01, -0.01, -0.01, 0.01, 0.01]
        result = await self.skill.execute({"returns_a": a, "returns_b": b})
        assert result.success
        assert abs(result.data["correlation"]) < 0.5

    @pytest.mark.asyncio
    async def test_regime_detection_high(self):
        a = list(range(30))
        b = [x * 2 for x in a]
        result = await self.skill.execute({
            "returns_a": [float(x) for x in a],
            "returns_b": [float(x) for x in b],
        })
        assert result.data["regime"] == "high"

    @pytest.mark.asyncio
    async def test_rolling_correlation_length(self):
        """Rolling correlations list should have (n - window + 1) entries."""
        n = 50
        a = [float(i) for i in range(n)]
        b = [float(i * 2) for i in range(n)]
        result = await self.skill.execute({"returns_a": a, "returns_b": b})
        assert result.success
        # window=20, so rolling length = n - 20 + 1 = 31
        assert len(result.data["rolling_correlations"]) == n - 20 + 1


# ======================================================================
# VolatilityForecastSkill
# ======================================================================

class TestVolatilityForecastSkill:
    def setup_method(self):
        self.skill = VolatilityForecastSkill()

    @pytest.mark.asyncio
    async def test_constant_returns_zero_vol(self):
        """Constant zero returns should give near-zero vol."""
        result = await self.skill.execute({
            "returns": [0.0] * 30,
            "horizon": 5,
        })
        assert result.success
        assert result.data["current_vol"] < 1e-6

    @pytest.mark.asyncio
    async def test_high_vol_returns(self):
        """Large swings produce high volatility."""
        returns = [0.10, -0.10, 0.10, -0.10, 0.10, -0.10] * 5
        result = await self.skill.execute({"returns": returns, "horizon": 1})
        assert result.success
        assert result.data["current_vol"] > 0.01

    @pytest.mark.asyncio
    async def test_vol_regime_classification(self):
        """Extreme vol returns classify as 'extreme'."""
        returns = [0.5, -0.5, 0.5, -0.5] * 10
        result = await self.skill.execute({"returns": returns, "horizon": 1})
        assert result.success
        assert result.data["vol_regime"] in ("high", "extreme")

    @pytest.mark.asyncio
    async def test_horizon_effect(self):
        """Longer horizon should produce larger forecast vol."""
        returns = [0.01, -0.01, 0.02, -0.02, 0.01] * 5
        r1 = await self.skill.execute({"returns": returns, "horizon": 1})
        r5 = await self.skill.execute({"returns": returns, "horizon": 5})
        assert r5.data["forecast_vol"] > r1.data["forecast_vol"]

    @pytest.mark.asyncio
    async def test_low_vol_regime(self):
        """Small daily returns -> low annualised vol -> 'low' regime."""
        returns = [0.001, -0.001, 0.0005, -0.0005] * 20
        result = await self.skill.execute({"returns": returns, "horizon": 1})
        assert result.data["vol_regime"] == "low"


# ======================================================================
# SentimentSkill
# ======================================================================

class TestSentimentSkill:
    @pytest.mark.asyncio
    async def test_with_provider(self):
        def provider(symbol):
            return {
                "fear_greed_index": 5,
                "funding_rate": -0.001,
                "long_short_ratio": 0.8,
            }

        skill = SentimentSkill(sentiment_provider=provider)
        result = await skill.execute({"symbol": "ETHUSDT"})
        assert result.success
        assert result.data["fear_greed_index"] == 5
        assert result.data["sentiment"] == "extreme_fear"
        assert result.data["signal"] == "contrarian_buy"

    @pytest.mark.asyncio
    async def test_without_provider(self):
        skill = SentimentSkill()
        result = await skill.execute({"symbol": "BTCUSDT"})
        assert result.success
        assert result.data["fear_greed_index"] == 50
        assert result.data["sentiment"] == "neutral"
        assert result.data["signal"] == "neutral"

    @pytest.mark.asyncio
    async def test_contrarian_sell(self):
        def provider(symbol):
            return {"fear_greed_index": 95}

        skill = SentimentSkill(sentiment_provider=provider)
        result = await skill.execute({"symbol": "BTCUSDT"})
        assert result.data["sentiment"] == "extreme_greed"
        assert result.data["signal"] == "contrarian_sell"

    @pytest.mark.asyncio
    async def test_greed_is_neutral_signal(self):
        """Greed (not extreme) should give neutral signal."""
        def provider(symbol):
            return {"fear_greed_index": 80}

        skill = SentimentSkill(sentiment_provider=provider)
        result = await skill.execute({"symbol": "BTCUSDT"})
        assert result.data["sentiment"] == "greed"
        assert result.data["signal"] == "neutral"
