"""
Unified Strategy Parameter Model — Level 1 (Universal) and Level 2 (Shared).

All strategies share a common vocabulary for risk/position parameters so that:
- Fair comparison during optimisation (same risk per trade)
- Unified grid-search over risk parameters
- Readable YAML config (single ``strategy_params:`` block)

Architecture
------------
Level 1 — UniversalStrategyParams
    Required for ALL strategies, identical name everywhere.

Level 2 — SharedPerformanceParams
    Same concept, potentially different implementation per strategy,
    but unified naming so the orchestrator can apply them generically.

Level 3 — Strategy-specific params (Grid/DCA/SMC/TF unique fields)
    Defined inside each strategy's own config / adapter; not touched here.

Usage::

    params = UniversalStrategyParams(
        symbol="BTC/USDT",
        risk_per_trade_pct=Decimal("0.01"),
    )
    cfg = OrchestratorBacktestConfig(
        symbol=params.symbol,
        risk_per_trade=params.risk_per_trade_pct,
        ...
    )
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class UniversalStrategyParams:
    """
    Level 1 — Universal parameters required by every strategy.

    These names are canonical across Grid, DCA, SMC, and TrendFollower.
    Old per-strategy names (``amount_per_grid``, ``risk_per_trade``, …) are
    accepted as constructor aliases with a ``DeprecationWarning``.
    """

    # -------------------------------------------------------------------
    # Mandatory
    # -------------------------------------------------------------------
    symbol: str = "BTC/USDT"

    # Risk per trade expressed as a *fraction* of balance (0.01 = 1 %).
    # All four strategies now use this single canonical name.
    risk_per_trade_pct: Decimal = Decimal("0.01")

    # Maximum position as a fraction of balance (0.30 = 30 %).
    max_position_pct: Decimal = Decimal("0.25")

    # Maximum daily loss as a fraction of balance (0.06 = 6 %).
    max_daily_loss_pct: Decimal = Decimal("0.06")

    # Maximum number of simultaneous positions.
    max_positions: int = 3

    # Volume confirmation filter.
    require_volume_confirmation: bool = True

    # Threshold relative to rolling-average volume (1.5 = 150 % of avg).
    min_volume_multiplier: Decimal = Decimal("1.5")

    # -------------------------------------------------------------------
    # Backward-compatibility shims
    # -------------------------------------------------------------------

    def __post_init__(self) -> None:
        # Ensure Decimal types for numeric fields that callers might pass as str/float.
        for attr in (
            "risk_per_trade_pct",
            "max_position_pct",
            "max_daily_loss_pct",
            "min_volume_multiplier",
        ):
            val = getattr(self, attr)
            if not isinstance(val, Decimal):
                object.__setattr__(self, attr, Decimal(str(val)))

    @classmethod
    def from_dict(cls, data: dict) -> UniversalStrategyParams:
        """
        Create from a dict, accepting legacy field names with deprecation warnings.

        Legacy aliases handled:
        - ``risk_per_trade`` → ``risk_per_trade_pct``
        - ``max_risk_per_trade_pct`` → ``risk_per_trade_pct`` (TF duplicate)
        """
        d = dict(data)

        # Legacy: ``risk_per_trade`` (SMC old name) → ``risk_per_trade_pct``
        if "risk_per_trade" in d and "risk_per_trade_pct" not in d:
            warnings.warn(
                "Parameter 'risk_per_trade' is deprecated; use 'risk_per_trade_pct' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            d["risk_per_trade_pct"] = d.pop("risk_per_trade")

        # Legacy: ``max_risk_per_trade_pct`` (TrendFollower duplicate) → ``risk_per_trade_pct``
        if "max_risk_per_trade_pct" in d and "risk_per_trade_pct" not in d:
            warnings.warn(
                "Parameter 'max_risk_per_trade_pct' is deprecated; use 'risk_per_trade_pct' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            d["risk_per_trade_pct"] = d.pop("max_risk_per_trade_pct")
        elif "max_risk_per_trade_pct" in d:
            d.pop("max_risk_per_trade_pct")  # silently drop duplicate

        # Only pass known fields
        known = set(cls.__dataclass_fields__)
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


@dataclass
class SharedPerformanceParams:
    """
    Level 2 — Shared performance/risk parameters.

    Same concept across strategies, unified naming.  Each strategy maps
    these to its own internal config fields in its factory/adapter.
    """

    # Minimum risk-to-reward ratio for entry signals.
    min_risk_reward: Decimal = Decimal("2.0")

    # Maximum drawdown before strategy is paused (fraction, 0.15 = 15 %).
    max_drawdown_pct: Decimal = Decimal("0.15")

    # Minimum Sharpe ratio (for validation / filtering).
    min_sharpe_ratio: Decimal = Decimal("1.0")

    # Minimum profit factor (for validation / filtering).
    min_profit_factor: Decimal = Decimal("1.5")

    # Trailing stop settings — unified names.
    enable_trailing_stop: bool = True

    # Activate trailing stop after position is up by this fraction (0.015 = 1.5 %).
    trailing_activation_pct: Decimal = Decimal("0.015")

    def __post_init__(self) -> None:
        for attr in (
            "min_risk_reward",
            "max_drawdown_pct",
            "min_sharpe_ratio",
            "min_profit_factor",
            "trailing_activation_pct",
        ):
            val = getattr(self, attr)
            if not isinstance(val, Decimal):
                object.__setattr__(self, attr, Decimal(str(val)))

    @classmethod
    def from_dict(cls, data: dict) -> SharedPerformanceParams:
        """Create from a dict, accepting legacy field name variants."""
        d = dict(data)

        # Legacy: ``use_trailing_stop`` (SMC) → ``enable_trailing_stop``
        if "use_trailing_stop" in d and "enable_trailing_stop" not in d:
            warnings.warn(
                "Parameter 'use_trailing_stop' is deprecated; use 'enable_trailing_stop' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            d["enable_trailing_stop"] = d.pop("use_trailing_stop")
        elif "use_trailing_stop" in d:
            d.pop("use_trailing_stop")

        # Legacy: ``max_drawdown`` (SMC, absolute fraction) → ``max_drawdown_pct``
        if "max_drawdown" in d and "max_drawdown_pct" not in d:
            warnings.warn(
                "Parameter 'max_drawdown' is deprecated; use 'max_drawdown_pct' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            d["max_drawdown_pct"] = d.pop("max_drawdown")
        elif "max_drawdown" in d:
            d.pop("max_drawdown")

        known = set(cls.__dataclass_fields__)
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)
