"""
Pydantic schemas for configuration validation.
Defines the structure and validation rules for bot configurations.
"""

from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class StrategyType(str, Enum):
    """Trading strategy types"""

    GRID = "grid"
    DCA = "dca"
    HYBRID = "hybrid"
    TREND_FOLLOWER = "trend_follower"
    SMC = "smc"


class ExchangeConfig(BaseModel):
    """Exchange connection configuration"""

    exchange_id: str = Field(
        ...,
        description="Exchange identifier (e.g., 'binance', 'bybit')",
        examples=["binance", "bybit", "okx"],
    )
    credentials_name: str = Field(..., description="Name of stored credentials to use")
    sandbox: bool = Field(default=False, description="Use testnet/sandbox mode")
    rate_limit: bool = Field(default=True, description="Enable rate limiting")


class GridConfig(BaseModel):
    """Grid trading strategy configuration"""

    enabled: bool = Field(default=True, description="Enable grid trading")
    upper_price: Decimal = Field(
        ...,
        gt=0,
        description="Upper price boundary for grid",
    )
    lower_price: Decimal = Field(
        ...,
        gt=0,
        description="Lower price boundary for grid",
    )
    grid_levels: int = Field(
        ...,
        ge=2,
        le=100,
        description="Number of grid levels",
    )
    amount_per_grid: Decimal = Field(
        ...,
        gt=0,
        description="Amount to trade per grid level",
    )
    profit_per_grid: Decimal = Field(
        default=Decimal("0.01"),
        gt=0,
        le=1,
        description="Profit percentage per grid (0.01 = 1%)",
    )

    @model_validator(mode="after")
    def validate_price_range(self) -> "GridConfig":
        """Ensure upper price is greater than lower price"""
        if self.upper_price <= self.lower_price:
            raise ValueError("upper_price must be greater than lower_price")
        return self


class DCAConfig(BaseModel):
    """DCA (Dollar Cost Averaging) configuration"""

    enabled: bool = Field(default=True, description="Enable DCA")
    trigger_percentage: Decimal = Field(
        default=Decimal("0.05"),
        gt=0,
        le=1,
        description="Price drop percentage to trigger DCA (0.05 = 5%)",
    )
    amount_per_step: Decimal = Field(
        ...,
        gt=0,
        description="Amount to buy per DCA step",
    )
    max_steps: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of DCA steps",
    )
    take_profit_percentage: Decimal = Field(
        default=Decimal("0.1"),
        gt=0,
        description="Take profit percentage after DCA (0.1 = 10%)",
    )

    # Catch-up mode: place missing DCA levels on bot startup
    catch_up_enabled: bool = Field(
        default=False,
        description="Place missing DCA levels below current price on startup",
    )
    catch_up_max_orders: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of catch-up orders to place on startup",
    )
    catch_up_reference: Literal["current_price", "last_high"] = Field(
        default="current_price",
        description="Reference point for calculating DCA levels: current price or recent high",
    )
    catch_up_lookback_bars: int = Field(
        default=500,
        ge=50,
        le=2000,
        description="Number of 1h bars to look back when finding the reference high",
    )


class TrendFollowerConfig(BaseModel):
    """Trend-Follower Strategy configuration"""

    enabled: bool = Field(default=True, description="Enable Trend-Follower strategy")

    # Market analysis settings
    ema_fast_period: int = Field(
        default=20,
        ge=5,
        le=50,
        description="Fast EMA period for trend detection",
    )
    ema_slow_period: int = Field(
        default=50,
        ge=20,
        le=200,
        description="Slow EMA period for trend detection",
    )
    atr_period: int = Field(
        default=14,
        ge=7,
        le=30,
        description="ATR period for volatility measurement",
    )
    rsi_period: int = Field(
        default=14,
        ge=7,
        le=30,
        description="RSI period for momentum confirmation",
    )

    # Entry settings
    volume_multiplier: Decimal = Field(
        default=Decimal("1.5"),
        gt=1,
        description="Volume multiplier for confirmation (1.5 = 150% of average)",
    )
    atr_filter_threshold: Decimal = Field(
        default=Decimal("0.05"),
        gt=0,
        description="Minimum ATR percentage for volatility filter (0.05 = 5%)",
    )

    # Position management - Take Profit ATR multipliers
    tp_atr_multiplier_sideways: Decimal = Field(
        default=Decimal("1.2"),
        gt=0,
        description="TP ATR multiplier for sideways market",
    )
    tp_atr_multiplier_weak: Decimal = Field(
        default=Decimal("1.8"),
        gt=0,
        description="TP ATR multiplier for weak trend",
    )
    tp_atr_multiplier_strong: Decimal = Field(
        default=Decimal("2.5"),
        gt=0,
        description="TP ATR multiplier for strong trend",
    )

    # Position management - Stop Loss ATR multipliers
    sl_atr_multiplier_sideways: Decimal = Field(
        default=Decimal("0.7"),
        gt=0,
        description="SL ATR multiplier for sideways market",
    )
    sl_atr_multiplier_trend: Decimal = Field(
        default=Decimal("1.0"),
        gt=0,
        description="SL ATR multiplier for trending market",
    )

    # Risk management
    risk_per_trade_pct: Decimal = Field(
        default=Decimal("0.02"),
        gt=0,
        le=0.1,
        description="Risk per trade as percentage of balance (0.02 = 2%)",
    )
    max_position_size_usd: Decimal = Field(
        default=Decimal("5000"),
        gt=0,
        description="Maximum position size in USD",
    )
    max_daily_loss_usd: Decimal = Field(
        default=Decimal("500"),
        gt=0,
        description="Maximum daily loss in USD",
    )
    max_positions: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of concurrent positions",
    )

    # Logging
    log_all_signals: bool = Field(
        default=False,
        description="Log all signals including non-executed ones",
    )


class SMCConfigSchema(BaseModel):
    """Smart Money Concepts strategy configuration"""

    enabled: bool = Field(default=True, description="Enable SMC strategy")

    # Timeframes
    trend_timeframe: str = Field(default="1d", description="Timeframe for global trend (D1)")
    structure_timeframe: str = Field(
        default="4h", description="Timeframe for market structure (H4)"
    )
    working_timeframe: str = Field(default="1h", description="Timeframe for confluence zones (H1)")
    entry_timeframe: str = Field(default="15m", description="Timeframe for entry signals (M15)")

    # Market Structure
    swing_length: int = Field(
        default=10,
        ge=5,
        le=200,
        description="Candles for swing high/low identification (single-TF default)",
    )
    trend_period: int = Field(
        default=20,
        ge=5,
        le=100,
        description="Lookback period for trend detection",
    )
    close_break: bool = Field(
        default=True,
        description="BOS/CHoCH: require candle close beyond level (vs wick)",
    )

    # Per-TF swing_length overrides (M5 two-level entry mode)
    swing_length_m5: int = Field(
        default=20,
        ge=5,
        le=200,
        description="Swing length for M5 entry timeframe (noise filter)",
    )
    swing_length_h1: int = Field(
        default=10,
        ge=5,
        le=200,
        description="Swing length for H1 structure timeframe",
    )

    # OHLCV limits
    m5_limit: int = Field(
        default=1000,
        ge=100,
        le=5000,
        description="Number of M5 candles to fetch (~3.5 days at 1000)",
    )
    h1_limit: int = Field(
        default=200,
        ge=50,
        le=1000,
        description="Number of H1 candles to fetch for structure",
    )

    # Warmup
    warmup_bars: int = Field(
        default=100,
        ge=0,
        le=1000,
        description="Skip signal generation for first N calls to build market structure",
    )

    # Confluence
    close_mitigation: bool = Field(
        default=False,
        description="OB: require close through OB for mitigation (vs wick)",
    )
    join_consecutive_fvg: bool = Field(
        default=False,
        description="FVG: merge adjacent same-direction FVGs",
    )
    liquidity_range_percent: float = Field(
        default=0.01,
        gt=0,
        le=0.1,
        description="Liquidity: percentage range for grouping swing clusters",
    )

    # Risk Management
    risk_per_trade: Decimal = Field(
        default=Decimal("2.0"),
        gt=0,
        le=100,
        description="Risk per trade as percentage of balance (2.0 = 2%)",
    )
    min_risk_reward: Decimal = Field(
        default=Decimal("2.0"),
        gt=0,
        description="Minimum risk:reward ratio",
    )
    max_position_size: Decimal = Field(
        default=Decimal("10000"),
        gt=0,
        description="Maximum position size in USD",
    )

    # Entry
    require_volume_confirmation: bool = Field(
        default=True,
        description="Require volume confirmation for entries",
    )
    min_volume_multiplier: Decimal = Field(
        default=Decimal("1.5"),
        gt=0,
        description="Minimum volume multiplier (1.5 = 150% of average)",
    )

    # Position Management
    use_trailing_stop: bool = Field(
        default=True,
        description="Enable trailing stop",
    )
    trailing_stop_activation: Decimal = Field(
        default=Decimal("0.015"),
        gt=0,
        description="Trailing stop activation at profit percentage (0.015 = 1.5%)",
    )
    trailing_stop_distance: Decimal = Field(
        default=Decimal("0.005"),
        gt=0,
        description="Trailing stop distance percentage (0.005 = 0.5%)",
    )

    # Limits
    max_positions: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of concurrent positions",
    )


class RiskManagementConfig(BaseModel):
    """Risk management configuration"""

    max_position_size: Decimal = Field(
        ...,
        gt=0,
        description="Maximum position size in quote currency",
    )
    stop_loss_percentage: Decimal | None = Field(
        default=None,
        gt=0,
        le=1,
        description="Stop loss percentage (0.2 = 20%)",
    )
    max_daily_loss: Decimal | None = Field(
        default=None,
        gt=0,
        description="Maximum daily loss in quote currency",
    )
    min_order_size: Decimal = Field(
        default=Decimal("10"),
        gt=0,
        description="Minimum order size in quote currency",
    )
    max_position_size_per_trade: Decimal | None = Field(
        default=None,
        gt=0,
        description="Max size of a single entry in quote currency. None = no per-trade cap.",
    )
    global_stop_loss_pct: Decimal | None = Field(
        default=None,
        gt=0,
        le=1,
        description=(
            "Per-symbol global stop-loss as fraction of total capital (e.g. 0.025 = 2.5%). "
            "When aggregate exposure across all strategies on one symbol exceeds this "
            "threshold, force_close_all is triggered. None disables the feature."
        ),
    )
    force_close_cooldown_seconds: int = Field(
        default=1800,
        ge=0,
        description=(
            "Cooldown in seconds after a force_close_all event before new entries "
            "on that symbol are allowed again (default 1800 = 30 min)."
        ),
    )


class NotificationConfig(BaseModel):
    """Notification configuration"""

    enabled: bool = Field(default=False, description="Enable notifications")
    telegram_bot_token: str | None = Field(default=None, description="Telegram bot token")
    telegram_chat_id: str | None = Field(default=None, description="Telegram chat ID")
    notify_on_trade: bool = Field(default=True, description="Notify on trade execution")
    notify_on_error: bool = Field(default=True, description="Notify on errors")


class BotConfig(BaseModel):
    """Main bot configuration"""

    version: int = Field(default=1, ge=1, description="Configuration version")
    name: str = Field(..., min_length=1, max_length=100, description="Bot name")
    symbol: str = Field(..., description="Trading pair symbol (e.g., 'BTC/USDT')")
    strategy: StrategyType = Field(..., description="Trading strategy type")

    # Sub-configurations
    exchange: ExchangeConfig
    grid: GridConfig | None = Field(default=None, description="Grid trading configuration")
    dca: DCAConfig | None = Field(default=None, description="DCA configuration")
    trend_follower: TrendFollowerConfig | None = Field(
        default=None, description="Trend-Follower strategy configuration"
    )
    smc: SMCConfigSchema | None = Field(default=None, description="SMC strategy configuration")
    risk_management: RiskManagementConfig
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)

    # Operational settings
    dry_run: bool = Field(default=False, description="Run in simulation mode without real orders")
    auto_start: bool = Field(default=False, description="Auto-start bot on initialization")
    strategy_switch_cooldown_seconds: int = Field(
        default=600,
        ge=0,
        le=7200,
        description="Minimum seconds between regime-based strategy switches (0 to disable)",
    )
    regime_check_interval_seconds: int = Field(
        default=60,
        ge=10,
        le=3600,
        description="How often (seconds) the bot re-evaluates the market regime and active strategies",
    )
    close_positions_on_switch: bool = Field(
        default=False,
        description="Close open positions when deactivating a strategy (False = hold positions)",
    )

    @model_validator(mode="after")
    def validate_strategy_config(self) -> "BotConfig":
        """Ensure strategy has corresponding configuration"""
        if self.strategy in (StrategyType.GRID, StrategyType.HYBRID):
            if self.grid is None:
                raise ValueError(f"Strategy '{self.strategy}' requires grid configuration")
        if self.strategy in (StrategyType.DCA, StrategyType.HYBRID):
            if self.dca is None:
                raise ValueError(f"Strategy '{self.strategy}' requires dca configuration")
        if self.strategy == StrategyType.TREND_FOLLOWER:
            if self.trend_follower is None:
                raise ValueError("Strategy 'trend_follower' requires trend_follower configuration")
        if self.strategy == StrategyType.SMC:
            if self.smc is None:
                raise ValueError("Strategy 'smc' requires smc configuration")
        return self

    class Config:
        use_enum_values = True
        json_schema_extra = {
            "example": {
                "version": 1,
                "name": "BTC_Grid_Bot_1",
                "symbol": "BTC/USDT",
                "strategy": "grid",
                "exchange": {
                    "exchange_id": "binance",
                    "credentials_name": "binance_main",
                    "sandbox": False,
                },
                "grid": {
                    "enabled": True,
                    "upper_price": 50000,
                    "lower_price": 40000,
                    "grid_levels": 10,
                    "amount_per_grid": 100,
                    "profit_per_grid": 0.01,
                },
                "risk_management": {
                    "max_position_size": 10000,
                    "stop_loss_percentage": 0.15,
                    "min_order_size": 10,
                },
                "dry_run": False,
                "auto_start": False,
            }
        }


class ScannerConfig(BaseModel):
    """Market Scanner configuration for automatic pair discovery."""

    enabled: bool = Field(default=False, description="Enable market scanner")
    pairs: list[str] = Field(
        default_factory=lambda: [
            "BTC/USDT",
            "ETH/USDT",
            "SOL/USDT",
            "XRP/USDT",
            "DOGE/USDT",
        ],
        description="Trading pairs to scan",
    )
    interval_minutes: int = Field(default=15, ge=1, le=1440, description="Scan interval in minutes")
    timeframe: str = Field(default="1h", description="OHLCV timeframe for regime analysis")
    ohlcv_limit: int = Field(default=200, ge=50, le=1000, description="OHLCV candles to fetch")
    min_volume_usdt: float = Field(
        default=1_000_000.0, ge=0, description="Minimum 24h volume in USDT"
    )
    max_spread_pct: float = Field(
        default=0.5, ge=0, le=10.0, description="Maximum bid-ask spread percentage"
    )
    min_liquidity_usdt: float = Field(
        default=50_000.0, ge=0, description="Minimum orderbook liquidity in USDT"
    )


class AutoTradeConfig(BaseModel):
    """Configuration for automatic multi-pair trading."""

    enabled: bool = Field(default=False, description="Enable automatic pair selection and trading")
    max_bots: int = Field(
        default=5, ge=1, le=50, description="Maximum number of simultaneously running bots"
    )
    min_confidence: float = Field(
        default=0.65, ge=0.0, le=1.0, description="Minimum regime confidence to start a bot"
    )
    strategy_template: str = Field(
        default="hybrid",
        description="Strategy template for auto-created bots (grid/dca/hybrid/trend_follower)",
    )
    scanner: ScannerConfig = Field(
        default_factory=ScannerConfig, description="Market scanner configuration"
    )


class AppConfig(BaseModel):
    """Application-wide configuration"""

    # Database
    database_url: str = Field(..., description="PostgreSQL database URL")
    database_pool_size: int = Field(
        default=5, ge=1, le=50, description="Database connection pool size"
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Logging level",
        pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$",
    )
    log_to_file: bool = Field(default=True, description="Enable file logging")
    log_to_console: bool = Field(default=True, description="Enable console logging")
    json_logs: bool = Field(default=False, description="Use JSON format for logs")

    # Encryption
    encryption_key: str = Field(
        ..., description="Encryption key for API credentials (base64 encoded)"
    )

    # Scanner
    scanner: ScannerConfig = Field(
        default_factory=ScannerConfig, description="Market scanner configuration"
    )

    # Auto-trade (multi-pair automation)
    auto_trade: AutoTradeConfig = Field(
        default_factory=AutoTradeConfig, description="Automatic multi-pair trading configuration"
    )

    # Bots
    bots: list[BotConfig] = Field(default_factory=list, description="Bot configurations")

    class Config:
        json_schema_extra = {
            "example": {
                "database_url": "postgresql+asyncpg://user:pass@localhost/traderagent",
                "database_pool_size": 5,
                "log_level": "INFO",
                "log_to_file": True,
                "log_to_console": True,
                "json_logs": False,
                "encryption_key": "base64_encoded_key_here",
                "bots": [],
            }
        }
