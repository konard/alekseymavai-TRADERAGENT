from __future__ import annotations

import time
from collections import deque
from enum import Enum
from dataclasses import dataclass, field
from typing import Any


class StrategyPhase(Enum):
    BIRTH = "birth"
    GROWTH = "growth"
    MATURITY = "maturity"
    DECLINE = "decline"
    RETIRED = "retired"


@dataclass
class _StrategyRecord:
    trades: list[tuple[float, float]] = field(default_factory=list)  # (pnl, ts)
    phase: StrategyPhase = StrategyPhase.BIRTH
    decline_consecutive: int = 0


class StrategyLifecycleManager:
    """Tracks per-strategy lifecycle phases based on rolling trade metrics."""

    ROLLING_WINDOW = 20

    def __init__(
        self,
        min_trades_birth: int = 10,
        growth_threshold: float = 0.6,
        decline_threshold: float = 0.4,
        retirement_trades: int = 50,
    ) -> None:
        self._min_trades_birth = min_trades_birth
        self._growth_threshold = growth_threshold
        self._decline_threshold = decline_threshold
        self._retirement_trades = retirement_trades
        self._strategies: dict[str, _StrategyRecord] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_trade(self, strategy_name: str, pnl: float, ts: float | None = None) -> None:
        if ts is None:
            ts = time.time()
        rec = self._strategies.setdefault(strategy_name, _StrategyRecord())
        rec.trades.append((pnl, ts))
        self._update_phase(strategy_name)

    def get_phase(self, strategy_name: str) -> StrategyPhase:
        rec = self._strategies.get(strategy_name)
        if rec is None:
            return StrategyPhase.BIRTH
        return rec.phase

    def get_all_phases(self) -> dict[str, StrategyPhase]:
        return {name: rec.phase for name, rec in self._strategies.items()}

    def get_recommendation(self, strategy_name: str) -> dict[str, Any]:
        phase = self.get_phase(strategy_name)
        mapping = {
            StrategyPhase.BIRTH: {"action": "monitor", "allocation_pct": 0.5},
            StrategyPhase.GROWTH: {"action": "increase", "allocation_pct": 1.2},
            StrategyPhase.MATURITY: {"action": "maintain", "allocation_pct": 1.0},
            StrategyPhase.DECLINE: {"action": "reduce", "allocation_pct": 0.3},
            StrategyPhase.RETIRED: {"action": "disable", "allocation_pct": 0.0},
        }
        return mapping[phase]

    def should_retire(self, strategy_name: str) -> bool:
        return self.get_phase(strategy_name) == StrategyPhase.RETIRED

    def get_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        for name, rec in self._strategies.items():
            window = self._rolling_window(rec)
            win_rate, avg_pnl = self._compute_metrics(window)
            stats[name] = {
                "phase": rec.phase.value,
                "trade_count": len(rec.trades),
                "win_rate": win_rate,
                "avg_pnl": avg_pnl,
                "decline_consecutive": rec.decline_consecutive,
            }
        return stats

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _rolling_window(self, rec: _StrategyRecord) -> list[tuple[float, float]]:
        return rec.trades[-self.ROLLING_WINDOW:]

    @staticmethod
    def _compute_metrics(window: list[tuple[float, float]]) -> tuple[float, float]:
        if not window:
            return 0.0, 0.0
        pnls = [t[0] for t in window]
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls)
        avg_pnl = sum(pnls) / len(pnls)
        return win_rate, avg_pnl

    @staticmethod
    def _compute_trend(window: list[tuple[float, float]]) -> float:
        """Return trend slope of PnL over the window. Positive = improving."""
        if len(window) < 4:
            return 0.0
        half = len(window) // 2
        first_half = [t[0] for t in window[:half]]
        second_half = [t[0] for t in window[half:]]
        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        return avg_second - avg_first

    def _update_phase(self, strategy_name: str) -> None:
        rec = self._strategies[strategy_name]

        if rec.phase == StrategyPhase.RETIRED:
            return

        total = len(rec.trades)
        if total < self._min_trades_birth:
            rec.phase = StrategyPhase.BIRTH
            return

        window = self._rolling_window(rec)
        win_rate, avg_pnl = self._compute_metrics(window)
        trend = self._compute_trend(window)

        # Check DECLINE first
        is_declining = win_rate < self._decline_threshold or avg_pnl < 0 or trend < 0
        if is_declining:
            if rec.phase != StrategyPhase.DECLINE:
                rec.decline_consecutive = 1
            else:
                rec.decline_consecutive += 1
            rec.phase = StrategyPhase.DECLINE

            if rec.decline_consecutive >= self._retirement_trades:
                rec.phase = StrategyPhase.RETIRED
            return

        # Not declining — reset counter
        rec.decline_consecutive = 0

        # GROWTH: above growth_threshold, positive pnl, improving
        if win_rate > self._growth_threshold and avg_pnl > 0 and trend > 0:
            rec.phase = StrategyPhase.GROWTH
            return

        # MATURITY: above decline_threshold, positive pnl, stable/flat
        if win_rate > self._decline_threshold and avg_pnl > 0:
            rec.phase = StrategyPhase.MATURITY
            return

        # Fallback — should not normally reach here
        rec.phase = StrategyPhase.MATURITY


class DrawdownRecoveryProtocol:
    """Manages risk reduction during portfolio drawdowns."""

    def __init__(
        self,
        trigger_pct: float = 0.10,
        recovery_pct: float = 0.05,
        conservative_risk_mult: float = 0.5,
        min_confidence: float = 0.7,
    ) -> None:
        self._trigger_pct = trigger_pct
        self._recovery_pct = recovery_pct
        self._conservative_risk_mult = conservative_risk_mult
        self._min_confidence = min_confidence

        self._peak: float = 0.0
        self._current: float = 0.0
        self._in_recovery: bool = False
        self._trades_blocked: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, portfolio_value: float, peak_value: float | None = None) -> None:
        if peak_value is not None:
            self._peak = max(self._peak, peak_value)
        self._peak = max(self._peak, portfolio_value)
        self._current = portfolio_value

        dd = self._drawdown_pct()

        if not self._in_recovery and dd >= self._trigger_pct:
            self._in_recovery = True
        elif self._in_recovery and dd <= self._recovery_pct:
            self._in_recovery = False

    def is_recovery_mode(self) -> bool:
        return self._in_recovery

    def get_risk_multiplier(self) -> float:
        if not self._in_recovery:
            return 1.0
        dd = self._drawdown_pct()
        # Linear scale: at trigger_pct → conservative_risk_mult, at 100% → 0.2
        # Interpolate between conservative_risk_mult and 0.2 as dd goes from trigger to 1.0
        if dd <= self._trigger_pct:
            return self._conservative_risk_mult
        slope = (self._conservative_risk_mult - 0.2) / (1.0 - self._trigger_pct)
        mult = self._conservative_risk_mult - slope * (dd - self._trigger_pct)
        return max(mult, 0.2)

    def should_allow_trade(self, confidence: float) -> bool:
        if not self._in_recovery:
            return True
        if confidence >= self._min_confidence:
            return True
        self._trades_blocked += 1
        return False

    def get_status(self) -> dict[str, Any]:
        dd = self._drawdown_pct()
        recovery_progress = 0.0
        if self._in_recovery and self._trigger_pct > self._recovery_pct:
            # 0.0 = just triggered, 1.0 = about to exit recovery
            progress = (self._trigger_pct - dd) / (self._trigger_pct - self._recovery_pct)
            recovery_progress = max(0.0, min(1.0, progress))
        return {
            "mode": "recovery" if self._in_recovery else "normal",
            "drawdown_pct": dd,
            "peak": self._peak,
            "current": self._current,
            "risk_multiplier": self.get_risk_multiplier(),
            "trades_blocked": self._trades_blocked,
            "recovery_progress": recovery_progress,
        }

    def get_stats(self) -> dict[str, Any]:
        return {
            "peak": self._peak,
            "current": self._current,
            "drawdown_pct": self._drawdown_pct(),
            "in_recovery": self._in_recovery,
            "trades_blocked": self._trades_blocked,
            "risk_multiplier": self.get_risk_multiplier(),
            "trigger_pct": self._trigger_pct,
            "recovery_pct": self._recovery_pct,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _drawdown_pct(self) -> float:
        if self._peak <= 0:
            return 0.0
        return (self._peak - self._current) / self._peak
