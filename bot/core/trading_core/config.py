"""
TradingCoreConfig — single source of truth for parameters shared between
the live bot (BotOrchestrator) and the backtest engine (UnifiedBacktestEngine).

Any parameter that appears in BOTH systems MUST be defined here and only here.
Bot-specific parameters (exchange credentials, Telegram token, etc.) live in
bot/config/schemas.py as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class TradingCoreConfig:
    """
    Unified configuration for live bot and backtest.

    Key design decisions
    --------------------
    cooldown_seconds : int
        Strategy-switch cooldown expressed in **wall-clock seconds** so that
        both systems use the same semantic value.  The backtest engine converts
        this to bars internally via ``bar_duration_seconds``.
        Production default: 600 s (10 min).
        Previous backtest default was 60 bars × 300 s = 18 000 s — WRONG.

    max_daily_loss_pct : float
        Maximum allowed cumulative intraday loss before RiskManager halts new
        entries.  Was 5 % in the bot but 25 % in the old backtest config.
        Unified default: 0.05 (5 %).

    max_position_size_pct : float
        Per-trade position cap as a fraction of current portfolio value.
        Unified default: 0.25 (25 %).
    """

    symbol: str = "BTC/USDT"
    initial_balance: Decimal = Decimal("10000")

    # ── Strategy routing ──────────────────────────────────────────────────────
    # Cooldown in SECONDS — converted to bars by downstream consumers.
    cooldown_seconds: int = 600                  # 10 min  (was 60 bars = 300 min in backtest)
    regime_check_interval_seconds: int = 3600    # 1 hour

    # ── Risk management ───────────────────────────────────────────────────────
    max_daily_loss_pct: float = 0.05             # 5 %  (was 25 % in old backtest)
    max_position_size_pct: float = 0.25          # 25 % of portfolio per trade
    min_order_size: Decimal = Decimal("10")      # minimum USDT order

    # ── Position sizing ───────────────────────────────────────────────────────
    risk_per_trade: Decimal = Decimal("0.02")    # 2 % of portfolio risked per trade
    max_position_pct: Decimal = Decimal("0.25")  # max 25 % of portfolio in one position

    # ── Strategies enabled ────────────────────────────────────────────────────
    enable_grid: bool = True
    enable_dca: bool = True
    enable_trend_follower: bool = True
    enable_smc: bool = False

    # ── Exchange simulation (backtest only, ignored in live) ──────────────────
    maker_fee: Decimal = Decimal("0.0002")       # Bybit VIP0 maker fee
    taker_fee: Decimal = Decimal("0.00055")      # Bybit VIP0 taker fee
    slippage: Decimal = Decimal("0.0003")        # realistic average slippage

    # ── Execution loop ────────────────────────────────────────────────────────
    analyze_every_n_bars: int = 4               # run strategy.analyze_market() every N M5 bars

    def cooldown_bars(self, bar_duration_seconds: int = 300) -> int:
        """Convert cooldown_seconds to bar count for a given bar duration.

        Args:
            bar_duration_seconds: Duration of one bar in seconds.
                Default 300 = M5 (5-minute bars).

        Returns:
            Number of bars equivalent to cooldown_seconds.
            Minimum 1 to prevent zero-cooldown bugs.

        Example:
            >>> cfg = TradingCoreConfig(cooldown_seconds=600)
            >>> cfg.cooldown_bars(bar_duration_seconds=300)  # M5
            2
            >>> cfg.cooldown_bars(bar_duration_seconds=60)   # M1
            10
        """
        return max(1, self.cooldown_seconds // bar_duration_seconds)

    def regime_check_bars(self, bar_duration_seconds: int = 300) -> int:
        """Convert regime_check_interval_seconds to bar count."""
        return max(1, self.regime_check_interval_seconds // bar_duration_seconds)

    def max_daily_loss_absolute(self) -> Decimal:
        """Return max daily loss as absolute USDT amount."""
        return self.initial_balance * Decimal(str(self.max_daily_loss_pct))

    def max_position_size_absolute(self) -> Decimal:
        """Return max position size as absolute USDT amount."""
        return self.initial_balance * Decimal(str(self.max_position_size_pct))
