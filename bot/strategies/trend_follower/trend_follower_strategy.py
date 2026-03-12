"""
Trend-Follower Strategy - Main Orchestration

Complete adaptive trend-following trading strategy with:
- Market analysis and phase detection
- Entry logic with volume confirmation
- Dynamic position management
- Risk management and capital allocation
- Trade logging and performance tracking
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import pandas as pd

from bot.strategies.trend_follower.config import DEFAULT_TREND_FOLLOWER_CONFIG, TrendFollowerConfig
from bot.strategies.trend_follower.entry_logic import EntryLogicAnalyzer, EntrySignal
from bot.strategies.trend_follower.market_analyzer import MarketAnalyzer, MarketConditions
from bot.strategies.trend_follower.position_manager import ExitReason, Position, PositionManager
from bot.strategies.trend_follower.risk_manager import RiskManager, RiskMetrics
from bot.strategies.trend_follower.trade_logger import TradeLogger
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class TrendFollowerStrategy:
    """
    Complete Adaptive Trend-Follower Trading Strategy

    Implements the full algorithm from Issue #124:

    1. Market Analysis:
       - Calculate EMA(20), EMA(50), ATR(14), RSI(14) in real-time
       - Determine market phase (Bullish/Bearish/Sideways)

    2. Entry Logic:
       - LONG: Pullback to EMA/support (trend) or RSI oversold/breakout (sideways)
       - SHORT: Inverse logic
       - Volume confirmation required
       - ATR filter (don't trade if ATR > 5% of price)

    3. Position Management:
       - Dynamic TP/SL based on ATR and market phase
       - Trailing stop (activate after 1.5 × ATR profit)
       - Breakeven move (after 1 × ATR profit)
       - Partial close (50% at 70% of TP target)

    4. Risk Management:
       - 2% risk per trade
       - Max 1% drawdown per trade
       - Reduce size by 50% after 3 consecutive losses
       - Daily loss limit ($500)
       - Max 3 concurrent positions

    5. Logging:
       - Full trade journal with entry/exit reasons
       - Performance metrics (Sharpe, drawdown, profit factor)
    """

    def __init__(
        self,
        config: Optional[TrendFollowerConfig] = None,
        initial_capital: Decimal = Decimal("10000"),
        log_trades: bool = True,
        log_file_path: Optional[str] = None,
    ):
        """
        Initialize Trend-Follower Strategy

        Args:
            config: Strategy configuration (uses defaults if None)
            initial_capital: Starting capital
            log_trades: Whether to log trades
            log_file_path: Path to trade log file
        """
        self.config = config or DEFAULT_TREND_FOLLOWER_CONFIG

        # Initialize all components
        self.market_analyzer = MarketAnalyzer(
            ema_fast_period=self.config.ema_fast_period,
            ema_slow_period=self.config.ema_slow_period,
            atr_period=self.config.atr_period,
            rsi_period=self.config.rsi_period,
            ema_divergence_threshold=self.config.ema_divergence_threshold,
            ranging_lookback=self.config.ranging_high_low_lookback,
            weak_trend_threshold=self.config.weak_trend_threshold,
            strong_trend_threshold=self.config.strong_trend_threshold,
        )

        self.entry_logic = EntryLogicAnalyzer(
            market_analyzer=self.market_analyzer,
            require_volume_confirmation=self.config.require_volume_confirmation,
            volume_multiplier=self.config.volume_multiplier,
            volume_lookback=self.config.volume_lookback,
            max_atr_filter_pct=self.config.max_atr_filter_pct,
            support_resistance_lookback=self.config.support_resistance_lookback,
            support_resistance_threshold=self.config.support_resistance_threshold,
            min_sr_touches=self.config.min_sr_touches,
            max_distance_from_ema_pct=self.config.max_distance_from_ema_pct,
            rsi_oversold=self.config.rsi_oversold,
            rsi_overbought=self.config.rsi_overbought,
        )

        self.position_manager = PositionManager(
            tp_multipliers=self.config.tp_multipliers,
            sl_multipliers=self.config.sl_multipliers,
            enable_breakeven=self.config.enable_breakeven,
            breakeven_activation_atr=self.config.breakeven_activation_atr,
            enable_trailing=self.config.enable_trailing_stop,
            trailing_activation_atr=self.config.trailing_activation_atr,
            trailing_distance_atr=self.config.trailing_distance_atr,
            enable_partial_close=self.config.enable_partial_close,
            partial_close_percentage=self.config.partial_close_percentage,
            partial_tp_percentage=self.config.partial_tp_percentage,
        )

        self.risk_manager = RiskManager(
            initial_capital=initial_capital,
            risk_per_trade_pct=self.config.risk_per_trade_pct,
            max_risk_per_trade_pct=self.config.max_risk_per_trade_pct,
            max_position_size_usd=self.config.max_position_size_usd,
            max_consecutive_losses=self.config.max_consecutive_losses,
            size_reduction_factor=self.config.size_reduction_factor,
            max_daily_loss_usd=self.config.max_daily_loss_usd,
            max_positions=self.config.max_positions,
            min_balance_buffer_pct=self.config.min_balance_buffer_pct,
        )

        self.trade_logger: Optional[TradeLogger]
        if log_trades:
            self.trade_logger = TradeLogger(
                log_file_path=log_file_path,
                log_to_file=True,
                log_to_console=self.config.log_all_signals,
            )
        else:
            self.trade_logger = None

        # State tracking
        self.current_market_conditions: Optional[MarketConditions] = None
        self.last_signal: Optional[EntrySignal] = None

        logger.info(
            "TrendFollowerStrategy initialized",
            initial_capital=float(initial_capital),
            config=self.config.__class__.__name__,
            logging_enabled=log_trades,
        )

    def analyze_market(self, df: pd.DataFrame) -> MarketConditions:
        """
        Analyze current market conditions

        Args:
            df: DataFrame with OHLCV data

        Returns:
            MarketConditions object
        """
        self.current_market_conditions = self.market_analyzer.analyze(df)

        if self.config.log_market_phases:
            logger.info(
                "Market analyzed",
                phase=self.current_market_conditions.phase,
                trend_strength=self.current_market_conditions.trend_strength,
                price=float(self.current_market_conditions.current_price),
                ema_divergence=float(self.current_market_conditions.ema_divergence_pct),
                atr_pct=float(self.current_market_conditions.atr_pct),
                rsi=float(self.current_market_conditions.rsi),
            )

        return self.current_market_conditions

    def check_entry_signal(
        self, df: pd.DataFrame, current_balance: Decimal
    ) -> Optional[tuple[EntrySignal, RiskMetrics, Decimal]]:
        """
        Check for entry signals and validate with risk management

        Args:
            df: DataFrame with OHLCV data
            current_balance: Current account balance

        Returns:
            Tuple of (EntrySignal, RiskMetrics, position_size) if entry valid,
            None otherwise
        """
        # Analyze entry conditions
        entry_signal = self.entry_logic.analyze_entry(df)

        if not entry_signal:
            return None

        self.last_signal = entry_signal

        # Check risk management
        risk_metrics = self.risk_manager.check_can_trade(entry_signal, current_balance)

        if not risk_metrics.can_trade:
            logger.info(
                "Trade rejected by risk management",
                reason=risk_metrics.rejection_reason,
                signal_type=entry_signal.signal_type,
            )
            return None

        position_size = risk_metrics.max_position_size_allowed

        logger.info(
            "Entry signal validated",
            type=entry_signal.signal_type,
            reason=entry_signal.entry_reason,
            price=float(entry_signal.entry_price),
            size=float(position_size),
            confidence=float(entry_signal.confidence),
        )

        return entry_signal, risk_metrics, position_size

    def open_position(self, entry_signal: EntrySignal, position_size: Decimal) -> str:
        """
        Open a new position

        Args:
            entry_signal: Validated entry signal
            position_size: Position size to open

        Returns:
            Position ID
        """
        position_id = str(uuid.uuid4())

        # Open position with position manager
        position = self.position_manager.open_position(
            entry_signal=entry_signal, position_size=position_size, position_id=position_id
        )

        # Update risk manager.
        # position_size is already in USD (quote currency), so position_value == position_size.
        # Multiplying by entry_price would be a unit error: price × USD ≠ USD.
        self.risk_manager.position_opened(position_value=position_size)

        logger.info(
            "Position opened",
            id=position_id,
            type=entry_signal.signal_type,
            entry=float(entry_signal.entry_price),
            size=float(position_size),
            sl=float(position.levels.stop_loss),
            tp=float(position.levels.take_profit),
        )

        return position_id

    def update_position(
        self, position_id: str, current_price: Decimal, df: pd.DataFrame
    ) -> Optional[ExitReason]:
        """
        Update position with current price

        Args:
            position_id: Position identifier
            current_price: Current market price
            df: Current market data for analysis

        Returns:
            ExitReason if position should be closed, None otherwise
        """
        # Get current market conditions
        market_conditions = self.market_analyzer.analyze(df)

        # Update position
        exit_reason = self.position_manager.update_position(
            position_id=position_id,
            current_price=current_price,
            market_conditions=market_conditions,
        )

        if exit_reason and self.config.log_position_updates:
            logger.info(
                "Position exit triggered",
                id=position_id,
                reason=exit_reason,
                price=float(current_price),
            )

        return exit_reason

    def close_position(
        self, position_id: str, exit_reason: ExitReason, exit_price: Decimal
    ) -> None:
        """
        Close position and record trade

        Args:
            position_id: Position identifier
            exit_reason: Reason for closure
            exit_price: Exit price
        """
        if position_id not in self.position_manager.active_positions:
            logger.warning("Attempted to close non-existent position", id=position_id)
            return

        position = self.position_manager.active_positions[position_id]

        # Record trade in risk manager
        self.risk_manager.record_trade(
            signal_type=position.signal_type,
            entry_price=position.entry_price,
            exit_price=exit_price,
            size=position.size,
        )

        # Log trade if logger enabled
        if self.trade_logger:
            self.trade_logger.log_trade(
                trade_id=position_id,
                entry_signal=position.entry_signal,
                exit_reason=exit_reason,
                exit_price=exit_price,
                exit_time=datetime.now(),
                position_size=position.size,
                stop_loss=position.levels.stop_loss,
                take_profit=position.levels.take_profit,
                max_favorable_excursion=position.max_profit,
                max_adverse_excursion=None,  # Could track this in position manager
                notes=f"Status: {position.status}",
            )

        # Close position in position manager
        self.position_manager.close_position(position_id, exit_reason)

        # position.size is already in USD — same fix as open_position.
        self.risk_manager.position_closed(position_value=position.size)

        logger.info(
            "Position closed and recorded",
            id=position_id,
            reason=exit_reason,
            exit_price=float(exit_price),
        )

    def get_active_positions(self) -> dict[str, Position]:
        """Get all active positions"""
        return self.position_manager.active_positions.copy()

    def get_statistics(self) -> dict:
        """
        Get comprehensive strategy statistics

        Returns:
            Dictionary with performance metrics including:
            - Risk metrics (capital, daily P&L, consecutive losses)
            - Trade statistics (total trades, win rate, profit factor)
            - Performance metrics (Sharpe ratio, max drawdown)
        """
        stats = {
            "risk_metrics": self.risk_manager.get_statistics(),
            "active_positions": len(self.position_manager.active_positions),
            "current_market": None,
        }

        if self.current_market_conditions:
            stats["current_market"] = {
                "phase": self.current_market_conditions.phase,
                "trend_strength": self.current_market_conditions.trend_strength,
                "price": float(self.current_market_conditions.current_price),
                "rsi": float(self.current_market_conditions.rsi),
                "atr_pct": float(self.current_market_conditions.atr_pct),
            }

        if self.trade_logger:
            stats["trade_statistics"] = self.trade_logger.get_statistics()

        return stats

    def validate_performance(self) -> dict:
        """
        Validate strategy performance against requirements from Issue #124

        Performance criteria:
        - Sharpe Ratio > 1.0
        - Max Drawdown < 20%
        - Profit Factor > 1.5
        - Win Rate > 45%
        - Profit/Loss Ratio > 1.5

        Returns:
            Dictionary with validation results
        """
        if not self.trade_logger:
            return {"validated": False, "reason": "Trade logging not enabled"}

        stats = self.trade_logger.get_statistics()

        validation: dict[str, Any] = {"validated": True, "criteria": {}, "failed_criteria": []}

        # Check Sharpe Ratio
        sharpe_pass = stats["sharpe_ratio"] >= float(self.config.min_sharpe_ratio)
        validation["criteria"]["sharpe_ratio"] = {
            "value": stats["sharpe_ratio"],
            "threshold": float(self.config.min_sharpe_ratio),
            "pass": sharpe_pass,
        }
        if not sharpe_pass:
            validation["failed_criteria"].append("sharpe_ratio")

        # Check Max Drawdown
        dd_pass = stats["max_drawdown"] <= float(self.config.max_drawdown_pct) * 100
        validation["criteria"]["max_drawdown"] = {
            "value": stats["max_drawdown"],
            "threshold": float(self.config.max_drawdown_pct) * 100,
            "pass": dd_pass,
        }
        if not dd_pass:
            validation["failed_criteria"].append("max_drawdown")

        # Check Profit Factor
        pf_pass = stats["profit_factor"] >= float(self.config.min_profit_factor)
        validation["criteria"]["profit_factor"] = {
            "value": stats["profit_factor"],
            "threshold": float(self.config.min_profit_factor),
            "pass": pf_pass,
        }
        if not pf_pass:
            validation["failed_criteria"].append("profit_factor")

        # Check Win Rate
        wr_pass = stats["win_rate"] >= float(self.config.min_win_rate_pct)
        validation["criteria"]["win_rate"] = {
            "value": stats["win_rate"],
            "threshold": float(self.config.min_win_rate_pct),
            "pass": wr_pass,
        }
        if not wr_pass:
            validation["failed_criteria"].append("win_rate")

        # Check Profit/Loss Ratio
        if stats["avg_loss"] > 0:
            pl_ratio = stats["avg_win"] / stats["avg_loss"]
        else:
            pl_ratio = float("inf")

        pl_pass = pl_ratio >= float(self.config.min_profit_loss_ratio)
        validation["criteria"]["profit_loss_ratio"] = {
            "value": pl_ratio,
            "threshold": float(self.config.min_profit_loss_ratio),
            "pass": pl_pass,
        }
        if not pl_pass:
            validation["failed_criteria"].append("profit_loss_ratio")

        # Overall validation
        validation["validated"] = len(validation["failed_criteria"]) == 0

        if validation["validated"]:
            logger.info("Strategy performance validation PASSED", criteria=validation["criteria"])
        else:
            logger.warning(
                "Strategy performance validation FAILED",
                failed=validation["failed_criteria"],
                criteria=validation["criteria"],
            )

        return validation
