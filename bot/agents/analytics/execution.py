from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutionAlgo(Enum):
    MARKET = "market"
    TWAP = "twap"
    VWAP = "vwap"
    ICEBERG = "iceberg"


@dataclass
class ExecutionPlan:
    algo: ExecutionAlgo
    total_qty: float
    slices: list[dict] = field(default_factory=list)
    estimated_slippage_pct: float = 0.0
    estimated_duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "algo": self.algo.value,
            "total_qty": self.total_qty,
            "slices": list(self.slices),
            "estimated_slippage_pct": self.estimated_slippage_pct,
            "estimated_duration_seconds": self.estimated_duration_seconds,
        }


class ExecutionOptimizer:
    """Selects execution algorithm and generates order slices based on
    order size relative to market volume and urgency."""

    def __init__(
        self,
        default_slice_count: int = 10,
        max_single_order_pct: float = 0.02,
        impact_factor: float = 0.1,
    ) -> None:
        self.default_slice_count = default_slice_count
        self.max_single_order_pct = max_single_order_pct
        self.impact_factor = impact_factor
        self._execution_history: list[dict] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan_execution(
        self,
        total_qty: float,
        current_price: float,
        avg_volume_24h: float,
        urgency: str = "normal",
    ) -> ExecutionPlan:
        volume_ratio = total_qty / avg_volume_24h if avg_volume_24h > 0 else 1.0
        slippage = self.estimate_slippage(total_qty, avg_volume_24h)

        # Small order → simple MARKET
        if volume_ratio < self.max_single_order_pct:
            slices = [{"qty": total_qty, "delay_seconds": 0.0, "price_limit": None}]
            return ExecutionPlan(
                algo=ExecutionAlgo.MARKET,
                total_qty=total_qty,
                slices=slices,
                estimated_slippage_pct=slippage,
                estimated_duration_seconds=0.0,
            )

        if urgency == "high":
            num_slices = max(2, self.default_slice_count // 2)
            duration = 60.0 * num_slices  # shorter windows
            slices = self.generate_twap_slices(total_qty, duration, num_slices)
            return ExecutionPlan(
                algo=ExecutionAlgo.TWAP,
                total_qty=total_qty,
                slices=slices,
                estimated_slippage_pct=slippage,
                estimated_duration_seconds=duration,
            )

        if urgency == "low":
            num_slices = self.default_slice_count * 2
            duration = 300.0 * num_slices  # generous windows
            slices = self.generate_vwap_slices(total_qty, None, num_slices)
            # Assign delays from duration
            interval = duration / num_slices
            for i, s in enumerate(slices):
                s["delay_seconds"] = interval * i
            return ExecutionPlan(
                algo=ExecutionAlgo.VWAP,
                total_qty=total_qty,
                slices=slices,
                estimated_slippage_pct=slippage,
                estimated_duration_seconds=duration,
            )

        # Default → ICEBERG
        slices = self.generate_iceberg_slices(total_qty)
        duration = sum(s["delay_seconds"] for s in slices)
        return ExecutionPlan(
            algo=ExecutionAlgo.ICEBERG,
            total_qty=total_qty,
            slices=slices,
            estimated_slippage_pct=slippage,
            estimated_duration_seconds=duration,
        )

    def estimate_slippage(
        self,
        order_size: float,
        avg_volume_24h: float,
        spread_pct: float = 0.001,
    ) -> float:
        volume_ratio = order_size / avg_volume_24h if avg_volume_24h > 0 else 1.0
        return spread_pct / 2.0 + volume_ratio * self.impact_factor

    def generate_twap_slices(
        self,
        total_qty: float,
        duration_seconds: float,
        num_slices: int,
    ) -> list[dict]:
        if num_slices <= 0:
            return []
        qty_per_slice = total_qty / num_slices
        interval = duration_seconds / num_slices
        return [
            {
                "qty": qty_per_slice,
                "delay_seconds": interval * i,
                "price_limit": None,
            }
            for i in range(num_slices)
        ]

    def generate_vwap_slices(
        self,
        total_qty: float,
        volume_profile: list[float] | None,
        num_slices: int,
    ) -> list[dict]:
        if num_slices <= 0:
            return []

        if volume_profile is not None and len(volume_profile) == num_slices:
            weights = list(volume_profile)
        else:
            # U-shaped default: higher weight at start and end
            weights = []
            for i in range(num_slices):
                # Normalised position 0..1
                t = i / max(num_slices - 1, 1)
                # Parabola: high at 0 and 1, low at 0.5
                w = 1.0 + 2.0 * (2.0 * t - 1.0) ** 2
                weights.append(w)

        total_w = sum(weights) or 1.0
        return [
            {
                "qty": total_qty * (w / total_w),
                "delay_seconds": 0.0,
                "price_limit": None,
            }
            for w in weights
        ]

    def generate_iceberg_slices(
        self,
        total_qty: float,
        visible_pct: float = 0.2,
        num_slices: int | None = None,
    ) -> list[dict]:
        if num_slices is None:
            num_slices = self.default_slice_count
        if num_slices <= 0:
            return []

        qty_per_slice = total_qty * visible_pct
        remaining = total_qty
        slices: list[dict] = []
        rng = random.Random(42)  # deterministic for reproducibility

        for i in range(num_slices):
            qty = min(qty_per_slice, remaining)
            if qty <= 0:
                break
            delay = 0.0 if i == 0 else rng.uniform(5.0, 30.0)
            slices.append({"qty": qty, "delay_seconds": delay, "price_limit": None})
            remaining -= qty

        # If there's leftover, add a final slice
        if remaining > 1e-12:
            slices.append(
                {
                    "qty": remaining,
                    "delay_seconds": rng.uniform(5.0, 30.0),
                    "price_limit": None,
                }
            )
        return slices

    def record_execution(
        self,
        plan: ExecutionPlan,
        actual_avg_price: float,
        planned_price: float,
    ) -> None:
        if planned_price <= 0:
            return
        actual_slippage = abs(actual_avg_price - planned_price) / planned_price
        self._execution_history.append(
            {
                "algo": plan.algo.value,
                "total_qty": plan.total_qty,
                "estimated_slippage_pct": plan.estimated_slippage_pct,
                "actual_slippage_pct": actual_slippage,
                "planned_price": planned_price,
                "actual_avg_price": actual_avg_price,
            }
        )

    def get_slippage_stats(self) -> dict:
        if not self._execution_history:
            return {"count": 0, "avg": 0.0, "max": 0.0, "min": 0.0}
        slippages = [r["actual_slippage_pct"] for r in self._execution_history]
        return {
            "count": len(slippages),
            "avg": sum(slippages) / len(slippages),
            "max": max(slippages),
            "min": min(slippages),
        }

    def get_stats(self) -> dict:
        return {
            "default_slice_count": self.default_slice_count,
            "max_single_order_pct": self.max_single_order_pct,
            "impact_factor": self.impact_factor,
            "executions_recorded": len(self._execution_history),
            "slippage_stats": self.get_slippage_stats(),
        }


# ======================================================================
# Market Microstructure
# ======================================================================


@dataclass
class _Tick:
    price: float
    volume: float
    side: str  # "buy" or "sell"
    ts: float


class MarketMicrostructure:
    """Rolling-window microstructure analysis from tick data."""

    def __init__(self, window_size: int = 100) -> None:
        self.window_size = window_size
        self._ticks: deque[_Tick] = deque(maxlen=window_size)

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def observe_tick(
        self,
        price: float,
        volume: float,
        side: str,
        ts: float | None = None,
    ) -> None:
        if ts is None:
            ts = time.time()
        self._ticks.append(_Tick(price=price, volume=volume, side=side, ts=ts))

    # ------------------------------------------------------------------
    # VPIN
    # ------------------------------------------------------------------

    def calculate_vpin(self, bucket_size: float = 1000.0) -> float:
        """Volume-Synchronized Probability of Informed Trading.

        Returns 0.0-1.0 where higher values indicate more toxic (informed) flow.
        """
        if not self._ticks:
            return 0.0

        # Build volume buckets
        buckets: list[tuple[float, float]] = []  # (buy_vol, sell_vol)
        buy_vol = 0.0
        sell_vol = 0.0
        bucket_vol = 0.0

        for tick in self._ticks:
            if tick.side == "buy":
                buy_vol += tick.volume
            else:
                sell_vol += tick.volume
            bucket_vol += tick.volume

            if bucket_vol >= bucket_size:
                buckets.append((buy_vol, sell_vol))
                buy_vol = 0.0
                sell_vol = 0.0
                bucket_vol = 0.0

        # Include partial last bucket if any volume remains
        if bucket_vol > 0:
            buckets.append((buy_vol, sell_vol))

        if not buckets:
            return 0.0

        total_imbalance = sum(abs(b - s) for b, s in buckets)
        total_volume = sum(b + s for b, s in buckets)
        if total_volume <= 0:
            return 0.0

        vpin = total_imbalance / total_volume
        return min(max(vpin, 0.0), 1.0)

    # ------------------------------------------------------------------
    # Spread metrics
    # ------------------------------------------------------------------

    def calculate_spread_metrics(self) -> dict:
        if len(self._ticks) < 2:
            return {"avg_spread": 0.0, "realized_spread": 0.0, "effective_spread": 0.0}

        # Approximate spread from consecutive buy/sell tick pairs
        spreads: list[float] = []
        for i in range(1, len(self._ticks)):
            prev = self._ticks[i - 1]
            curr = self._ticks[i]
            if prev.side != curr.side:
                spreads.append(abs(curr.price - prev.price))

        if not spreads:
            return {"avg_spread": 0.0, "realized_spread": 0.0, "effective_spread": 0.0}

        avg_spread = sum(spreads) / len(spreads)
        mid_prices: list[float] = []
        for i in range(1, len(self._ticks)):
            prev = self._ticks[i - 1]
            curr = self._ticks[i]
            if prev.side != curr.side:
                mid_prices.append((prev.price + curr.price) / 2.0)

        # Effective spread: average distance from trade price to midpoint
        effective_spreads: list[float] = []
        mid_idx = 0
        for i in range(1, len(self._ticks)):
            prev = self._ticks[i - 1]
            curr = self._ticks[i]
            if prev.side != curr.side and mid_idx < len(mid_prices):
                mid = mid_prices[mid_idx]
                effective_spreads.append(2.0 * abs(curr.price - mid))
                mid_idx += 1

        effective = sum(effective_spreads) / len(effective_spreads) if effective_spreads else 0.0

        # Realized spread: effective spread minus price impact
        # Approximation: half the avg spread
        realized = avg_spread / 2.0

        return {
            "avg_spread": avg_spread,
            "realized_spread": realized,
            "effective_spread": effective,
        }

    # ------------------------------------------------------------------
    # Tick patterns
    # ------------------------------------------------------------------

    def detect_tick_patterns(self) -> dict:
        if not self._ticks:
            return {
                "tick_direction_runs": 0,
                "avg_run_length": 0.0,
                "reversal_rate": 0.0,
                "large_trade_ratio": 0.0,
            }

        ticks = list(self._ticks)

        # Direction runs
        runs = 1
        run_lengths: list[int] = []
        current_run = 1
        reversals = 0

        for i in range(1, len(ticks)):
            if ticks[i].side == ticks[i - 1].side:
                current_run += 1
            else:
                run_lengths.append(current_run)
                runs += 1
                current_run = 1
                reversals += 1
        run_lengths.append(current_run)

        avg_run = sum(run_lengths) / len(run_lengths) if run_lengths else 0.0
        reversal_rate = reversals / max(len(ticks) - 1, 1)

        # Large trades
        volumes = [t.volume for t in ticks]
        avg_vol = sum(volumes) / len(volumes) if volumes else 0.0
        large_threshold = 2.0 * avg_vol
        large_count = sum(1 for v in volumes if v > large_threshold)
        large_ratio = large_count / len(ticks) if ticks else 0.0

        return {
            "tick_direction_runs": runs,
            "avg_run_length": avg_run,
            "reversal_rate": reversal_rate,
            "large_trade_ratio": large_ratio,
        }

    # ------------------------------------------------------------------
    # Toxicity
    # ------------------------------------------------------------------

    def get_toxicity_score(self) -> dict:
        if not self._ticks:
            return {
                "score": 0.0,
                "level": "low",
                "components": {
                    "vpin": 0.0,
                    "spread_score": 0.0,
                    "pattern_score": 0.0,
                },
            }

        vpin = self.calculate_vpin()
        spread = self.calculate_spread_metrics()
        patterns = self.detect_tick_patterns()

        # Normalise spread component: higher effective spread → higher toxicity
        prices = [t.price for t in self._ticks]
        avg_price = sum(prices) / len(prices) if prices else 1.0
        spread_pct = spread["effective_spread"] / avg_price if avg_price > 0 else 0.0
        # Map spread_pct to 0-1 (0.5% → 1.0)
        spread_score = min(spread_pct / 0.005, 1.0)

        # Pattern score: high run length + low reversal → directional pressure
        avg_run = patterns["avg_run_length"]
        run_score = min(avg_run / 5.0, 1.0)  # runs of 5+ → score 1.0
        large_score = min(patterns["large_trade_ratio"] / 0.2, 1.0)
        pattern_score = (run_score + large_score) / 2.0

        # Composite: weighted average
        score = 0.5 * vpin + 0.25 * spread_score + 0.25 * pattern_score
        score = min(max(score, 0.0), 1.0)

        if score >= 0.75:
            level = "toxic"
        elif score >= 0.5:
            level = "high"
        elif score >= 0.25:
            level = "medium"
        else:
            level = "low"

        return {
            "score": score,
            "level": level,
            "components": {
                "vpin": vpin,
                "spread_score": spread_score,
                "pattern_score": pattern_score,
            },
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        return {
            "window_size": self.window_size,
            "ticks_observed": len(self._ticks),
            "toxicity": self.get_toxicity_score(),
        }
