from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# StopAction
# ---------------------------------------------------------------------------

@dataclass
class StopAction:
    """Describes a stop-loss management action."""

    action: str  # "move_to_breakeven", "trail", "partial_exit", "hold"
    new_stop: float | None = None
    exit_pct: float = 0.0  # 0-1, fraction to exit on partial_exit
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "new_stop": self.new_stop,
            "exit_pct": self.exit_pct,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# DynamicStopManager
# ---------------------------------------------------------------------------

@dataclass
class _PositionState:
    entry_price: float
    stop_loss: float
    take_profit: float
    direction: str
    has_partial_exited: bool = False
    current_stop: float | None = None
    highest_since_entry: float | None = None


class DynamicStopManager:
    """Manages dynamic stop-loss adjustments based on R-multiple thresholds."""

    def __init__(
        self,
        breakeven_trigger_r: float = 1.0,
        partial_exit_trigger_r: float = 2.0,
        partial_exit_pct: float = 0.5,
        trail_activation_r: float = 1.5,
        trail_distance_pct: float = 0.02,
    ) -> None:
        self.breakeven_trigger_r = breakeven_trigger_r
        self.partial_exit_trigger_r = partial_exit_trigger_r
        self.partial_exit_pct = partial_exit_pct
        self.trail_activation_r = trail_activation_r
        self.trail_distance_pct = trail_distance_pct
        self._positions: Dict[str, _PositionState] = {}

    # -- stateless evaluation ------------------------------------------------

    def evaluate(
        self,
        entry_price: float,
        current_price: float,
        stop_loss: float,
        take_profit: float,
        direction: str = "long",
        highest_since_entry: float | None = None,
        *,
        _has_partial_exited: bool = False,
    ) -> StopAction:
        """Return the recommended :class:`StopAction` for a position.

        Parameters
        ----------
        _has_partial_exited:
            Internal flag used by :meth:`update_position` to indicate that a
            partial exit has already occurred.  Callers of the public API
            should not set this directly.
        """
        initial_risk = abs(entry_price - stop_loss)
        if initial_risk == 0:
            return StopAction(action="hold", reason="zero initial risk")

        if direction == "long":
            current_profit = current_price - entry_price
        else:
            current_profit = entry_price - current_price

        r_multiple = current_profit / initial_risk

        # Priority 1: partial exit
        if r_multiple >= self.partial_exit_trigger_r and not _has_partial_exited:
            return StopAction(
                action="partial_exit",
                exit_pct=self.partial_exit_pct,
                reason=f"R-multiple {r_multiple:.2f} >= {self.partial_exit_trigger_r} partial exit trigger",
            )

        # Priority 2: trailing stop
        if r_multiple >= self.trail_activation_r:
            ref = highest_since_entry if highest_since_entry is not None else current_price
            if direction == "long":
                trail_stop = ref * (1 - self.trail_distance_pct)
            else:
                trail_stop = ref * (1 + self.trail_distance_pct)
            return StopAction(
                action="trail",
                new_stop=trail_stop,
                reason=f"R-multiple {r_multiple:.2f} >= {self.trail_activation_r} trail activation",
            )

        # Priority 3: breakeven
        if r_multiple >= self.breakeven_trigger_r:
            buffer = initial_risk * 0.01  # small buffer above entry
            if direction == "long":
                be_stop = entry_price + buffer
            else:
                be_stop = entry_price - buffer
            return StopAction(
                action="move_to_breakeven",
                new_stop=be_stop,
                reason=f"R-multiple {r_multiple:.2f} >= {self.breakeven_trigger_r} breakeven trigger",
            )

        # Default
        return StopAction(action="hold", reason=f"R-multiple {r_multiple:.2f} below thresholds")

    # -- stateful position tracking ------------------------------------------

    def track_position(
        self,
        position_id: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        direction: str,
    ) -> None:
        self._positions[position_id] = _PositionState(
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            direction=direction,
            current_stop=stop_loss,
        )

    def update_position(
        self,
        position_id: str,
        current_price: float,
        high_since_entry: float | None = None,
    ) -> StopAction | None:
        state = self._positions.get(position_id)
        if state is None:
            return None

        # Update high watermark
        if high_since_entry is not None:
            if state.highest_since_entry is None or high_since_entry > state.highest_since_entry:
                state.highest_since_entry = high_since_entry

        action = self.evaluate(
            entry_price=state.entry_price,
            current_price=current_price,
            stop_loss=state.stop_loss,
            take_profit=state.take_profit,
            direction=state.direction,
            highest_since_entry=state.highest_since_entry,
            _has_partial_exited=state.has_partial_exited,
        )

        if action.action == "hold":
            return None

        # Apply state mutations
        if action.action == "partial_exit":
            state.has_partial_exited = True
        if action.new_stop is not None:
            state.current_stop = action.new_stop

        return action

    def get_position_status(self, position_id: str) -> dict | None:
        state = self._positions.get(position_id)
        if state is None:
            return None
        return {
            "position_id": position_id,
            "entry_price": state.entry_price,
            "stop_loss": state.stop_loss,
            "take_profit": state.take_profit,
            "direction": state.direction,
            "has_partial_exited": state.has_partial_exited,
            "current_stop": state.current_stop,
            "highest_since_entry": state.highest_since_entry,
        }

    def remove_position(self, position_id: str) -> None:
        self._positions.pop(position_id, None)

    def get_stats(self) -> dict:
        total = len(self._positions)
        partial_exited = sum(1 for s in self._positions.values() if s.has_partial_exited)
        return {
            "total_tracked": total,
            "partial_exited": partial_exited,
            "active": total - partial_exited,
        }


# ---------------------------------------------------------------------------
# StrategyRiskContribution
# ---------------------------------------------------------------------------

@dataclass
class StrategyRiskContribution:
    """Risk contribution of a single strategy in the portfolio."""

    strategy: str
    current_allocation: float
    volatility: float  # annualized
    var_contribution: float
    target_allocation: float
    adjustment: float  # target - current

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "current_allocation": self.current_allocation,
            "volatility": self.volatility,
            "var_contribution": self.var_contribution,
            "target_allocation": self.target_allocation,
            "adjustment": self.adjustment,
        }


# ---------------------------------------------------------------------------
# RiskParityAllocator
# ---------------------------------------------------------------------------

class RiskParityAllocator:
    """Allocates capital across strategies using inverse-volatility risk parity."""

    def __init__(
        self,
        target_total_risk: float = 0.10,
        max_single_allocation: float = 0.40,
        min_allocation: float = 0.05,
    ) -> None:
        self.target_total_risk = target_total_risk
        self.max_single_allocation = max_single_allocation
        self.min_allocation = min_allocation
        self._vols: Dict[str, float] = {}

    def set_strategy_vol(self, strategy: str, volatility: float) -> None:
        if volatility <= 0:
            raise ValueError(f"Volatility must be positive, got {volatility}")
        self._vols[strategy] = volatility

    def get_inverse_vol_weights(self) -> dict[str, float]:
        """Raw 1/vol weights before clamping, normalized to sum=1."""
        if not self._vols:
            return {}
        inv = {s: 1.0 / v for s, v in self._vols.items()}
        total = sum(inv.values())
        return {s: w / total for s, w in inv.items()}

    def calculate_allocations(self) -> dict[str, StrategyRiskContribution]:
        if not self._vols:
            return {}

        # Step 1: inverse-vol weights
        raw = self.get_inverse_vol_weights()

        # Step 2: clamp and re-normalize (iterate until stable)
        alloc = dict(raw)
        for _ in range(20):  # convergence loop
            clamped = False
            for s in alloc:
                if alloc[s] > self.max_single_allocation:
                    alloc[s] = self.max_single_allocation
                    clamped = True
                if alloc[s] < self.min_allocation:
                    alloc[s] = self.min_allocation
                    clamped = True
            total = sum(alloc.values())
            if total == 0:
                break
            alloc = {s: w / total for s, w in alloc.items()}
            if not clamped:
                break

        # Build result
        result: Dict[str, StrategyRiskContribution] = {}
        for s, target_w in alloc.items():
            vol = self._vols[s]
            var_contrib = target_w * vol * self.target_total_risk
            result[s] = StrategyRiskContribution(
                strategy=s,
                current_allocation=0.0,
                volatility=vol,
                var_contribution=var_contrib,
                target_allocation=target_w,
                adjustment=target_w,
            )
        return result

    def get_rebalance_actions(
        self,
        current_allocations: dict[str, float],
    ) -> list[dict]:
        targets = self.calculate_allocations()
        actions: List[dict] = []

        all_strategies = set(targets.keys()) | set(current_allocations.keys())
        for s in sorted(all_strategies):
            current = current_allocations.get(s, 0.0)
            target = targets[s].target_allocation if s in targets else 0.0
            delta = target - current

            # Update the contribution objects with current allocation info
            if s in targets:
                targets[s].current_allocation = current
                targets[s].adjustment = delta

            if abs(delta) <= 0.02:
                action_type = "hold"
            elif delta > 0:
                action_type = "increase"
            else:
                action_type = "decrease"

            actions.append({
                "strategy": s,
                "action": action_type,
                "from_pct": current,
                "to_pct": target,
                "delta_pct": delta,
            })

        return actions

    def get_stats(self) -> dict:
        allocs = self.calculate_allocations()
        return {
            "num_strategies": len(self._vols),
            "strategies": list(self._vols.keys()),
            "target_total_risk": self.target_total_risk,
            "allocations": {s: c.target_allocation for s, c in allocs.items()},
        }
