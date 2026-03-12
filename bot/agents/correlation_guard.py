"""Correlation Guard — detects cross-pair correlation and manages portfolio exposure."""

import math
import time
from dataclasses import dataclass, field
from typing import Any

from bot.core.event_bus import DomainEvent, EventBus, EventPriority
from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CorrelationAlert:
    pair_a: str  # e.g., "ETH/USDT"
    pair_b: str
    correlation: float  # -1 to 1
    direction_a: str  # "LONG" / "SHORT"
    direction_b: str
    effective_exposure: float  # combined exposure considering correlation
    recommendation: str  # "reduce", "hedge", "ok"
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair_a": self.pair_a,
            "pair_b": self.pair_b,
            "correlation": self.correlation,
            "direction_a": self.direction_a,
            "direction_b": self.direction_b,
            "effective_exposure": self.effective_exposure,
            "recommendation": self.recommendation,
            "ts": self.ts,
        }


class CorrelationGuard:
    """Monitors cross-pair correlation and flags concentrated risk.

    Tracks rolling correlation between price returns of different pairs.
    When correlated pairs have same-direction positions, flags the effective
    exposure as being higher than the sum of individual positions.

    Example: 5 LONG positions on BTC, ETH, SOL, AVAX, LINK with avg correlation 0.85
    -> effective exposure is not 5x but closer to 4.25x (almost one giant position)
    """

    def __init__(
        self,
        window_size: int = 50,
        correlation_threshold: float = 0.7,
        max_correlated_exposure: float = 3.0,
        event_bus: EventBus | None = None,
    ) -> None:
        self._window_size = window_size
        self._correlation_threshold = correlation_threshold
        self._max_correlated_exposure = max_correlated_exposure
        self._event_bus = event_bus

        self._returns: dict[str, list[float]] = {}
        self._positions: dict[str, dict] = {}
        self._alerts: list[CorrelationAlert] = []
        self._max_alerts: int = 200

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe_return(self, symbol: str, return_pct: float) -> None:
        """Record a price return for a symbol. Called each bar."""
        if symbol not in self._returns:
            self._returns[symbol] = []
        self._returns[symbol].append(return_pct)
        # Keep only the most recent window_size entries
        if len(self._returns[symbol]) > self._window_size * 2:
            self._returns[symbol] = self._returns[symbol][-self._window_size:]

    def observe_returns_batch(self, returns: dict[str, float]) -> None:
        """Record returns for multiple symbols at once."""
        for symbol, ret in returns.items():
            self.observe_return(symbol, ret)

    def set_positions(self, positions: dict[str, dict]) -> None:
        """Update current open positions.

        positions = {"ETH/USDT": {"direction": "LONG", "size_pct": 2.0}, ...}
        """
        self._positions = dict(positions)

    # ------------------------------------------------------------------
    # Correlation calculation (numpy-free)
    # ------------------------------------------------------------------

    def calculate_correlation(self, symbol_a: str, symbol_b: str) -> float | None:
        """Calculate Pearson correlation between two symbols' returns.

        Returns None if insufficient data.
        Uses numpy-free implementation (manual Pearson formula).
        """
        returns_a = self._returns.get(symbol_a, [])
        returns_b = self._returns.get(symbol_b, [])

        if not returns_a or not returns_b:
            return None

        # Align to same length (use the most recent overlapping bars)
        n = min(len(returns_a), len(returns_b), self._window_size)
        if n < 5:  # need at least 5 data points for meaningful correlation
            return None

        xs = returns_a[-n:]
        ys = returns_b[-n:]

        mean_x = sum(xs) / n
        mean_y = sum(ys) / n

        cov = 0.0
        var_x = 0.0
        var_y = 0.0
        for i in range(n):
            dx = xs[i] - mean_x
            dy = ys[i] - mean_y
            cov += dx * dy
            var_x += dx * dx
            var_y += dy * dy

        if var_x == 0.0 or var_y == 0.0:
            return 0.0

        return cov / math.sqrt(var_x * var_y)

    def get_correlation_matrix(self) -> dict[str, dict[str, float]]:
        """Get full pairwise correlation matrix for all tracked symbols."""
        symbols = list(self._returns.keys())
        matrix: dict[str, dict[str, float]] = {}

        for sym_a in symbols:
            matrix[sym_a] = {}
            for sym_b in symbols:
                if sym_a == sym_b:
                    matrix[sym_a][sym_b] = 1.0
                else:
                    corr = self.calculate_correlation(sym_a, sym_b)
                    matrix[sym_a][sym_b] = corr if corr is not None else 0.0

        return matrix

    # ------------------------------------------------------------------
    # Exposure analysis
    # ------------------------------------------------------------------

    def check_exposure(self) -> list[CorrelationAlert]:
        """Check all open positions for correlated exposure.

        For each pair of same-direction positions:
          1. Calculate correlation
          2. If correlation > threshold:
             - effective_exposure = size_a + size_b * correlation
             - If total effective exposure > max_correlated_exposure -> alert

        Returns list of alerts.
        """
        alerts: list[CorrelationAlert] = []
        symbols = list(self._positions.keys())

        for i in range(len(symbols)):
            for j in range(i + 1, len(symbols)):
                sym_a = symbols[i]
                sym_b = symbols[j]
                pos_a = self._positions[sym_a]
                pos_b = self._positions[sym_b]

                dir_a = pos_a.get("direction", "LONG")
                dir_b = pos_b.get("direction", "LONG")
                size_a = pos_a.get("size_pct", 1.0)
                size_b = pos_b.get("size_pct", 1.0)

                corr = self.calculate_correlation(sym_a, sym_b)
                if corr is None:
                    continue

                abs_corr = abs(corr)

                # Same direction + positively correlated = concentrated risk
                # Opposite direction + positively correlated = natural hedge
                same_direction = dir_a == dir_b
                is_risky = (same_direction and corr > self._correlation_threshold) or (
                    not same_direction and corr < -self._correlation_threshold
                )

                if not is_risky:
                    continue

                effective = size_a + size_b * abs_corr

                if effective > self._max_correlated_exposure:
                    recommendation = "reduce"
                elif abs_corr > 0.9:
                    recommendation = "hedge"
                else:
                    recommendation = "ok"

                alert = CorrelationAlert(
                    pair_a=sym_a,
                    pair_b=sym_b,
                    correlation=corr,
                    direction_a=dir_a,
                    direction_b=dir_b,
                    effective_exposure=effective,
                    recommendation=recommendation,
                )
                alerts.append(alert)

                # Store alert
                self._alerts.append(alert)
                if len(self._alerts) > self._max_alerts:
                    self._alerts = self._alerts[-self._max_alerts:]

                logger.info(
                    "correlation_alert",
                    pair_a=sym_a,
                    pair_b=sym_b,
                    correlation=round(corr, 3),
                    effective_exposure=round(effective, 2),
                    recommendation=recommendation,
                )

        return alerts

    def get_effective_exposure(self) -> float:
        """Calculate total effective portfolio exposure considering correlations.

        For N same-direction positions with avg correlation rho:
          effective = sqrt(N + N*(N-1)*rho) * avg_position_size
          (simplified portfolio variance formula)
        """
        if not self._positions:
            return 0.0

        # Group positions by direction
        groups: dict[str, list[tuple[str, float]]] = {}
        for sym, pos in self._positions.items():
            direction = pos.get("direction", "LONG")
            size = pos.get("size_pct", 1.0)
            if direction not in groups:
                groups[direction] = []
            groups[direction].append((sym, size))

        total_effective = 0.0

        for direction, positions in groups.items():
            n = len(positions)
            if n == 0:
                continue

            avg_size = sum(s for _, s in positions) / n

            if n == 1:
                total_effective += positions[0][1]
                continue

            # Calculate average pairwise correlation
            corr_sum = 0.0
            corr_count = 0
            for i in range(n):
                for j in range(i + 1, n):
                    corr = self.calculate_correlation(positions[i][0], positions[j][0])
                    if corr is not None:
                        corr_sum += corr
                        corr_count += 1

            avg_corr = corr_sum / corr_count if corr_count > 0 else 0.0

            # Portfolio variance formula: sqrt(N + N*(N-1)*rho) * avg_size
            variance_factor = n + n * (n - 1) * max(avg_corr, 0.0)
            effective = math.sqrt(max(variance_factor, 0.0)) * avg_size
            total_effective += effective

        return total_effective

    def should_allow_new_position(
        self, symbol: str, direction: str, size_pct: float
    ) -> tuple[bool, str]:
        """Check if adding a new position would exceed correlated exposure limits.

        Returns (allowed, reason).
        """
        # Temporarily add the position
        saved = dict(self._positions)
        self._positions[symbol] = {"direction": direction, "size_pct": size_pct}

        effective = self.get_effective_exposure()

        # Restore original positions
        self._positions = saved

        if effective > self._max_correlated_exposure:
            reason = (
                f"Adding {symbol} {direction} would bring effective exposure to "
                f"{effective:.2f} (limit: {self._max_correlated_exposure:.2f})"
            )
            logger.warning("position_blocked", symbol=symbol, reason=reason)
            return False, reason

        return True, "ok"

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_recent_alerts(self, limit: int = 20) -> list[dict]:
        """Get recent correlation alerts."""
        return [a.to_dict() for a in self._alerts[-limit:]]

    def get_stats(self) -> dict:
        """Get correlation guard statistics."""
        symbols = list(self._returns.keys())
        n_symbols = len(symbols)
        n_positions = len(self._positions)

        # Count high correlations
        high_corr_pairs = 0
        total_pairs = 0
        for i in range(n_symbols):
            for j in range(i + 1, n_symbols):
                corr = self.calculate_correlation(symbols[i], symbols[j])
                if corr is not None:
                    total_pairs += 1
                    if abs(corr) > self._correlation_threshold:
                        high_corr_pairs += 1

        return {
            "tracked_symbols": n_symbols,
            "open_positions": n_positions,
            "total_pairs": total_pairs,
            "high_correlation_pairs": high_corr_pairs,
            "effective_exposure": round(self.get_effective_exposure(), 3),
            "max_correlated_exposure": self._max_correlated_exposure,
            "correlation_threshold": self._correlation_threshold,
            "window_size": self._window_size,
            "total_alerts": len(self._alerts),
        }
