from __future__ import annotations

import math
import random
from typing import Any, Callable, Dict, List, Optional

from bot.agents.tools.market_data import BaseTool, ToolResult


class BacktestSkill(BaseTool):
    """Run a quick mini-backtest on a hypothesis."""

    name = "backtest"
    description = "Run a quick mini-backtest on a hypothesis"

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        strategy: str = params.get("strategy", "long")
        entry_price: float = params["entry_price"]
        stop_loss: float = params["stop_loss"]
        take_profit: float = params["take_profit"]
        historical_prices: List[float] = params["historical_prices"]

        if not historical_prices:
            return ToolResult(success=False, error="historical_prices is empty")

        # --- deterministic single-path simulation ---
        result, pnl, bars_to_exit = self._simulate_path(
            strategy, entry_price, stop_loss, take_profit, historical_prices,
        )

        # --- risk / reward ---
        if strategy == "long":
            risk = abs(entry_price - stop_loss)
            reward = abs(take_profit - entry_price)
        else:
            risk = abs(stop_loss - entry_price)
            reward = abs(entry_price - take_profit)
        risk_reward = reward / risk if risk > 0 else 0.0

        # --- Monte Carlo (100 shuffled paths) ---
        mc_wins = 0
        n_runs = 100
        for _ in range(n_runs):
            shuffled = list(historical_prices)
            random.shuffle(shuffled)
            r, _, _ = self._simulate_path(
                strategy, entry_price, stop_loss, take_profit, shuffled,
            )
            if r == "win":
                mc_wins += 1

        return ToolResult(
            success=True,
            data={
                "result": result,
                "pnl": round(pnl, 6),
                "bars_to_exit": bars_to_exit,
                "risk_reward": round(risk_reward, 4),
                "monte_carlo_win_pct": mc_wins / n_runs,
            },
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _simulate_path(
        strategy: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        prices: List[float],
    ) -> tuple[str, float, int]:
        """Walk *prices* bar-by-bar; return (result, pnl, bars)."""
        for i, price in enumerate(prices):
            if strategy == "long":
                if price <= stop_loss:
                    return "loss", stop_loss - entry_price, i + 1
                if price >= take_profit:
                    return "win", take_profit - entry_price, i + 1
            else:  # short
                if price >= stop_loss:
                    return "loss", entry_price - stop_loss, i + 1
                if price <= take_profit:
                    return "win", entry_price - take_profit, i + 1
        # Neither SL nor TP hit
        last = prices[-1]
        pnl = (last - entry_price) if strategy == "long" else (entry_price - last)
        return "open", pnl, len(prices)


class CorrelationSkill(BaseTool):
    """Check correlation between two return series."""

    name = "correlation_check"
    description = "Calculate Pearson and rolling correlation between two return series"

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        returns_a: List[float] = params["returns_a"]
        returns_b: List[float] = params["returns_b"]
        label_a: str = params.get("label_a", "A")
        label_b: str = params.get("label_b", "B")

        n = min(len(returns_a), len(returns_b))
        if n < 2:
            return ToolResult(success=False, error="Need at least 2 data points")

        a = returns_a[:n]
        b = returns_b[:n]

        corr = self._pearson(a, b)

        # Rolling 20-period correlation
        window = 20
        rolling: List[float | None] = []
        for end in range(window, n + 1):
            rolling.append(round(self._pearson(a[end - window : end], b[end - window : end]), 4))

        regime = self._classify_regime(abs(corr))

        interpretation = (
            f"{label_a} and {label_b} show {regime} correlation ({corr:.2f})"
        )

        return ToolResult(
            success=True,
            data={
                "correlation": round(corr, 4),
                "regime": regime,
                "rolling_correlations": rolling,
                "interpretation": interpretation,
            },
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _pearson(x: List[float], y: List[float]) -> float:
        n = len(x)
        if n == 0:
            return 0.0
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
        std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
        std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
        if std_x == 0 or std_y == 0:
            return 0.0
        return cov / (std_x * std_y)

    @staticmethod
    def _classify_regime(abs_corr: float) -> str:
        if abs_corr > 0.7:
            return "high"
        if abs_corr > 0.4:
            return "medium"
        return "low"


class VolatilityForecastSkill(BaseTool):
    """Forecast volatility using EWMA model."""

    name = "volatility_forecast"
    description = "EWMA volatility forecast with regime classification"

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        returns: List[float] = params["returns"]
        horizon: int = params.get("horizon", 5)

        if len(returns) < 2:
            return ToolResult(success=False, error="Need at least 2 returns")

        lam = 0.94  # RiskMetrics lambda

        # EWMA variance
        var = returns[0] ** 2
        for r in returns[1:]:
            var = lam * var + (1 - lam) * r ** 2

        current_vol = math.sqrt(var)

        # Multi-step forecast: for EWMA, h-step variance = var (constant)
        forecast_vol = current_vol * math.sqrt(horizon)

        # Annualise (assuming daily returns, 365 crypto days)
        annualized_vol = current_vol * math.sqrt(365)

        # Suggested stop width: 2 * forecast_vol (as fraction)
        # The caller can multiply by current_price externally; here we
        # return the multiplier so the tool stays price-agnostic.
        suggested_stop_width = 2.0 * forecast_vol

        regime = self._vol_regime(annualized_vol)

        return ToolResult(
            success=True,
            data={
                "current_vol": round(current_vol, 6),
                "forecast_vol": round(forecast_vol, 6),
                "annualized_vol": round(annualized_vol, 6),
                "suggested_stop_width": round(suggested_stop_width, 6),
                "vol_regime": regime,
            },
        )

    @staticmethod
    def _vol_regime(ann_vol: float) -> str:
        if ann_vol < 0.3:
            return "low"
        if ann_vol < 0.6:
            return "normal"
        if ann_vol < 1.0:
            return "high"
        return "extreme"


class SentimentSkill(BaseTool):
    """Retrieve market sentiment and produce a contrarian signal."""

    name = "sentiment"
    description = "Fetch sentiment data and generate contrarian signals"

    def __init__(self, sentiment_provider: Optional[Callable[[str], Dict[str, Any]]] = None):
        self._provider = sentiment_provider

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        symbol: str = params.get("symbol", "BTCUSDT")

        if self._provider is not None:
            raw = self._provider(symbol)
        else:
            raw = {}

        fgi: int = raw.get("fear_greed_index", 50)
        funding: float = raw.get("funding_rate", 0.0)
        ls_ratio: float = raw.get("long_short_ratio", 1.0)

        sentiment = self._classify_fgi(fgi)
        signal = self._contrarian_signal(sentiment)

        return ToolResult(
            success=True,
            data={
                "fear_greed_index": fgi,
                "funding_rate": funding,
                "long_short_ratio": ls_ratio,
                "sentiment": sentiment,
                "signal": signal,
            },
        )

    @staticmethod
    def _classify_fgi(value: int) -> str:
        if value <= 10:
            return "extreme_fear"
        if value <= 30:
            return "fear"
        if value <= 70:
            return "neutral"
        if value <= 90:
            return "greed"
        return "extreme_greed"

    @staticmethod
    def _contrarian_signal(sentiment: str) -> str:
        if sentiment == "extreme_fear":
            return "contrarian_buy"
        if sentiment == "extreme_greed":
            return "contrarian_sell"
        return "neutral"
