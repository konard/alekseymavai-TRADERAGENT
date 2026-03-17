from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any


class VolRegime(Enum):
    CALM = "calm"
    NORMAL = "normal"
    STRESSED = "stressed"
    CRISIS = "crisis"


_RISK_MULTIPLIERS: dict[VolRegime, float] = {
    VolRegime.CALM: 1.2,
    VolRegime.NORMAL: 1.0,
    VolRegime.STRESSED: 0.6,
    VolRegime.CRISIS: 0.3,
}

_RECOMMENDED_PARAMS: dict[VolRegime, dict[str, float]] = {
    VolRegime.CALM: {"sl_width": 0.8, "tp_width": 0.8, "position_size": 1.2},
    VolRegime.NORMAL: {"sl_width": 1.0, "tp_width": 1.0, "position_size": 1.0},
    VolRegime.STRESSED: {"sl_width": 1.5, "tp_width": 0.7, "position_size": 0.6},
    VolRegime.CRISIS: {"sl_width": 2.0, "tp_width": 0.5, "position_size": 0.3},
}


class VolatilityRegimeClassifier:
    """Classifies market volatility into regimes using EWMA volatility."""

    def __init__(
        self,
        window_size: int = 20,
        calm_threshold: float = 0.005,
        normal_threshold: float = 0.015,
        stressed_threshold: float = 0.03,
    ) -> None:
        self._window_size = window_size
        self._calm_threshold = calm_threshold
        self._normal_threshold = normal_threshold
        self._stressed_threshold = stressed_threshold

        self._returns: list[float] = []
        self._timestamps: list[float] = []
        self._all_vols: list[float] = []
        self._regime_history: list[dict[str, Any]] = []
        self._ewma_lambda = 0.94

    # ------------------------------------------------------------------
    def observe(self, ret: float, ts: float | None = None) -> None:
        """Store a return observation in the rolling window."""
        self._returns.append(ret)
        self._timestamps.append(ts if ts is not None else time.time())
        # Keep only the rolling window
        if len(self._returns) > self._window_size:
            self._returns = self._returns[-self._window_size :]
            self._timestamps = self._timestamps[-self._window_size :]

    # ------------------------------------------------------------------
    def _ewma_vol(self) -> float:
        """Compute EWMA volatility from the rolling window."""
        if len(self._returns) < 2:
            return 0.0
        lam = self._ewma_lambda
        variance = self._returns[0] ** 2
        for r in self._returns[1:]:
            variance = lam * variance + (1.0 - lam) * r * r
        return math.sqrt(variance)

    # ------------------------------------------------------------------
    def _regime_from_annualized(self, ann_vol: float) -> VolRegime:
        if ann_vol < self._calm_threshold:
            return VolRegime.CALM
        if ann_vol < self._normal_threshold:
            return VolRegime.NORMAL
        if ann_vol < self._stressed_threshold:
            return VolRegime.STRESSED
        return VolRegime.CRISIS

    # ------------------------------------------------------------------
    def _trend(self) -> str:
        """Compare volatility of first half vs second half of window."""
        if len(self._returns) < 4:
            return "stable"
        mid = len(self._returns) // 2
        first_half = self._returns[:mid]
        second_half = self._returns[mid:]

        def _rms(vals: list[float]) -> float:
            return math.sqrt(sum(v * v for v in vals) / len(vals))

        vol_first = _rms(first_half)
        vol_second = _rms(second_half)
        if vol_first == 0.0:
            return "stable" if vol_second == 0.0 else "increasing"
        ratio = vol_second / vol_first
        if ratio > 1.15:
            return "increasing"
        if ratio < 0.85:
            return "decreasing"
        return "stable"

    # ------------------------------------------------------------------
    def _percentile(self, current_vol: float) -> float:
        """Where *current_vol* ranks in all historically observed vols (0-100)."""
        if not self._all_vols:
            return 50.0
        count_below = sum(1 for v in self._all_vols if v <= current_vol)
        return 100.0 * count_below / len(self._all_vols)

    # ------------------------------------------------------------------
    def classify(self) -> dict[str, Any]:
        """Classify current regime and return diagnostic dict."""
        vol = self._ewma_vol()
        # Annualise assuming ~365 daily periods (crypto)
        ann_vol = vol * math.sqrt(365)
        self._all_vols.append(ann_vol)
        regime = self._regime_from_annualized(ann_vol)
        pct = self._percentile(ann_vol)
        trend = self._trend()

        entry = {
            "regime": regime.value,
            "volatility": vol,
            "annualized_vol": ann_vol,
            "percentile": pct,
            "trend": trend,
            "ts": self._timestamps[-1] if self._timestamps else time.time(),
        }
        self._regime_history.append(entry)
        # Keep history bounded
        if len(self._regime_history) > 500:
            self._regime_history = self._regime_history[-500:]
        return entry

    # ------------------------------------------------------------------
    def get_regime_history(self) -> list[dict[str, Any]]:
        """Return recent regime classifications with timestamps."""
        return list(self._regime_history)

    # ------------------------------------------------------------------
    def get_risk_multiplier(self) -> float:
        if not self._regime_history:
            return 1.0
        last_regime = VolRegime(self._regime_history[-1]["regime"])
        return _RISK_MULTIPLIERS[last_regime]

    # ------------------------------------------------------------------
    def get_recommended_params(self) -> dict[str, float]:
        if not self._regime_history:
            return dict(_RECOMMENDED_PARAMS[VolRegime.NORMAL])
        last_regime = VolRegime(self._regime_history[-1]["regime"])
        return dict(_RECOMMENDED_PARAMS[last_regime])

    # ------------------------------------------------------------------
    def get_stats(self) -> dict[str, Any]:
        return {
            "window_size": self._window_size,
            "observations": len(self._returns),
            "history_len": len(self._regime_history),
            "all_vols_count": len(self._all_vols),
            "thresholds": {
                "calm": self._calm_threshold,
                "normal": self._normal_threshold,
                "stressed": self._stressed_threshold,
            },
        }


# ======================================================================
# Liquidity scoring
# ======================================================================


@dataclass
class LiquidityScore:
    symbol: str
    score: float
    level: str
    components: dict[str, float]
    tradeable: bool
    max_safe_order_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score": self.score,
            "level": self.level,
            "components": dict(self.components),
            "tradeable": self.tradeable,
            "max_safe_order_usd": self.max_safe_order_usd,
        }


@dataclass
class _LiqObs:
    spread_pct: float
    depth_usd: float
    volume_24h_usd: float
    ts: float


class LiquidityScorer:
    """Score liquidity for symbols based on spread, depth, volume, stability."""

    _OBS_WINDOW = 20

    def __init__(
        self,
        min_score_to_trade: float = 0.3,
        depth_reference_usd: float = 100_000,
    ) -> None:
        self._min_score = min_score_to_trade
        self._depth_ref = depth_reference_usd
        self._observations: dict[str, list[_LiqObs]] = defaultdict(list)

    # ------------------------------------------------------------------
    def observe(
        self,
        symbol: str,
        spread_pct: float,
        depth_usd: float,
        volume_24h_usd: float,
        ts: float | None = None,
    ) -> None:
        obs = _LiqObs(
            spread_pct=spread_pct,
            depth_usd=depth_usd,
            volume_24h_usd=volume_24h_usd,
            ts=ts if ts is not None else time.time(),
        )
        buf = self._observations[symbol]
        buf.append(obs)
        if len(buf) > self._OBS_WINDOW:
            self._observations[symbol] = buf[-self._OBS_WINDOW :]

    # ------------------------------------------------------------------
    @staticmethod
    def _level(score: float) -> str:
        if score >= 0.8:
            return "excellent"
        if score >= 0.6:
            return "good"
        if score >= 0.4:
            return "fair"
        if score >= 0.2:
            return "poor"
        return "dangerous"

    # ------------------------------------------------------------------
    def score(self, symbol: str) -> LiquidityScore:
        buf = self._observations.get(symbol)
        if not buf:
            return LiquidityScore(
                symbol=symbol,
                score=0.0,
                level="dangerous",
                components={
                    "spread_score": 0.0,
                    "depth_score": 0.0,
                    "volume_score": 0.0,
                    "stability_score": 0.0,
                },
                tradeable=False,
                max_safe_order_usd=0.0,
            )

        spreads = [o.spread_pct for o in buf]
        depths = [o.depth_usd for o in buf]
        volumes = [o.volume_24h_usd for o in buf]

        avg_spread = sum(spreads) / len(spreads)
        avg_depth = sum(depths) / len(depths)
        avg_volume = sum(volumes) / len(volumes)

        spread_score = max(0.0, 1.0 - min(avg_spread / 0.005, 1.0))
        depth_score = min(avg_depth / self._depth_ref, 1.0)
        volume_score = min(avg_volume / 1_000_000, 1.0)

        # stability: 1 - CV(spread)
        if avg_spread > 0 and len(spreads) > 1:
            variance = sum((s - avg_spread) ** 2 for s in spreads) / len(spreads)
            std_spread = math.sqrt(variance)
            stability_score = max(0.0, 1.0 - std_spread / avg_spread)
        else:
            stability_score = 0.0

        overall = (
            0.30 * spread_score + 0.30 * depth_score + 0.25 * volume_score + 0.15 * stability_score
        )

        return LiquidityScore(
            symbol=symbol,
            score=overall,
            level=self._level(overall),
            components={
                "spread_score": spread_score,
                "depth_score": depth_score,
                "volume_score": volume_score,
                "stability_score": stability_score,
            },
            tradeable=overall >= self._min_score,
            max_safe_order_usd=avg_depth * 0.1,
        )

    # ------------------------------------------------------------------
    def score_all(self) -> dict[str, LiquidityScore]:
        return {sym: self.score(sym) for sym in self._observations}

    # ------------------------------------------------------------------
    def get_best_symbols(self, n: int = 5) -> list[LiquidityScore]:
        all_scores = list(self.score_all().values())
        all_scores.sort(key=lambda s: s.score, reverse=True)
        return all_scores[:n]

    # ------------------------------------------------------------------
    def should_trade(self, symbol: str) -> bool:
        return self.score(symbol).tradeable

    # ------------------------------------------------------------------
    def get_stats(self) -> dict[str, Any]:
        return {
            "symbols_tracked": len(self._observations),
            "min_score_to_trade": self._min_score,
            "depth_reference_usd": self._depth_ref,
            "observations_per_symbol": {sym: len(obs) for sym, obs in self._observations.items()},
        }
