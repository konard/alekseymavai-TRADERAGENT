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

# Dummy pattern required by SMCSignal dataclass when creating structural-event signals
from bot.strategies.smc.entry_signals import (
    EntrySignalGenerator,
    PatternType,
    PriceActionPattern,
    SMCSignal,
)
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

    def detect_level_break(
        self, df_m5: pd.DataFrame, current_price: Decimal
    ) -> Optional[SMCSignal]:
        """
        Detect support / resistance breakout events.

        Analyses the M5 OHLCV frame against the last known SMC context held in
        ``self.market_structure``.  Returns an ``SMCSignal`` with
        ``event_type`` set to ``"support_break"`` or ``"resistance_break"``
        when a structural break is detected, or ``None`` when price is still
        inside the current range.

        Algorithm
        ---------
        1. Retrieve the cached ``SMCContext`` from the market-structure analyser
           (populated by the most recent ``generate_signals`` / ``analyze_market``
           call).  If no context is available, run a fresh analysis on *df_m5*.
        2. Determine active key levels from OBs and FVGs closest to *current_price*.
        3. **Support break**: last M5 candle *closed* below the nearest support
           (bull OB high / bull FVG gap_low below current price).
        4. **Resistance break**: last M5 candle *closed* above the nearest
           resistance (bear OB low / bear FVG gap_high above current price).
        5. Compute ``structure_confidence`` from OB quality, volume confirmation
           relative to the recent average, and last BOS/CHoCH structural event.

        Args:
            df_m5: M5 OHLCV DataFrame.  Must have columns: open, high, low,
                   close, volume (volume may be zeros).
            current_price: Latest traded price as ``Decimal``.

        Returns:
            ``SMCSignal`` with ``event_type != "entry"`` on breakout, else
            ``None``.
        """
        if df_m5.empty or len(df_m5) < 2:
            return None

        price = float(current_price)
        last_close = float(df_m5["close"].iloc[-1])

        # ------------------------------------------------------------------
        # 1. Obtain SMC context
        # ------------------------------------------------------------------
        ctx = self.market_structure._smc_context
        if ctx is None:
            # No cached context — run a fresh analysis on the M5 frame
            ctx = self.market_structure._analyzer.analyze(df_m5)
            if not ctx.warmup_complete:
                return None

        analyzer = self.market_structure._analyzer

        # ------------------------------------------------------------------
        # 2. Find nearest support below *and* nearest resistance above price.
        # For breakout detection we also need to look at levels that are
        # *just above the last close* (support that was broken downward) or
        # *just below the last close* (resistance that was broken upward).
        # We search within a ±2% window around last_close so that we catch
        # levels that were crossed by the last candle.
        # ------------------------------------------------------------------
        window = last_close * 0.02  # 2% search window

        # Nearest support below price (for next_level after a support break)
        nearest_support_below = analyzer.find_next_support(ctx, last_close)
        # Nearest resistance above price (for next_level after a resistance break)
        nearest_resistance_above = analyzer.find_next_resistance(ctx, last_close)

        # Candidate support level that could have been broken: highest support
        # within [last_close, last_close + window]
        candidate_support = self._find_support_just_above(ctx, last_close, window)
        # Candidate resistance level that could have been broken: lowest resistance
        # within [last_close - window, last_close]
        candidate_resistance = self._find_resistance_just_below(ctx, last_close, window)

        event_type: Optional[str] = None
        broken_level: Optional[float] = None
        break_extreme: Optional[float] = None
        next_level: Optional[float] = None

        # ------------------------------------------------------------------
        # 3. Support break: last close is *below* a support that existed just
        #    above it (price closed through that support)
        # ------------------------------------------------------------------
        if candidate_support is not None and last_close < candidate_support:
            event_type = "support_break"
            broken_level = candidate_support
            # break_extreme = lowest low of the last candle (for +1% trigger base)
            break_extreme = float(df_m5["low"].iloc[-1])
            # next SMC support below the broken level
            next_level = nearest_support_below

        # ------------------------------------------------------------------
        # 4. Resistance break: last close is *above* a resistance that existed
        #    just below it (price closed through that resistance)
        # ------------------------------------------------------------------
        elif candidate_resistance is not None and last_close > candidate_resistance:
            event_type = "resistance_break"
            broken_level = candidate_resistance
            # break_extreme = highest high of the last candle (for +1% trigger base)
            break_extreme = float(df_m5["high"].iloc[-1])
            # next SMC resistance above the broken level
            next_level = nearest_resistance_above

        if event_type is None:
            return None

        # ------------------------------------------------------------------
        # 5. Compute structure_confidence
        # ------------------------------------------------------------------
        structure_confidence = self._compute_structure_confidence(
            ctx=ctx, df_m5=df_m5, event_type=event_type
        )

        # ------------------------------------------------------------------
        # 6. Build a minimal SMCSignal carrying the structural event data
        # ------------------------------------------------------------------
        # Use last candle's close as entry, SL = break_extreme, TP = next_level
        entry_d = Decimal(str(last_close))
        sl_d = Decimal(str(break_extreme)) if break_extreme is not None else entry_d
        if next_level is not None:
            tp_d = Decimal(str(next_level))
        else:
            # Fallback: project 1% beyond the broken level in break direction
            if event_type == "support_break":
                tp_d = Decimal(str(broken_level)) * Decimal("0.99")
            else:
                tp_d = Decimal(str(broken_level)) * Decimal("1.01")

        direction = (
            SMCSignalDirection.SHORT if event_type == "support_break" else SMCSignalDirection.LONG
        )

        # A lightweight placeholder pattern (required by the dataclass)
        dummy_pattern = PriceActionPattern(
            pattern_type=PatternType.ENGULFING,
            is_bullish=(direction == SMCSignalDirection.LONG),
            index=len(df_m5) - 1,
            timestamp=df_m5.index[-1],
            open=Decimal(str(df_m5["open"].iloc[-1])),
            high=Decimal(str(df_m5["high"].iloc[-1])),
            low=Decimal(str(df_m5["low"].iloc[-1])),
            close=entry_d,
            quality_score=structure_confidence * 100,
            confidence=structure_confidence,
        )

        risk = abs(entry_d - sl_d)
        reward = abs(tp_d - entry_d)
        rr = float(reward / risk) if risk > 0 else 0.0

        signal = SMCSignal(
            timestamp=df_m5.index[-1],
            direction=direction,
            entry_price=entry_d,
            stop_loss=sl_d,
            take_profit=tp_d,
            pattern=dummy_pattern,
            confidence=structure_confidence,
            risk_reward_ratio=rr,
            # Structural event fields
            event_type=event_type,
            broken_level=Decimal(str(broken_level)) if broken_level is not None else None,
            break_extreme=Decimal(str(break_extreme)) if break_extreme is not None else None,
            next_level=Decimal(str(next_level)) if next_level is not None else None,
            structure_confidence=structure_confidence,
        )

        logger.info(
            "level_break_detected",
            event_type=event_type,
            broken_level=broken_level,
            break_extreme=break_extreme,
            next_level=next_level,
            structure_confidence=round(structure_confidence, 3),
        )

        return signal

    # ------------------------------------------------------------------
    # Internal: level search helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_support_just_above(ctx, price: float, window: float) -> Optional[float]:
        """
        Return the highest support level in (price, price + window].

        A support level is a bull OB high, bull FVG gap_low, or EQL/DEMAND
        liquidity level that sits just above the current price — indicating
        price has closed *through* (below) it.
        """
        candidates: list[float] = []

        for ob in ctx.order_blocks:
            if ob.ob_type.value == "bull" and not ob.invalidated:
                if price < ob.high <= price + window:
                    candidates.append(ob.high)

        for fvg in ctx.fair_value_gaps:
            if fvg.fvg_type.value == "bull" and not fvg.filled:
                if price < fvg.gap_low <= price + window:
                    candidates.append(fvg.gap_low)

        for lv in ctx.liquidity_levels:
            if lv.liquidity_type.value in ("eql", "demand") and not lv.swept:
                if price < lv.price <= price + window:
                    candidates.append(lv.price)

        return max(candidates) if candidates else None

    @staticmethod
    def _find_resistance_just_below(ctx, price: float, window: float) -> Optional[float]:
        """
        Return the lowest resistance level in [price - window, price).

        A resistance level is a bear OB low, bear FVG gap_high, or EQH/SUPPLY
        liquidity level that sits just below the current price — indicating
        price has closed *through* (above) it.
        """
        candidates: list[float] = []

        for ob in ctx.order_blocks:
            if ob.ob_type.value == "bear" and not ob.invalidated:
                if price - window <= ob.low < price:
                    candidates.append(ob.low)

        for fvg in ctx.fair_value_gaps:
            if fvg.fvg_type.value == "bear" and not fvg.filled:
                if price - window <= fvg.gap_high < price:
                    candidates.append(fvg.gap_high)

        for lv in ctx.liquidity_levels:
            if lv.liquidity_type.value in ("eqh", "supply") and not lv.swept:
                if price - window <= lv.price < price:
                    candidates.append(lv.price)

        return min(candidates) if candidates else None

    # ------------------------------------------------------------------
    # Internal: structure confidence scoring
    # ------------------------------------------------------------------

    def _compute_structure_confidence(self, ctx, df_m5: pd.DataFrame, event_type: str) -> float:
        """
        Compute structure_confidence (0.0–1.0) from three factors:

        * OB quality (0–0.4): fraction of non-invalidated OBs aligned with
          the break direction.
        * Volume confirmation (0–0.3): last candle volume vs 20-bar average.
        * CHoCH/BOS confirmation (0–0.3): last structural event aligns with
          the break direction.
        """
        score = 0.0

        # 1. OB quality
        if ctx.order_blocks:
            if event_type == "support_break":
                # Bear OBs (supply) are consistent with a downside break
                aligned = [
                    ob
                    for ob in ctx.order_blocks
                    if ob.ob_type.value == "bear" and not ob.invalidated
                ]
            else:
                aligned = [
                    ob
                    for ob in ctx.order_blocks
                    if ob.ob_type.value == "bull" and not ob.invalidated
                ]
            ob_score = min(len(aligned) / max(len(ctx.order_blocks), 1), 1.0) * 0.4
            score += ob_score

        # 2. Volume confirmation
        if "volume" in df_m5.columns and len(df_m5) >= 2:
            lookback = min(20, len(df_m5) - 1)
            avg_vol = df_m5["volume"].iloc[-lookback - 1 : -1].mean()
            last_vol = df_m5["volume"].iloc[-1]
            if avg_vol > 0:
                vol_ratio = min(float(last_vol) / float(avg_vol), 3.0)
                score += (vol_ratio / 3.0) * 0.3

        # 3. CHoCH / BOS alignment
        last_event = ctx.last_structure
        if last_event is not None:
            is_bear_event = not last_event.is_bullish
            if event_type == "support_break" and is_bear_event:
                score += 0.3
            elif event_type == "resistance_break" and last_event.is_bullish:
                score += 0.3

        return round(min(score, 1.0), 4)

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
                "risk_per_trade": self.config.risk_per_trade,
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
