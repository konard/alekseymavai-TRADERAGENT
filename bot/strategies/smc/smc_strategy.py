"""
Smart Money Concepts (SMC) Strategy - Complete Integration
Main strategy class coordinating all SMC components
Issue #123
"""

from decimal import Decimal
from typing import Optional

import pandas as pd

from bot.strategies.smc.config import DEFAULT_SMC_CONFIG, SMCConfig
from bot.strategies.smc.confluence_zones import ConfluenceZoneAnalyzer
from bot.strategies.smc.entry_signals import EntrySignalGenerator, SMCSignal
from bot.strategies.smc.entry_signals import SignalDirection as SMCSignalDirection
from bot.strategies.smc.market_structure import MarketStructureAnalyzer, TrendDirection
from bot.strategies.smc.position_manager import PositionManager, PositionMetrics
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class SMCStrategy:
    """
    Complete Smart Money Concepts trading strategy

    Integrates:
    - Market Structure Analysis (trends, BOS, CHoCH)
    - Confluence Zones (Order Blocks, Fair Value Gaps)
    - Entry Signals (Price Action patterns)
    - Position Management (Kelly Criterion, dynamic SL/TP)
    """

    def __init__(
        self, config: Optional[SMCConfig] = None, account_balance: Decimal = Decimal("10000")
    ):
        """
        Initialize SMC Strategy

        Args:
            config: SMC configuration (uses defaults if None)
            account_balance: Starting account balance
        """
        self.config = config or DEFAULT_SMC_CONFIG

        # Initialize all components
        self.market_structure = MarketStructureAnalyzer(
            swing_length=self.config.swing_length,
            trend_period=self.config.trend_period,
            close_break=self.config.close_break,
        )

        self.confluence_analyzer = ConfluenceZoneAnalyzer(
            market_structure=self.market_structure,
            timeframe=self.config.working_timeframe,
            close_mitigation=self.config.close_mitigation,
            join_consecutive_fvg=self.config.join_consecutive_fvg,
            liquidity_range_percent=self.config.liquidity_range_percent,
        )

        self.signal_generator = EntrySignalGenerator(
            market_structure=self.market_structure,
            confluence_analyzer=self.confluence_analyzer,
            min_risk_reward=self.config.min_risk_reward,
            sl_buffer_pct=0.5,
        )

        self.position_manager = PositionManager(
            market_structure=self.market_structure,
            account_balance=account_balance,
            risk_per_trade_pct=self.config.risk_per_trade_pct,
            max_position_size=self.config.max_position_size,
            min_rr_ratio=self.config.min_risk_reward,
        )

        # State tracking
        self.current_trend = TrendDirection.RANGING
        self.trend_strength = 0.0
        self.active_signals: list[SMCSignal] = []

        # Warmup tracking
        self._generate_call_count: int = 0
        self._warmup_logged: bool = False

        logger.info(
            "SMC Strategy initialized",
            config=self.config.__class__.__name__,
            balance=float(account_balance),
            warmup_bars=self.config.warmup_bars,
        )

    def analyze_market(
        self, df_d1: pd.DataFrame, df_h4: pd.DataFrame, df_h1: pd.DataFrame, df_m15: pd.DataFrame
    ) -> dict:
        """
        Perform complete multi-timeframe market analysis

        Args:
            df_d1: Daily timeframe data for global trend
            df_h4: 4-hour data for market structure
            df_h1: 1-hour data for confluence zones
            df_m15: 15-minute data for entry signals

        Returns:
            Dictionary with complete market analysis
        """
        logger.info("Starting multi-timeframe analysis")

        # 1. Analyze market structure on H4 (primary structure)
        structure_analysis = self.market_structure.analyze(df_h4)

        # 2. Multi-timeframe trend analysis
        trend_analysis = self.market_structure.analyze_trend(df_d1, df_h4)
        self.current_trend = trend_analysis.get("h4_trend", TrendDirection.RANGING)
        self.trend_strength = trend_analysis.get("trend_strength", 0.0)

        # 3. Detect confluence zones on H1
        confluence_analysis = self.confluence_analyzer.analyze(df_h1)

        # 4. Scan for entry patterns on M15
        # (signal generation happens separately)

        analysis_result = {
            "timestamp": df_m15.iloc[-1].name if len(df_m15) > 0 else pd.Timestamp.now(),
            "market_structure": structure_analysis,
            "trend_analysis": trend_analysis,
            "confluence_zones": confluence_analysis,
            "current_trend": self.current_trend,
            "trend_strength": self.trend_strength,
        }

        logger.info(
            "Market analysis complete",
            trend=self.current_trend,
            strength=self.trend_strength,
            obs=confluence_analysis.get("order_blocks", {}).get("active", 0),
            fvgs=confluence_analysis.get("fair_value_gaps", {}).get("active", 0),
        )

        return analysis_result

    def generate_signals(self, df_h1: pd.DataFrame, df_m15: pd.DataFrame) -> list[SMCSignal]:
        """
        Generate trading signals based on current market conditions

        Args:
            df_h1: 1-hour data for confluence context
            df_m15: 15-minute data for pattern detection

        Returns:
            List of high-confidence SMCSignal objects
        """
        self._generate_call_count += 1

        # Skip signal generation during warmup period
        if self._generate_call_count <= self.config.warmup_bars:
            if not self._warmup_logged:
                logger.info(
                    "smc_warmup_skip_N_bars",
                    warmup_bars=self.config.warmup_bars,
                    swing_length=self.config.swing_length,
                )
                self._warmup_logged = True
            return []

        logger.info("Generating trading signals")

        # Analyze M15 for entry patterns
        all_signals = self.signal_generator.analyze(df_m15)

        # Filter signals based on trend alignment and confluence
        filtered_signals = self._filter_signals(all_signals)

        # Validate signals against risk management
        validated_signals = []
        for signal in filtered_signals:
            # Calculate position size
            position_size = self.position_manager.calculate_position_size(signal)

            # Validate risk
            is_valid, reason = self.position_manager.validate_position_risk(signal, position_size)

            if is_valid:
                validated_signals.append(signal)
                logger.info(f"Signal validated: {signal}")
            else:
                logger.debug(f"Signal rejected: {reason}")

        self.active_signals = validated_signals

        logger.info(
            "Signal generation complete",
            total_detected=len(all_signals),
            after_filter=len(filtered_signals),
            validated=len(validated_signals),
        )

        return validated_signals

    def _filter_signals(self, signals: list[SMCSignal]) -> list[SMCSignal]:
        """
        Filter signals based on strategy rules

        Filters:
        - Minimum confidence threshold
        - Trend alignment (optional based on config)
        - Confluence requirement
        - Max signals limit
        """
        filtered = []

        for signal in signals:
            # Check minimum confidence
            if signal.confidence < 0.6:  # 60% minimum
                continue

            # Prefer trend-aligned signals (but don't reject counter-trend)
            if signal.trend_aligned:
                signal.confidence *= 1.1  # 10% confidence boost

            # Require at least some confluence for high confidence
            if signal.confidence > 0.8 and signal.confluence_score < 20:
                signal.confidence *= 0.9  # Reduce if no confluence

            filtered.append(signal)

        # Sort by confidence and take top signals
        filtered.sort(key=lambda s: s.confidence, reverse=True)

        # Limit to max 3 concurrent signals
        return filtered[:3]

    def generate_signals_m5(self, df_h1: pd.DataFrame, df_m5: pd.DataFrame) -> list[SMCSignal]:
        """
        Two-level entry: H1 structure → M5 precision entry.

        Logic:
          1. Analyse H1 for BOS/CHoCH to determine direction.
          2. Use M5 data (with swing_length_m5) to find OB/FVG entries
             within the H1 zone.
          3. Only emit signals when M5 FVG/OB overlaps with the H1 zone.

        Args:
            df_h1: 1-hour candles for structural context.
            df_m5: 5-minute candles for precision entry.

        Returns:
            List of validated SMCSignal objects (empty list if in warmup).
        """
        self._generate_call_count += 1

        if self._generate_call_count <= self.config.warmup_bars:
            return []

        if df_h1.empty or df_m5.empty:
            return []

        # 1. Determine H1 trend/direction (uses existing market_structure analyser)
        h1_structure = self.market_structure.analyze(df_h1)
        h1_trend = h1_structure.get("current_trend", self.current_trend)

        # No strong directional bias — skip
        if h1_trend == TrendDirection.RANGING:
            logger.debug("m5_entry_skipped_ranging_h1")
            return []

        # 2. Create a dedicated M5 structure analyser with noise-filtering swing_length
        m5_swing_length = self.config.swing_length_m5
        m5_analyzer = MarketStructureAnalyzer(
            swing_length=m5_swing_length,
            trend_period=self.config.trend_period,
            close_break=self.config.close_break,
        )
        m5_confluence = ConfluenceZoneAnalyzer(
            market_structure=m5_analyzer,
            timeframe="5m",
            close_mitigation=self.config.close_mitigation,
            join_consecutive_fvg=self.config.join_consecutive_fvg,
            liquidity_range_percent=self.config.liquidity_range_percent,
        )
        m5_signal_gen = EntrySignalGenerator(
            market_structure=m5_analyzer,
            confluence_analyzer=m5_confluence,
            min_risk_reward=self.config.min_risk_reward,
            sl_buffer_pct=0.5,
        )

        # 3. Populate M5 structure + confluence zones before signal generation.
        # Without this, EntrySignalGenerator sees empty OB/FVG lists and
        # all signals get zero confluence score.
        m5_analyzer.analyze(df_m5)
        m5_confluence.analyze(df_m5)

        # 4. Detect M5 entry signals
        try:
            m5_signals_raw = m5_signal_gen.analyze(df_m5)
        except Exception as e:
            logger.warning("m5_signal_gen_error", error=str(e))
            return []

        # 5. Filter: only keep signals aligned with H1 trend
        aligned: list[SMCSignal] = []
        for sig in m5_signals_raw:
            if h1_trend == TrendDirection.BULLISH and sig.direction == SMCSignalDirection.LONG:
                aligned.append(sig)
            elif h1_trend == TrendDirection.BEARISH and sig.direction == SMCSignalDirection.SHORT:
                aligned.append(sig)

        # Validate risk / position sizing
        validated: list[SMCSignal] = []
        for signal in aligned:
            position_size = self.position_manager.calculate_position_size(signal)
            is_valid, reason = self.position_manager.validate_position_risk(signal, position_size)
            if is_valid:
                validated.append(signal)
            else:
                logger.debug("m5_signal_rejected", reason=reason)

        logger.info(
            "m5_signals_generated",
            h1_trend=h1_trend,
            m5_raw=len(m5_signals_raw),
            aligned=len(aligned),
            validated=len(validated),
        )
        return validated

    def manage_positions(
        self, current_prices: dict[str, Decimal], df: Optional[pd.DataFrame] = None
    ) -> list[PositionMetrics]:
        """
        Update and manage open positions

        Args:
            current_prices: Dict of position_id -> current_price
            df: Optional dataframe for structure-based trailing

        Returns:
            List of updated PositionMetrics
        """
        updated_positions = []
        positions_to_close = []

        for position_id, current_price in current_prices.items():
            if position_id not in self.position_manager.open_positions:
                continue

            # Update position
            position = self.position_manager.update_position(position_id, current_price, df)

            # Check exit conditions
            should_exit, exit_reason = self.position_manager.check_exit_conditions(
                position_id, current_price
            )

            if should_exit:
                positions_to_close.append((position_id, current_price, exit_reason))
            else:
                updated_positions.append(position)

        # Close positions that hit SL/TP
        for position_id, exit_price, reason in positions_to_close:
            closed_position = self.position_manager.close_position(position_id, exit_price, reason)
            logger.info(
                "Position closed",
                id=position_id,
                reason=reason,
                pnl=float(closed_position.realized_pnl),
            )

        return updated_positions

    def get_strategy_state(self) -> dict:
        """
        Get complete strategy state

        Returns:
            Dictionary with all strategy metrics and status
        """
        return {
            "strategy": "Smart Money Concepts (SMC)",
            "version": "1.0.0",
            # Market analysis
            "current_trend": self.current_trend.value if self.current_trend else None,
            "trend_strength": self.trend_strength,
            # Market structure
            "swing_highs": len(self.market_structure.swing_highs),
            "swing_lows": len(self.market_structure.swing_lows),
            "structure_events": len(self.market_structure.structure_events),
            # Confluence zones
            "active_order_blocks": len(self.confluence_analyzer.get_active_order_blocks()),
            "active_fvgs": len(self.confluence_analyzer.get_active_fvgs()),
            "active_liquidity_zones": len(self.confluence_analyzer.get_active_liquidity_zones()),
            # Signals
            "active_signals": len(self.active_signals),
            "signal_summary": self.signal_generator.get_signals_summary(),
            # Positions
            "position_summary": self.position_manager.get_position_summary(),
            # Configuration
            "config": {
                "risk_per_trade_pct": self.config.risk_per_trade_pct,
                "min_risk_reward": self.config.min_risk_reward,
                "max_position_size": float(self.config.max_position_size),
            },
        }

    def get_performance_report(self) -> dict:
        """
        Get detailed performance report

        Returns:
            Dictionary with performance metrics
        """
        stats = self.position_manager.performance_stats

        return {
            "total_trades": stats.total_trades,
            "winning_trades": stats.winning_trades,
            "losing_trades": stats.losing_trades,
            "win_rate": f"{stats.win_rate * 100:.1f}%",
            "total_profit": float(stats.total_profit),
            "total_loss": float(stats.total_loss),
            "net_profit": float(stats.total_profit - stats.total_loss),
            "avg_win": float(stats.avg_win),
            "avg_loss": float(stats.avg_loss),
            "profit_factor": stats.profit_factor,
            "largest_win": float(stats.largest_win),
            "largest_loss": float(stats.largest_loss),
            "avg_hold_time_hours": stats.avg_hold_time_hours,
            "account_balance": float(self.position_manager.account_balance),
            "open_positions": len(self.position_manager.open_positions),
        }

    def reset(self):
        """Reset strategy state (for backtesting)"""
        # Log suppressed warning summary before resetting
        suppressed = {
            "structure_insufficient_data": self.market_structure._insufficient_data_count,
            "zone_insufficient_data": self.confluence_analyzer._insufficient_data_count,
            "liquidity_detection_failed": self.confluence_analyzer._liquidity_fail_count,
            "pattern_insufficient_data": self.signal_generator._insufficient_data_count,
        }
        total = sum(suppressed.values())
        if total > 0:
            logger.info("smc_warnings_suppressed", count=total, breakdown=suppressed)

        self.market_structure = MarketStructureAnalyzer(
            swing_length=self.config.swing_length,
            trend_period=self.config.trend_period,
            close_break=self.config.close_break,
        )
        self.confluence_analyzer = ConfluenceZoneAnalyzer(
            market_structure=self.market_structure,
            timeframe=self.config.working_timeframe,
            close_mitigation=self.config.close_mitigation,
            join_consecutive_fvg=self.config.join_consecutive_fvg,
            liquidity_range_percent=self.config.liquidity_range_percent,
        )
        self.signal_generator = EntrySignalGenerator(
            market_structure=self.market_structure,
            confluence_analyzer=self.confluence_analyzer,
            min_risk_reward=self.config.min_risk_reward,
            sl_buffer_pct=0.5,
        )
        self.active_signals.clear()
        self._generate_call_count = 0
        self._warmup_logged = False

        logger.info("Strategy reset")
