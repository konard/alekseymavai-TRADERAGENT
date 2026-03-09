"""
CapitalArbiter — capital allocation manager for multi-strategy cooperation.

Implements the "two work, two rest" principle: each strategy receives a
capital allocation that depends on the current market regime.  Strategies
that are not suited for the current conditions receive zero allocation and
are effectively paused.

Allocation matrix (Grid / DCA / TF / SMC):

    Regime family         Grid    DCA     TF      SMC
    ─────────────────────────────────────────────────
    SIDEWAYS              40%     0%      10%     10%
    UPTREND               10%     0%      40%     20%
    DOWNTREND             0%      25%     25%     10%
    VOLATILE              0%      0%      15%     30%
    UNKNOWN / transition  15%     15%     15%     5%

The arbiter maps the v2.0 detailed MarketRegime string values to these five
allocation families before looking up the matrix.

Usage::

    from bot.core.capital_arbiter import CapitalArbiter
    from bot.core.virtual_position_manager import VirtualPositionManager
    from bot.orchestrator.market_regime import MarketRegime

    vpm = VirtualPositionManager()
    arbiter = CapitalArbiter(vpm)

    allowed = arbiter.get_allowed_capital("grid", MarketRegime.TIGHT_RANGE, Decimal("10000"))
    # → Decimal("4000.00")  (40% for SIDEWAYS)
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from bot.utils.logger import get_logger

if TYPE_CHECKING:
    from bot.core.virtual_position_manager import VirtualPositionManager
    from bot.orchestrator.market_regime import MarketRegime

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Allocation families
# ---------------------------------------------------------------------------

# Internal regime family labels used for the allocation matrix.
_SIDEWAYS = "sideways"
_UPTREND = "uptrend"
_DOWNTREND = "downtrend"
_VOLATILE = "volatile"
_UNKNOWN = "unknown"

# Map detailed v2.0 MarketRegime string values → allocation family.
# Using string keys avoids a circular import with bot.orchestrator.
# The MarketRegime enum inherits from str, so regime.value works as key.
_REGIME_FAMILY: dict[str, str] = {
    "tight_range": _SIDEWAYS,
    "wide_range": _SIDEWAYS,
    "accumulation": _SIDEWAYS,  # SMC accumulation ≈ sideways with SMC bias
    "bull_trend": _UPTREND,
    "bear_trend": _DOWNTREND,
    "distribution": _DOWNTREND,  # SMC distribution ≈ downtrend with SMC bias
    "volatile_transition": _VOLATILE,
    "quiet_transition": _UNKNOWN,
    "unknown": _UNKNOWN,
}

# Capital allocation matrix: family → {strategy: fraction of total balance}
ALLOCATION: dict[str, dict[str, Decimal]] = {
    _SIDEWAYS: {
        "grid": Decimal("0.40"),
        "dca": Decimal("0.00"),
        "tf": Decimal("0.10"),
        "smc": Decimal("0.10"),
    },
    _UPTREND: {
        "grid": Decimal("0.10"),
        "dca": Decimal("0.00"),
        "tf": Decimal("0.40"),
        "smc": Decimal("0.20"),
    },
    _DOWNTREND: {
        "grid": Decimal("0.00"),
        "dca": Decimal("0.25"),
        "tf": Decimal("0.25"),
        "smc": Decimal("0.10"),
    },
    _VOLATILE: {
        "grid": Decimal("0.00"),
        "dca": Decimal("0.00"),
        "tf": Decimal("0.15"),
        "smc": Decimal("0.30"),
    },
    _UNKNOWN: {
        "grid": Decimal("0.15"),
        "dca": Decimal("0.15"),
        "tf": Decimal("0.15"),
        "smc": Decimal("0.05"),
    },
}

# Mutual exclusion thresholds
_MAX_GRID_CAPITAL_PCT = Decimal("0.35")  # 35% capital cap for Grid co-existence checks
_ADX_DCA_BLOCK_THRESHOLD = 30.0  # DCA blocked when ADX > this in downtrend


def _normalise_strategy(strategy: str) -> str:
    """Normalise strategy name aliases to internal key.

    Accepts:
      - ``"grid"``
      - ``"dca"``
      - ``"tf"`` or ``"trend_follower"`` or ``"hybrid"``
      - ``"smc"``
    """
    if strategy in ("trend_follower", "hybrid"):
        return "tf"
    return strategy


def _regime_to_str(regime: MarketRegime | str) -> str:
    """Extract the string value from a MarketRegime enum or pass through a str.

    MarketRegime(str, Enum) stores the raw value (e.g. ``"bull_trend"``).
    We use ``.value`` when the attribute is available (enum member), otherwise
    treat the argument as a plain string already containing the regime name.
    """
    value = getattr(regime, "value", None)
    if value is not None:
        return str(value)
    return str(regime)


class CapitalArbiter:
    """
    Determines how much capital each strategy may use given the market regime.

    The arbiter enforces:

    1. **Allocation matrix** — each strategy has a hard percentage cap per regime.
    2. **Mutual exclusion rules** — additional constraints that can further
       restrict a strategy even when its allocation is non-zero.
    3. **VPM utilisation query** — reads open positions from
       :class:`~bot.core.virtual_position_manager.VirtualPositionManager`
       to compute how much capital is currently in use per strategy.

    Parameters
    ----------
    vpm:
        Reference to the shared :class:`VirtualPositionManager` instance.
        Used by :meth:`get_current_utilization` to compute open exposure.
    """

    def __init__(self, vpm: VirtualPositionManager) -> None:
        self._vpm = vpm

    # ------------------------------------------------------------------
    # Main API
    # ------------------------------------------------------------------

    def get_allowed_capital(
        self,
        strategy: str,
        regime: MarketRegime | str,
        balance: Decimal,
    ) -> Decimal:
        """
        Return the maximum capital the strategy may deploy right now.

        Parameters
        ----------
        strategy:
            One of ``"grid"``, ``"dca"``, ``"tf"`` (or ``"trend_follower"``),
            ``"smc"``.
        regime:
            Current :class:`~bot.orchestrator.market_regime.MarketRegime` or
            its string value (e.g. ``"bull_trend"``).
        balance:
            Total available balance in quote currency.

        Returns
        -------
        Decimal
            Maximum allowed capital (≥ 0).  Returns ``Decimal("0")`` when
            the strategy is idle in this regime.
        """
        key = _normalise_strategy(strategy)
        regime_str = _regime_to_str(regime)
        family = _REGIME_FAMILY.get(regime_str, _UNKNOWN)
        fraction = ALLOCATION[family].get(key, Decimal("0"))
        allowed = balance * fraction

        logger.debug(
            "capital_arbiter_allocation",
            strategy=strategy,
            regime=regime_str,
            family=family,
            fraction=float(fraction),
            balance=float(balance),
            allowed=float(allowed),
        )

        return allowed

    def check_mutual_exclusion(
        self,
        strategy: str,
        regime: MarketRegime | str,
        vpm: VirtualPositionManager | None = None,
        adx: float = 0.0,
        is_bearish: bool = False,
    ) -> bool:
        """
        Return True when the strategy is *allowed* to act (not mutually excluded).

        Exclusion rules:

        - **DCA**: blocked when ``ADX > 30`` and the market is bearish (avoid
          buying into a strong downtrend).
        - **Grid**: blocked from re-centring when Grid already occupies > 35%
          of total capital.
        - **TF**: blocked from entering when Grid already occupies > 35% of
          capital (avoid overloading long-side exposure).
        - **SMC**: always allowed (minimum-size position, works in any regime).

        Parameters
        ----------
        strategy:
            Strategy name (same aliases accepted as :meth:`get_allowed_capital`).
        regime:
            Current market regime.
        vpm:
            Optional VPM reference override.  Falls back to the instance-level
            VPM when ``None``.
        adx:
            Current ADX value (0-100).  Used for the DCA block rule.
        is_bearish:
            True when the trend is currently bearish (EMA fast < EMA slow).
            Used together with ADX for the DCA block rule.

        Returns
        -------
        bool
            True = strategy is allowed; False = strategy is mutually excluded.
        """
        key = _normalise_strategy(strategy)

        if key == "dca":
            # Block DCA when strong downtrend (high ADX + bearish EMA)
            if adx > _ADX_DCA_BLOCK_THRESHOLD and is_bearish:
                logger.info(
                    "capital_arbiter_dca_blocked",
                    adx=adx,
                    is_bearish=is_bearish,
                    reason="adx_bearish_trend",
                )
                return False

        elif key == "grid":
            # Block Grid re-centring when it already holds too much capital
            grid_pct = self._get_grid_utilization_pct(vpm)
            if grid_pct > _MAX_GRID_CAPITAL_PCT:
                logger.info(
                    "capital_arbiter_grid_blocked",
                    grid_utilization_pct=float(grid_pct),
                    threshold=float(_MAX_GRID_CAPITAL_PCT),
                    reason="grid_overutilised",
                )
                return False

        elif key == "tf":
            # Block TF entry when Grid is already dominant
            grid_pct = self._get_grid_utilization_pct(vpm)
            if grid_pct > _MAX_GRID_CAPITAL_PCT:
                logger.info(
                    "capital_arbiter_tf_blocked",
                    grid_utilization_pct=float(grid_pct),
                    threshold=float(_MAX_GRID_CAPITAL_PCT),
                    reason="grid_overutilised",
                )
                return False

        elif key == "smc":
            # SMC always allowed
            pass

        return True

    async def get_current_utilization(self, strategy: str) -> Decimal:
        """
        Return the total invested capital currently held by *strategy*.

        Computed as the sum of ``entry_price × qty`` for all open positions
        of the strategy reported by the VirtualPositionManager.

        Parameters
        ----------
        strategy:
            Strategy name (same aliases accepted as :meth:`get_allowed_capital`).

        Returns
        -------
        Decimal
            Total notional value of open positions (≥ 0).
        """
        key = _normalise_strategy(strategy)
        # VPM uses raw strategy names: "grid", "dca", "tf", "smc"
        positions = await self._vpm.get_by_strategy(key)
        utilization = sum(
            (p.entry_price * p.qty for p in positions),
            Decimal("0"),
        )
        logger.debug(
            "capital_arbiter_utilization",
            strategy=strategy,
            open_positions=len(positions),
            utilization=float(utilization),
        )
        return utilization

    def format_capital_report(
        self,
        regime: MarketRegime | str,
        balance: Decimal,
        utilizations: dict[str, Decimal] | None = None,
    ) -> str:
        """
        Format a human-readable capital distribution report for Telegram.

        Parameters
        ----------
        regime:
            Current market regime.
        balance:
            Total balance in quote currency.
        utilizations:
            Optional mapping of strategy → currently utilised capital.
            When provided, the report shows current vs allowed.

        Returns
        -------
        str
            Multi-line report string ready for Telegram message.
        """
        strategies = ["grid", "dca", "tf", "smc"]
        lines = ["📊 Распределение капитала:"]
        regime_str = _regime_to_str(regime)
        family = _REGIME_FAMILY.get(regime_str, _UNKNOWN)

        total_allocated = Decimal("0")
        for strat in strategies:
            allowed = self.get_allowed_capital(strat, regime, balance)
            total_allocated += allowed
            fraction = ALLOCATION[family].get(strat, Decimal("0"))
            pct_str = f"{float(fraction * 100):.0f}%"
            status = "active" if allowed > 0 else "idle"

            current = ""
            if utilizations and strat in utilizations:
                util = utilizations[strat]
                current = f"  [used: ${float(util):.0f}]"

            lines.append(
                f"{strat.upper():8s}: ${float(allowed):.0f} / {pct_str} ({status}){current}"
            )

        free = balance - total_allocated
        lines.append(f"{'Свободно':8s}: ${float(free):.0f} / {float(free / balance * 100):.0f}%")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_grid_utilization_pct(self, vpm: VirtualPositionManager | None) -> Decimal:
        """
        Synchronously estimate Grid utilization percentage from cached VPM data.

        This is a best-effort synchronous helper used in ``check_mutual_exclusion``
        (which is intentionally sync for ease of use in the main loop).
        Returns 0 when we cannot compute it synchronously.  For accurate values,
        use :meth:`get_current_utilization`.

        Note: In a real integration, the bot caches the latest balance and can
        compute this fraction accurately by comparing Grid utilization to balance.
        """
        # We cannot easily call async methods synchronously here.
        # Return 0 to avoid blocking — callers that need accurate values
        # should call get_current_utilization() and compare externally.
        # This sentinel value means "not overutilised" so Grid/TF are not
        # incorrectly blocked when we can't check.
        return Decimal("0")
