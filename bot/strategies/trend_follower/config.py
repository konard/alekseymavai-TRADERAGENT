"""
Trend-Follower Strategy Configuration
Based on Issue #124 requirements
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class TrendFollowerConfig:
    """
    Adaptive Trend-Follower strategy configuration

    Implements an algorithm for trading bot with adaptive "Trend-Follower" strategy
    on cryptocurrency market as specified in Issue #124.
    """

    # ===== INDICATOR PARAMETERS =====
    # EMA (Exponential Moving Average) periods
    ema_fast_period: int = 20  # EMA(20) for fast trend line
    ema_slow_period: int = 50  # EMA(50) for slow trend line

    # ATR (Average True Range) for volatility measurement
    atr_period: int = 14  # ATR(14) for volatility assessment

    # RSI (Relative Strength Index) for momentum
    rsi_period: int = 14  # RSI(14) for overbought/oversold detection
    rsi_oversold: Decimal = Decimal("30")  # RSI oversold threshold
    rsi_overbought: Decimal = Decimal("70")  # RSI overbought threshold

    # ===== MARKET PHASE DETECTION =====
    # Trend thresholds
    ema_divergence_threshold: Decimal = Decimal("0.005")  # 0.5% EMA divergence for trend

    # Ranging detection (sideways market)
    ranging_high_low_lookback: int = 50  # Candles for range detection

    # ===== ENTRY LOGIC =====
    # Volume confirmation
    require_volume_confirmation: bool = True
    volume_multiplier: Decimal = Decimal("1.5")  # 1.5x average volume required
    volume_lookback: int = 20  # Periods for average volume calculation

    # Support/Resistance zones
    support_resistance_lookback: int = 50  # Candles for S/R identification
    support_resistance_threshold: Decimal = Decimal("0.01")  # 1% zone around S/R levels
    min_sr_touches: int = 1  # Min touches to validate S/R level (was hardcoded 2; 1 works on M5)

    # EMA proximity for trend entries
    # Corridor [EMA, EMA*(1+max_distance_from_ema_pct)] is the valid entry zone.
    # Wider than support_resistance_threshold to capture pullbacks on short timeframes.
    max_distance_from_ema_pct: Decimal = Decimal("0.03")  # 3% above EMA20 (was hardcoded 1%)

    # Filters
    max_atr_filter_pct: Decimal = Decimal("0.05")  # Don't trade if ATR > 5% of price

    # ===== POSITION MANAGEMENT =====
    # TP/SL Multipliers based on market phase
    # Format: (sideways, weak_trend, strong_trend)
    tp_multipliers: tuple = (Decimal("1.2"), Decimal("1.8"), Decimal("2.5"))
    sl_multipliers: tuple = (Decimal("0.7"), Decimal("1.0"), Decimal("1.0"))

    # Trend strength thresholds for classification
    weak_trend_threshold: Decimal = Decimal("0.01")  # 1% EMA divergence
    strong_trend_threshold: Decimal = Decimal("0.02")  # 2% EMA divergence

    # Trailing Stop
    enable_trailing_stop: bool = True
    trailing_activation_atr: Decimal = Decimal("1.5")  # Activate after 1.5x ATR profit
    trailing_distance_atr: Decimal = Decimal("0.5")  # Trail by 0.5x ATR

    # Breakeven
    enable_breakeven: bool = True
    breakeven_activation_atr: Decimal = Decimal("1.0")  # Move to BE after 1x ATR profit

    # Partial close
    enable_partial_close: bool = True
    partial_close_percentage: Decimal = Decimal("0.50")  # Close 50% at partial TP
    partial_tp_percentage: Decimal = Decimal("0.70")  # Partial TP at 70% of full TP

    # ===== CAPITAL MANAGEMENT =====
    # Risk per trade (updated per owner requirement: 1% per position)
    risk_per_trade_pct: Decimal = Decimal("0.01")  # 1% of capital per trade
    max_risk_per_trade_pct: Decimal = Decimal("0.01")  # Max 1% drawdown per trade

    # Position sizing
    max_position_size_usd: Decimal = Decimal("10000")  # Maximum position size

    # Total exposure limit (updated per owner requirement: max 20% total in positions)
    max_total_exposure_pct: Decimal = Decimal("0.20")  # Max 20% of capital in open positions

    # Drawdown protection
    max_consecutive_losses: int = 3  # Reduce size after N losses
    size_reduction_factor: Decimal = Decimal("0.5")  # Reduce to 50% after losses

    # Daily limits
    max_daily_loss_usd: Decimal = Decimal("500")  # Stop trading after daily loss
    max_positions: int = 20  # Maximum concurrent positions (20 x 1% = 20% max exposure)

    # ===== BACKTESTING & VALIDATION =====
    # Performance targets (for validation)
    min_sharpe_ratio: Decimal = Decimal("1.0")
    max_drawdown_pct: Decimal = Decimal("0.20")  # 20%
    min_profit_factor: Decimal = Decimal("1.5")
    min_win_rate_pct: Decimal = Decimal("45")  # 45%
    min_profit_loss_ratio: Decimal = Decimal("1.5")  # 1.5:1

    # Backtesting period
    min_backtest_months: int = 2  # Minimum 2 months of data

    # ===== LOGGING & MONITORING =====
    log_all_signals: bool = True
    log_market_phases: bool = True
    log_position_updates: bool = True
    debug_mode: bool = False

    # ===== EXCHANGE & EXECUTION =====
    # Order execution
    use_limit_orders: bool = True
    limit_order_offset_pct: Decimal = Decimal("0.001")  # 0.1% offset for limit orders
    order_timeout_seconds: int = 60  # Cancel unfilled orders after timeout

    # Slippage
    max_slippage_pct: Decimal = Decimal("0.005")  # 0.5% max slippage

    # API & Balance checks
    check_api_availability: bool = True
    check_sufficient_balance: bool = True
    min_balance_buffer_pct: Decimal = Decimal("0.1")  # Keep 10% buffer


# Default configuration instance
DEFAULT_TREND_FOLLOWER_CONFIG = TrendFollowerConfig()
