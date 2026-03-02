"""
BotOrchestrator - Main coordinator for trading strategies and lifecycle management.

v2.0: Multi-strategy support with market regime detection and health monitoring.
Manages Grid, DCA, SMC, and Trend-Follower engines with dynamic strategy selection.
"""

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import pandas as pd
import redis.asyncio as redis

from bot.config.schemas import BotConfig, StrategyType
from bot.core.dca_engine import DCAEngine
from bot.core.grid_engine import GridEngine, GridType
from bot.core.risk_manager import RiskManager
from bot.database.manager import DatabaseManager
from bot.database.models import BotStateSnapshot
from bot.orchestrator import state_persistence as sp
from bot.orchestrator.events import EventType, TradingEvent
from bot.orchestrator.health_monitor import HealthCheckResult, HealthMonitor, HealthThresholds
from bot.orchestrator.market_regime import (
    MarketRegimeDetector,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.orchestrator.strategy_registry import (
    StrategyInstance,
    StrategyRegistry,
)
from bot.strategies.base import SignalDirection as BaseSignalDirection
from bot.strategies.dca.dca_signal_generator import MarketState
from bot.strategies.grid.grid_risk_manager import GridRiskManager
from bot.core.trading_core import HybridCoordinator, TradingCore, TradingCoreConfig
from bot.strategies.hybrid.hybrid_config import HybridConfig, HybridMode
from bot.strategies.hybrid.hybrid_strategy import HybridStrategy
from bot.strategies.smc.config import SMCConfig
from bot.strategies.smc_adapter import SMCStrategyAdapter
from bot.strategies.trend_follower import TrendFollowerConfig as TrendFollowerDataclassConfig
from bot.strategies.trend_follower import TrendFollowerStrategy
from bot.strategies.trend_follower.entry_logic import SignalType
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class BotState(str, Enum):
    """Bot lifecycle states."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    EMERGENCY = "emergency"


class BotOrchestrator:
    """
    Main orchestrator for coordinating trading strategies.

    v2.0 Features:
    - Multi-strategy lifecycle management via StrategyRegistry
    - Market regime detection for dynamic strategy selection
    - Health monitoring with auto-restart capabilities
    - Manages lifecycle of Grid, DCA, SMC, and Trend-Follower engines
    - Coordinates strategy execution and conflict resolution
    - Publishes events via Redis Pub/Sub
    - Handles state transitions (Running, Paused, Stopped, Emergency)
    - Integrates risk management across all strategies
    """

    def __init__(
        self,
        bot_config: BotConfig,
        exchange_client: Any,
        db_manager: DatabaseManager,
        redis_url: str = "redis://localhost:6379",
    ):
        """
        Initialize Bot Orchestrator.

        Args:
            bot_config: Bot configuration
            exchange_client: Exchange API client
            db_manager: Database manager
            redis_url: Redis connection URL
        """
        self.config = bot_config
        self.exchange = exchange_client
        self.db = db_manager
        self.redis_url = redis_url

        # State management
        self.state = BotState.STOPPED
        self._state_lock = asyncio.Lock()

        # Redis for event pub/sub
        self.redis_client: redis.Redis | None = None
        self.redis_pubsub: redis.client.PubSub | None = None

        # Trading engines
        self.grid_engine: GridEngine | None = None
        self.dca_engine: DCAEngine | None = None
        self.trend_follower_strategy: TrendFollowerStrategy | None = None
        self.smc_strategy: SMCStrategyAdapter | None = None
        self.hybrid_strategy: HybridStrategy | None = None
        self.risk_manager: RiskManager | None = None

        # Runtime state
        self._running = False
        self._main_task: asyncio.Task | None = None
        self._price_monitor_task: asyncio.Task | None = None
        self._regime_monitor_task: asyncio.Task | None = None
        self.current_price: Decimal | None = None
        self._cached_balance: Decimal | None = None
        self._last_daily_reset: object | None = None  # date object

        # State persistence
        self._state_loaded = False
        self._last_state_save: float = 0.0
        self._state_save_interval: float = 30.0  # seconds

        # SMC analysis throttle (entry timeframe is M15 → analyze every 5 min)
        self._smc_last_analysis: float = 0.0
        self._smc_analysis_interval: float = 300.0  # 5 minutes
        self._smc_stale_count: int = 0  # count consecutive stale rejections

        # v2.0: Multi-strategy components
        self.strategy_registry = StrategyRegistry(max_strategies=10)
        self.market_regime_detector = MarketRegimeDetector()
        self.health_monitor = HealthMonitor(
            registry=self.strategy_registry,
            thresholds=HealthThresholds(),
            check_interval=30.0,
        )
        self._current_regime: RegimeAnalysis | None = None
        self._regime_check_interval: float = 60.0  # seconds
        self._last_regime_update_at: float = 0.0   # monotonic ts of last successful regime update
        self._regime_stale_threshold: float = 120.0  # warn after 2× check interval
        self._active_strategies: set[str] = set()  # strategies active for current regime
        self._last_strategy_switch_at: float = 0.0  # monotonic timestamp of last switch
        self._strategy_switch_cooldown: float = float(
            getattr(self.config, "strategy_switch_cooldown_seconds", 600)
        )

        # Manual strategy lock (prevents auto-switching when locked)
        self._strategy_locked: bool = False
        self._locked_strategies: set[str] | None = None

        # Unified TradingCore kernel — shared config/coordinator with backtest engine
        self._trading_core: TradingCore = TradingCore.from_config(
            TradingCoreConfig(
                symbol=str(getattr(bot_config, "symbol", "BTC/USDT")),
                cooldown_seconds=int(
                    getattr(bot_config, "strategy_switch_cooldown_seconds", 600)
                ),
            )
        )

        # Set health callbacks
        self.health_monitor.set_unhealthy_callback(self._on_strategy_unhealthy)
        self.health_monitor.set_critical_callback(self._on_strategy_critical)

        logger.info(
            "bot_orchestrator_initialized",
            bot_name=bot_config.name,
            symbol=bot_config.symbol,
            strategy=bot_config.strategy,
            version="2.0",
        )

    async def initialize(self) -> None:
        """Initialize orchestrator and all components."""
        logger.info("initializing_orchestrator", bot_name=self.config.name)

        # Connect to Redis
        self.redis_client = redis.from_url(self.redis_url, encoding="utf-8", decode_responses=True)
        assert self.redis_client is not None
        await self.redis_client.ping()  # type: ignore[misc]
        logger.info("redis_connected")

        # Initialize risk manager
        if self.config.risk_management:
            self.risk_manager = RiskManager(
                max_position_size=Decimal(str(self.config.risk_management.max_position_size)),
                stop_loss_percentage=(
                    Decimal(str(self.config.risk_management.stop_loss_percentage))
                    if self.config.risk_management.stop_loss_percentage
                    else None
                ),
                max_daily_loss=(
                    Decimal(str(self.config.risk_management.max_daily_loss))
                    if self.config.risk_management.max_daily_loss
                    else None
                ),
                min_order_size=Decimal(str(self.config.risk_management.min_order_size)),
            )

            # Initialize with current balance
            balance = await self.exchange.fetch_balance()
            quote_currency = self.config.symbol.split("/")[1]
            # balance structure: {'free': {'USDT': 100000, ...}, 'total': {...}, 'used': {...}}
            free_balances = balance.get("free", {})
            available_balance = Decimal(str(free_balances.get(quote_currency, 0)))
            self.risk_manager.initialize_balance(available_balance)
            logger.info(
                "risk_manager_initialized",
                initial_balance=str(available_balance),
            )

        # Initialize Grid engine if enabled
        if self.config.strategy in ["grid", "hybrid"] and self.config.grid:
            self.grid_engine = GridEngine(
                symbol=self.config.symbol,
                upper_price=Decimal(str(self.config.grid.upper_price)),
                lower_price=Decimal(str(self.config.grid.lower_price)),
                grid_levels=self.config.grid.grid_levels,
                amount_per_grid=Decimal(str(self.config.grid.amount_per_grid)),
                profit_per_grid=Decimal(str(self.config.grid.profit_per_grid)),
                grid_type=GridType.STATIC,
            )
            logger.info("grid_engine_initialized")

        # Initialize DCA engine if enabled
        if self.config.strategy in ["dca", "hybrid"] and self.config.dca:
            self.dca_engine = DCAEngine(
                symbol=self.config.symbol,
                trigger_percentage=Decimal(str(self.config.dca.trigger_percentage)),
                amount_per_step=Decimal(str(self.config.dca.amount_per_step)),
                max_steps=self.config.dca.max_steps,
                take_profit_percentage=Decimal(str(self.config.dca.take_profit_percentage)),
            )
            logger.info("dca_engine_initialized")

        # Initialize HybridStrategy coordinator when both Grid and DCA are present
        if (
            self.config.strategy == "hybrid"
            and self.grid_engine is not None
            and self.dca_engine is not None
        ):
            self.hybrid_strategy = HybridStrategy(
                config=HybridConfig(),
                grid_risk_manager=GridRiskManager(),
                dca_engine=None,  # Orchestrator manages DCA directly
            )
            logger.info("hybrid_strategy_initialized")

        # Initialize Trend-Follower strategy if enabled
        if self.config.strategy == StrategyType.TREND_FOLLOWER and self.config.trend_follower:
            # Get initial balance for strategy
            balance = await self.exchange.fetch_balance()
            quote_currency = self.config.symbol.split("/")[1]
            free_balances = balance.get("free", {})
            initial_capital = Decimal(str(free_balances.get(quote_currency, 0)))

            # Convert Pydantic TrendFollowerConfig to dataclass TrendFollowerConfig
            pydantic_tf = self.config.trend_follower
            dataclass_config = TrendFollowerDataclassConfig(
                ema_fast_period=pydantic_tf.ema_fast_period,
                ema_slow_period=pydantic_tf.ema_slow_period,
                atr_period=pydantic_tf.atr_period,
                rsi_period=pydantic_tf.rsi_period,
                volume_multiplier=pydantic_tf.volume_multiplier,
                max_atr_filter_pct=pydantic_tf.atr_filter_threshold,
                tp_multipliers=(
                    pydantic_tf.tp_atr_multiplier_sideways,
                    pydantic_tf.tp_atr_multiplier_weak,
                    pydantic_tf.tp_atr_multiplier_strong,
                ),
                sl_multipliers=(
                    pydantic_tf.sl_atr_multiplier_sideways,
                    pydantic_tf.sl_atr_multiplier_trend,
                    pydantic_tf.sl_atr_multiplier_trend,
                ),
                risk_per_trade_pct=pydantic_tf.risk_per_trade_pct,
                max_position_size_usd=pydantic_tf.max_position_size_usd,
                max_daily_loss_usd=pydantic_tf.max_daily_loss_usd,
                max_positions=pydantic_tf.max_positions,
                log_all_signals=pydantic_tf.log_all_signals,
            )
            self.trend_follower_strategy = TrendFollowerStrategy(
                config=dataclass_config,
                initial_capital=initial_capital,
                log_trades=True,
            )
            logger.info(
                "trend_follower_strategy_initialized",
                initial_capital=str(initial_capital),
                ema_fast=self.config.trend_follower.ema_fast_period,
                ema_slow=self.config.trend_follower.ema_slow_period,
            )

        # Initialize SMC strategy if enabled
        if self.config.strategy == StrategyType.SMC and self.config.smc:
            # Get initial balance for strategy
            balance = await self.exchange.fetch_balance()
            quote_currency = self.config.symbol.split("/")[1]
            free_balances = balance.get("free", {})
            initial_capital = Decimal(str(free_balances.get(quote_currency, 0)))

            # Convert Pydantic SMCConfigSchema to SMCConfig dataclass
            pydantic_smc = self.config.smc
            smc_dataclass_config = SMCConfig(
                trend_timeframe=pydantic_smc.trend_timeframe,
                structure_timeframe=pydantic_smc.structure_timeframe,
                working_timeframe=pydantic_smc.working_timeframe,
                entry_timeframe=pydantic_smc.entry_timeframe,
                swing_length=pydantic_smc.swing_length,
                trend_period=pydantic_smc.trend_period,
                close_break=pydantic_smc.close_break,
                close_mitigation=pydantic_smc.close_mitigation,
                join_consecutive_fvg=pydantic_smc.join_consecutive_fvg,
                liquidity_range_percent=pydantic_smc.liquidity_range_percent,
                risk_per_trade=pydantic_smc.risk_per_trade,
                min_risk_reward=pydantic_smc.min_risk_reward,
                max_position_size=pydantic_smc.max_position_size,
                require_volume_confirmation=pydantic_smc.require_volume_confirmation,
                min_volume_multiplier=pydantic_smc.min_volume_multiplier,
                max_positions=pydantic_smc.max_positions,
                use_trailing_stop=pydantic_smc.use_trailing_stop,
                trailing_stop_activation=pydantic_smc.trailing_stop_activation,
                trailing_stop_distance=pydantic_smc.trailing_stop_distance,
            )
            self.smc_strategy = SMCStrategyAdapter(
                config=smc_dataclass_config,
                account_balance=initial_capital,
                name=self.config.name,
            )
            logger.info(
                "smc_strategy_initialized",
                initial_capital=str(initial_capital),
                swing_length=pydantic_smc.swing_length,
                max_positions=pydantic_smc.max_positions,
            )

        # Try to load persisted state
        await self.load_state()

        logger.info("orchestrator_initialized", bot_name=self.config.name)

    async def start(self) -> None:
        """Start the bot and begin trading."""
        async with self._state_lock:
            if self.state != BotState.STOPPED:
                logger.warning(
                    "bot_already_running",
                    current_state=self.state,
                )
                return

            logger.info("starting_bot", bot_name=self.config.name)
            self.state = BotState.STARTING

            try:
                # Get current price
                ticker = await self.exchange.fetch_ticker(self.config.symbol)
                self.current_price = Decimal(str(ticker["last"]))
                logger.info("current_price_fetched", price=str(self.current_price))

                if self._state_loaded:
                    # State was loaded from DB — reconcile with exchange
                    await self.reconcile_with_exchange()
                    logger.info("state_reconciled_with_exchange")
                else:
                    # Fresh start — initialize grid if enabled
                    if self.grid_engine:
                        grid_orders = self.grid_engine.initialize_grid(self.current_price)

                        # Filter out sell orders we can't back with available base balance
                        balance = await self.exchange.fetch_balance()
                        base_symbol = self.config.symbol.split("/")[0]
                        available_base = Decimal(str(balance.get("free", {}).get(base_symbol, 0)))

                        backed_orders = []
                        reserved_base = Decimal("0")
                        for order in grid_orders:
                            if order.side == "sell":
                                if reserved_base + order.amount > available_base:
                                    logger.warning(
                                        "grid_sell_skipped_insufficient_base",
                                        price=str(order.price),
                                        amount=str(order.amount),
                                        available=str(available_base - reserved_base),
                                    )
                                    continue
                                reserved_base += order.amount
                            backed_orders.append(order)

                        if len(backed_orders) < len(grid_orders):
                            logger.info(
                                "grid_sell_orders_filtered",
                                total=len(grid_orders),
                                placed=len(backed_orders),
                                skipped=len(grid_orders) - len(backed_orders),
                            )

                        logger.info(
                            "grid_initialized",
                            order_count=len(backed_orders),
                        )

                        # Place grid orders on exchange (if not dry run)
                        if not self.config.dry_run:
                            await self._place_grid_orders(backed_orders)

                        await self._publish_event(
                            EventType.GRID_INITIALIZED,
                            {
                                "order_count": len(backed_orders),
                                "current_price": str(self.current_price),
                            },
                        )

                    # Initialize DCA if enabled
                    if self.dca_engine:
                        self.dca_engine.reset()
                        logger.info("dca_engine_ready")

                # Start main loop
                self._running = True
                self.state = BotState.RUNNING
                self._main_task = asyncio.create_task(self._main_loop())
                self._price_monitor_task = asyncio.create_task(self._price_monitor())

                # v2.0: Start regime monitor and health monitor
                self._regime_monitor_task = asyncio.create_task(self._regime_monitor_loop())
                await self.health_monitor.start()

                await self._publish_event(
                    EventType.BOT_STARTED,
                    {"strategy": self.config.strategy, "version": "2.0"},
                )

                logger.info("bot_started", bot_name=self.config.name)

            except Exception as e:
                logger.error("bot_start_failed", error=str(e), exc_info=True)
                self.state = BotState.STOPPED
                await self._publish_event(
                    EventType.ERROR_OCCURRED,
                    {"error": str(e), "phase": "start"},
                )
                raise

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        async with self._state_lock:
            if self.state == BotState.STOPPED:
                logger.warning("bot_already_stopped")
                return

            logger.info("stopping_bot", bot_name=self.config.name)
            self.state = BotState.STOPPING
            self._running = False

            # Save state before stopping
            try:
                await self.save_state()
            except Exception as e:
                logger.error("save_state_on_stop_failed", error=str(e))

            # Cancel running tasks
            if self._main_task and not self._main_task.done():
                self._main_task.cancel()
                try:
                    await self._main_task
                except asyncio.CancelledError:
                    pass

            if self._price_monitor_task and not self._price_monitor_task.done():
                self._price_monitor_task.cancel()
                try:
                    await self._price_monitor_task
                except asyncio.CancelledError:
                    pass

            # v2.0: Stop regime monitor
            if self._regime_monitor_task and not self._regime_monitor_task.done():
                self._regime_monitor_task.cancel()
                try:
                    await self._regime_monitor_task
                except asyncio.CancelledError:
                    pass

            # v2.0: Stop health monitor and all strategies
            await self.health_monitor.stop()
            await self.strategy_registry.stop_all()

            # Cancel all open orders (if not dry run)
            if not self.config.dry_run:
                await self._cancel_all_orders()

            self.state = BotState.STOPPED
            await self._publish_event(EventType.BOT_STOPPED, {})

            logger.info("bot_stopped", bot_name=self.config.name)

    async def pause(self) -> None:
        """Pause the bot (stop placing new orders but keep existing ones)."""
        async with self._state_lock:
            if self.state != BotState.RUNNING:
                logger.warning("bot_not_running", current_state=self.state)
                return

            logger.info("pausing_bot", bot_name=self.config.name)
            self.state = BotState.PAUSED
            await self._publish_event(EventType.BOT_PAUSED, {})
            logger.info("bot_paused")

    async def resume(self) -> None:
        """Resume bot from paused state."""
        async with self._state_lock:
            if self.state != BotState.PAUSED:
                logger.warning("bot_not_paused", current_state=self.state)
                return

            logger.info("resuming_bot", bot_name=self.config.name)
            self.state = BotState.RUNNING

            # Resume risk manager if halted
            if self.risk_manager:
                self.risk_manager.resume()

            await self._publish_event(EventType.BOT_RESUMED, {})
            logger.info("bot_resumed")

    async def emergency_stop(self) -> None:
        """Emergency stop - immediate halt and cancel all orders."""
        async with self._state_lock:
            logger.warning("emergency_stop_triggered", bot_name=self.config.name)
            self.state = BotState.EMERGENCY
            self._running = False

            # Best-effort state save
            try:
                await self.save_state()
            except Exception as e:
                logger.error("save_state_on_emergency_failed", error=str(e))

            # Cancel all tasks immediately
            if self._main_task:
                self._main_task.cancel()
            if self._price_monitor_task:
                self._price_monitor_task.cancel()

            # Cancel all orders
            if not self.config.dry_run:
                try:
                    await self._cancel_all_orders()
                except Exception as e:
                    logger.error("emergency_cancel_failed", error=str(e))

            await self._publish_event(
                EventType.BOT_EMERGENCY_STOP,
                {"reason": "manual_emergency_stop"},
            )

            logger.warning("emergency_stop_completed")

    # --- Regime-aware strategy selection ---

    _REGIME_TO_STRATEGIES: dict[RecommendedStrategy, set[str]] = {
        RecommendedStrategy.GRID: {"grid"},
        RecommendedStrategy.DCA: {"dca"},
        RecommendedStrategy.HYBRID: {"grid", "dca"},
        RecommendedStrategy.HOLD: set(),
        RecommendedStrategy.REDUCE_EXPOSURE: set(),
    }

    async def _update_active_strategies(self) -> None:
        """Update which strategies should run based on current regime.

        Called every iteration of _main_loop.  When no regime has been
        detected yet all configured engines remain active so the bot
        behaves exactly as before the feature was introduced.

        A cooldown guard prevents rapid oscillation between strategy sets.
        When strategies are deactivated, open orders are cancelled (and
        optionally positions closed) before the new set is activated.
        """
        if self._strategy_locked and self._locked_strategies is not None:
            if self._active_strategies != self._locked_strategies:
                self._active_strategies = self._locked_strategies
            return

        # Eager first fetch: if regime was never detected, request it now so
        # that strategy routing is available from the very first main-loop tick.
        if self._last_regime_update_at == 0.0:
            await self.detect_market_regime()

        # Staleness warning: if regime data is older than 2× check interval
        elif self._current_regime is not None:
            age = time.monotonic() - self._last_regime_update_at
            if age > self._regime_stale_threshold:
                logger.warning(
                    "stale_regime_data",
                    age_seconds=int(age),
                    threshold_seconds=int(self._regime_stale_threshold),
                )

        recommendation = self.get_strategy_recommendation()
        if recommendation is None:
            # No regime data yet — keep everything active (backward-compat)
            self._active_strategies = {"grid", "dca", "trend_follower", "smc"}
            return

        strategies = self._REGIME_TO_STRATEGIES.get(recommendation, set()).copy()

        # Trend Follower is recommended for trending regimes
        if self._current_regime and self._current_regime.regime.value in (
            "bull_trend",
            "bear_trend",
        ):
            strategies.add("trend_follower")

        # SMC runs in trending regimes or volatile transitions
        if self._current_regime and self._current_regime.regime.value in (
            "bull_trend",
            "bear_trend",
            "volatile_transition",
        ):
            strategies.add("smc")

        prev = self._active_strategies
        if strategies != prev:
            if prev:
                # Cooldown guard: block switch if too soon after last one
                now = time.monotonic()
                elapsed = now - self._last_strategy_switch_at
                if self._strategy_switch_cooldown > 0 and elapsed < self._strategy_switch_cooldown:
                    logger.info(
                        "strategy_switch_cooldown_active",
                        remaining_seconds=int(self._strategy_switch_cooldown - elapsed),
                        blocked_strategies=sorted(strategies),
                        current_strategies=sorted(prev),
                    )
                    return

                deactivated = prev - strategies
                if deactivated:
                    await self._graceful_transition(deactivated, strategies)

                logger.info(
                    "active_strategies_updated",
                    regime=self._current_regime.regime.value if self._current_regime else "unknown",
                    recommendation=recommendation.value,
                    active=sorted(strategies),
                    deactivated=sorted(prev - strategies),
                )

            # Record timestamp for both first-time set and subsequent switches
            self._last_strategy_switch_at = time.monotonic()
        self._active_strategies = strategies

    def lock_strategy(self, strategies: set[str]) -> None:
        """Lock to a specific strategy set, preventing auto-switching."""
        self._strategy_locked = True
        self._locked_strategies = strategies
        self._active_strategies = strategies
        logger.info("strategy_locked", strategies=sorted(strategies))
        self._publish_event_sync(
            EventType.STRATEGY_LOCKED,
            {"strategies": sorted(strategies)},
        )

    def unlock_strategy(self) -> None:
        """Remove strategy lock, re-enable auto-switching."""
        self._strategy_locked = False
        self._locked_strategies = None
        logger.info("strategy_unlocked")
        self._publish_event_sync(
            EventType.STRATEGY_UNLOCKED,
            {},
        )

    async def _graceful_transition(
        self, deactivated: set[str], new_strategies: set[str]
    ) -> None:
        """Handle graceful cleanup when strategies are deactivated.

        1. Cancel open orders for deactivated strategies
        2. Optionally close positions (configurable via close_positions_on_switch)
        3. Wait for exchange confirmation

        Args:
            deactivated: Strategy names being turned off.
            new_strategies: Strategy names that will be active after transition.
        """
        close_positions = getattr(self.config, "close_positions_on_switch", False)

        await self._publish_event(
            EventType.STRATEGY_TRANSITION_STARTED,
            {
                "deactivated": sorted(deactivated),
                "new_strategies": sorted(new_strategies),
                "close_positions": close_positions,
            },
        )

        logger.info(
            "graceful_transition_started",
            deactivated=sorted(deactivated),
            close_positions=close_positions,
        )

        # --- 1. Cancel open orders for deactivated strategies ---
        if not self.config.dry_run:
            # Grid orders: cancel all when grid is deactivated
            if "grid" in deactivated and self.grid_engine:
                try:
                    await self.exchange.cancel_all_orders(self.config.symbol)
                    logger.info("transition_grid_orders_cancelled")
                except Exception as e:
                    logger.error("transition_grid_cancel_failed", error=str(e))

        # --- 2. Optionally close positions ---
        if close_positions and not self.config.dry_run:
            # Close DCA position if DCA is being deactivated
            if "dca" in deactivated and self.dca_engine:
                try:
                    await self._close_dca_position()
                    logger.info("transition_dca_position_closed")
                except Exception as e:
                    logger.error("transition_dca_close_failed", error=str(e))

            # Close trend follower positions if being deactivated
            if "trend_follower" in deactivated and self.trend_follower_strategy:
                try:
                    pm = self.trend_follower_strategy.position_manager
                    for pos_id in list(pm.active_positions.keys()):
                        pos = pm.active_positions[pos_id]
                        if self.current_price:
                            base_amount = float(pos.size / self.current_price)
                            side = "sell" if pos.direction.value == "long" else "buy"
                            await self.exchange.create_order(
                                symbol=self.config.symbol,
                                order_type="market",
                                side=side,
                                amount=base_amount,
                            )
                            pm.close_position(pos_id, self.current_price)
                    logger.info("transition_trend_follower_positions_closed")
                except Exception as e:
                    logger.error("transition_tf_close_failed", error=str(e))

            # Close SMC positions if being deactivated
            if "smc" in deactivated and self.smc_strategy:
                try:
                    adapter = self.smc_strategy
                    if hasattr(adapter, "active_positions"):
                        for pos in list(adapter.active_positions):
                            if self.current_price:
                                base_amount = float(
                                    Decimal(str(pos.get("size", 0))) / self.current_price
                                )
                                side = (
                                    "sell" if pos.get("direction") == "long" else "buy"
                                )
                                await self.exchange.create_order(
                                    symbol=self.config.symbol,
                                    order_type="market",
                                    side=side,
                                    amount=base_amount,
                                )
                    logger.info("transition_smc_positions_closed")
                except Exception as e:
                    logger.error("transition_smc_close_failed", error=str(e))

        await self._publish_event(
            EventType.STRATEGY_TRANSITION_COMPLETED,
            {
                "deactivated": sorted(deactivated),
                "new_strategies": sorted(new_strategies),
                "close_positions": close_positions,
            },
        )

        logger.info("graceful_transition_completed", deactivated=sorted(deactivated))

    def _is_strategy_active(self, strategy_name: str) -> bool:
        """Check whether *strategy_name* should execute this cycle."""
        return strategy_name in self._active_strategies

    async def _main_loop(self) -> None:
        """Main trading loop - processes orders and strategy logic."""
        logger.info("main_loop_started")

        while self._running:
            try:
                # Skip processing if paused
                if self.state == BotState.PAUSED:
                    await asyncio.sleep(1)
                    continue

                # Reset daily loss counter on UTC day change (#232)
                if self.risk_manager:
                    today = datetime.now(timezone.utc).date()
                    if self._last_daily_reset != today:
                        self.risk_manager.reset_daily_loss()
                        self._last_daily_reset = today
                        logger.info("daily_loss_reset", date=str(today))

                # Cache balance once per iteration (#233)
                self._cached_balance = await self._get_available_balance()

                # Update which strategies should run based on regime (#283, #292)
                await self._update_active_strategies()

                # Process Grid + DCA (hybrid coordination or independent)
                grid_active = self.grid_engine and self._is_strategy_active("grid")
                dca_active = (
                    self.dca_engine
                    and self.current_price
                    and self._is_strategy_active("dca")
                )

                if grid_active and dca_active and self.hybrid_strategy:
                    await self._process_hybrid_logic()
                else:
                    if grid_active:
                        await self._process_grid_orders()
                    if dca_active:
                        await self._process_dca_logic()

                # Process Trend-Follower logic
                if (
                    self.trend_follower_strategy
                    and self.current_price
                    and self._is_strategy_active("trend_follower")
                ):
                    await self._process_trend_follower_logic()

                # Process SMC logic
                if self.smc_strategy and self.current_price and self._is_strategy_active("smc"):
                    await self._process_smc_logic()

                # Update risk manager
                if self.risk_manager:
                    await self._update_risk_manager()

                # Periodic state save
                now = time.monotonic()
                if now - self._last_state_save >= self._state_save_interval:
                    try:
                        await self.save_state()
                        self._last_state_save = now
                    except Exception as e:
                        logger.error("periodic_state_save_failed", error=str(e))

                # Sleep between iterations
                await asyncio.sleep(1)

            except asyncio.CancelledError:
                logger.info("main_loop_cancelled")
                break
            except Exception as e:
                logger.error("main_loop_error", error=str(e), exc_info=True)
                await self._publish_event(
                    EventType.ERROR_OCCURRED,
                    {"error": str(e), "phase": "main_loop"},
                )
                await asyncio.sleep(5)  # Wait before retrying

        logger.info("main_loop_stopped")

    async def _price_monitor(self) -> None:
        """Monitor price updates and publish events."""
        logger.info("price_monitor_started")

        while self._running:
            try:
                ticker = await self.exchange.fetch_ticker(self.config.symbol)
                new_price = Decimal(str(ticker["last"]))

                if new_price != self.current_price:
                    self.current_price = new_price
                    await self._publish_event(
                        EventType.PRICE_UPDATED,
                        {"price": str(self.current_price)},
                    )

                await asyncio.sleep(5)  # Update every 5 seconds

            except asyncio.CancelledError:
                logger.info("price_monitor_cancelled")
                break
            except Exception as e:
                logger.error("price_monitor_error", error=str(e))
                await asyncio.sleep(5)

        logger.info("price_monitor_stopped")

    async def _process_hybrid_logic(self) -> None:
        """Delegate Grid/DCA execution to HybridCoordinator (unified kernel)."""
        if not self.current_price:
            return

        # Extract ADX from regime analysis if available
        adx: float | None = None
        if self._current_regime and hasattr(self._current_regime, "adx"):
            adx = self._current_regime.adx  # type: ignore[attr-defined]

        # Use TradingCore.hybrid_coordinator for the routing decision
        # (stateless, identical logic to what BacktestOrchestratorEngine uses)
        coordinator = self._trading_core.hybrid_coordinator
        decision = coordinator.evaluate(adx=adx, current_price=self.current_price)

        # Cross-check with HybridStrategy for transition tracking (legacy)
        if self.hybrid_strategy:
            market_state = MarketState(current_price=self.current_price, adx=adx)
            try:
                action = self.hybrid_strategy.evaluate(market_state, adx=adx)
                if action.transition_triggered:
                    await self._publish_event(
                        EventType.HYBRID_TRANSITION,
                        {
                            "from_mode": (
                                action.transition_event.from_mode.value
                                if action.transition_event
                                else None
                            ),
                            "to_mode": decision.mode.value,
                            "reason": decision.reason,
                        },
                    )
                    logger.info(
                        "hybrid_mode_transition",
                        mode=decision.mode.value,
                        reason=decision.reason,
                    )
            except Exception as e:
                logger.warning("hybrid_strategy_evaluate_failed", error=str(e))

        # Route execution based on coordinator decision
        if decision.run_grid and decision.run_dca:
            await self._process_grid_orders()
            await self._process_dca_logic()
        elif decision.run_grid:
            await self._process_grid_orders()
        elif decision.run_dca:
            await self._process_dca_logic()
        else:
            # No-op: neither grid nor DCA (shouldn't happen with current coordinator)
            logger.debug("hybrid_no_active_strategy", adx=adx, reason=decision.reason)

    async def _process_grid_orders(self) -> None:
        """Process grid order fills and rebalancing."""
        if not self.grid_engine:
            return

        # Check for filled orders (simulation for dry run)
        if self.config.dry_run:
            # In dry run, simulate order fills based on current price
            pass
        else:
            # Fetch actual orders from exchange
            open_orders = await self.exchange.fetch_open_orders(self.config.symbol)
            # Process filled orders
            open_order_ids = {o["id"] for o in open_orders}
            for order_id, grid_order in list(self.grid_engine.active_orders.items()):
                if order_id not in open_order_ids:
                    # Order disappeared — verify it was actually filled (#230)
                    try:
                        order_info = await self.exchange.fetch_order(order_id, self.config.symbol)
                        order_status = order_info.get("status", "")
                    except Exception:
                        logger.warning("fetch_order_failed", order_id=order_id)
                        continue

                    if order_status != "closed":
                        logger.warning(
                            "grid_order_not_filled",
                            order_id=order_id,
                            status=order_status,
                        )
                        # Remove stale tracking for cancelled/expired orders
                        if order_status in ("canceled", "cancelled", "expired", "rejected"):
                            self.grid_engine.active_orders.pop(order_id, None)
                        continue

                    filled_price = grid_order.price
                    rebalance_order = self.grid_engine.handle_order_filled(
                        order_id, filled_price, grid_order.amount
                    )

                    await self._publish_event(
                        EventType.ORDER_FILLED,
                        {
                            "order_id": order_id,
                            "price": str(filled_price),
                            "side": grid_order.side,
                        },
                    )

                    if rebalance_order and self.state == BotState.RUNNING:
                        await self._place_single_order(rebalance_order)

    async def _process_dca_logic(self) -> None:
        """Process DCA triggers and take profit logic."""
        if not self.dca_engine or not self.current_price:
            return

        # Update DCA engine with current price
        dca_actions = self.dca_engine.update_price(self.current_price)

        # Handle DCA trigger
        if dca_actions["dca_triggered"] and self.state == BotState.RUNNING:
            # Check risk limits
            if self.risk_manager:
                order_value = self.dca_engine.amount_per_step
                current_position = (
                    self.dca_engine.position.amount if self.dca_engine.position else Decimal("0")
                )
                balance = self._cached_balance or await self._get_available_balance()

                risk_check = self.risk_manager.check_trade(order_value, current_position, balance)
                if not risk_check:
                    logger.warning("dca_blocked_by_risk", reason=risk_check.reason)
                    return

            # Place order on exchange first, then advance state (#231)
            if not self.config.dry_run:
                try:
                    await self._place_dca_order()
                except Exception as e:
                    logger.error("dca_order_failed_skipping_state", error=str(e))
                    return

            # Only advance DCA state after order confirmed
            success = self.dca_engine.execute_dca_step(self.current_price)
            if success:
                await self._publish_event(
                    EventType.DCA_TRIGGERED,
                    {
                        "price": str(self.current_price),
                        "step": (
                            self.dca_engine.position.step_number if self.dca_engine.position else 0
                        ),
                        "avg_entry": (
                            str(self.dca_engine.position.average_entry_price)
                            if self.dca_engine.position
                            else "0"
                        ),
                    },
                )

        # Handle take profit
        if dca_actions["tp_triggered"] and self.state == BotState.RUNNING:
            pnl = self.dca_engine.close_position(self.current_price)
            await self._publish_event(
                EventType.TAKE_PROFIT_HIT,
                {
                    "price": str(self.current_price),
                    "pnl": str(pnl),
                },
            )

            # Close position on exchange
            if not self.config.dry_run:
                await self._close_dca_position()

    async def _update_risk_manager(self) -> None:
        """Update risk manager with current balance and position."""
        if not self.risk_manager:
            return

        # Use cached balance from current iteration
        balance = self._cached_balance or await self._get_available_balance()
        self.risk_manager.update_balance(balance)

        # Check if risk limits triggered emergency stop
        risk_status = self.risk_manager.get_risk_status()
        if risk_status["is_halted"]:
            logger.warning(
                "risk_limit_triggered",
                reason=risk_status["halt_reason"],
            )
            await self.emergency_stop()

    async def _place_grid_orders(self, orders: list) -> None:
        """Place grid orders on exchange."""
        for order in orders:
            await self._place_single_order(order)

    async def _place_single_order(self, order: Any) -> None:
        """Place a single order on exchange."""
        try:
            result = await self.exchange.create_order(
                symbol=self.config.symbol,
                order_type="limit",
                side=order.side,
                amount=float(order.amount),
                price=float(order.price),
            )
            order_id = result["id"]
            if self.grid_engine:
                self.grid_engine.register_order(order, order_id)

            await self._publish_event(
                EventType.ORDER_PLACED,
                {
                    "order_id": order_id,
                    "side": order.side,
                    "price": str(order.price),
                    "amount": str(order.amount),
                },
            )
        except Exception as e:
            logger.error("order_placement_failed", error=str(e))
            await self._publish_event(
                EventType.ORDER_FAILED,
                {"error": str(e), "order": str(order)},
            )

    async def _place_dca_order(self) -> None:
        """Place DCA buy order."""
        if not self.dca_engine or not self.current_price:
            return

        try:
            # amount_per_step is in quote currency (USD), convert to base currency
            base_amount = float(self.dca_engine.amount_per_step / self.current_price)
            result = await self.exchange.create_order(
                symbol=self.config.symbol,
                order_type="market",
                side="buy",
                amount=base_amount,
            )
            logger.info("dca_order_placed", order_id=result["id"], base_amount=base_amount)
        except Exception as e:
            logger.error("dca_order_failed", error=str(e))

    async def _close_dca_position(self) -> None:
        """Close DCA position."""
        if not self.dca_engine or not self.dca_engine.position or not self.current_price:
            return

        try:
            # total_amount tracks accumulated amount_per_step values (in USD)
            # Convert to base currency using current price
            base_amount = float(self.dca_engine.position.amount / self.current_price)
            result = await self.exchange.create_order(
                symbol=self.config.symbol,
                order_type="market",
                side="sell",
                amount=base_amount,
            )
            logger.info("dca_position_closed", order_id=result["id"], base_amount=base_amount)
        except Exception as e:
            logger.error("dca_close_failed", error=str(e))

    async def _cancel_all_orders(self) -> None:
        """Cancel all open orders."""
        try:
            await self.exchange.cancel_all_orders(self.config.symbol)
            logger.info("all_orders_cancelled")
        except Exception as e:
            logger.error("cancel_orders_failed", error=str(e))

    async def _process_trend_follower_logic(self) -> None:
        """Process Trend-Follower strategy logic."""
        if not self.trend_follower_strategy or not self.current_price:
            return

        try:
            # Fetch OHLCV data for analysis
            ohlcv = await self.exchange.fetch_ohlcv(
                symbol=self.config.symbol,
                timeframe="1h",  # TODO: Make configurable
                limit=100,
            )

            # Convert to DataFrame
            df = pd.DataFrame(
                ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

            # 1. Analyze market
            market_conditions = self.trend_follower_strategy.analyze_market(df)
            logger.debug(
                "trend_follower_market_analyzed",
                phase=market_conditions.phase.value if market_conditions else None,
            )

            # 2. Check for entry signals
            balance = self._cached_balance or await self._get_available_balance()
            entry_data = self.trend_follower_strategy.check_entry_signal(df, balance)

            if entry_data and self.state == BotState.RUNNING:
                signal, metrics, position_size = entry_data

                # Risk check
                if self.risk_manager:
                    current_position_value = sum(
                        (
                            pos.size
                            for pos in self.trend_follower_strategy.position_manager.active_positions.values()
                        ),
                        Decimal(0),
                    )
                    risk_check = self.risk_manager.check_trade(
                        position_size, current_position_value, balance
                    )
                    if not risk_check.allowed:
                        logger.warning(
                            "trend_follower_signal_blocked_by_risk", reason=risk_check.reason
                        )
                        return

                # Open position
                position_id = self.trend_follower_strategy.open_position(signal, position_size)

                # Execute order on exchange (if not dry run)
                if not self.config.dry_run:
                    await self._execute_trend_follower_entry(signal, position_size)

                await self._publish_event(
                    EventType.ORDER_PLACED,
                    {
                        "strategy": "trend_follower",
                        "position_id": position_id,
                        "signal_type": signal.signal_type.value,
                        "entry_price": str(signal.entry_price),
                        "position_size": str(position_size),
                        "tp": str(getattr(signal, "take_profit", "")),
                        "sl": str(getattr(signal, "stop_loss", "")),
                        "market_phase": (
                            market_conditions.phase.value if market_conditions else None
                        ),
                    },
                )

                logger.info(
                    "trend_follower_position_opened",
                    position_id=position_id,
                    signal_type=signal.signal_type.value,
                    entry_price=str(signal.entry_price),
                    size=str(position_size),
                )

            # 3. Update existing positions
            active_positions = list(
                self.trend_follower_strategy.position_manager.active_positions.keys()
            )
            for position_id in active_positions:
                exit_reason = self.trend_follower_strategy.update_position(
                    position_id, self.current_price, df
                )

                if exit_reason:
                    # Position was closed
                    position = self.trend_follower_strategy.position_manager.active_positions.get(
                        position_id
                    )

                    if not self.config.dry_run and position:
                        await self._execute_trend_follower_exit(position)

                    await self._publish_event(
                        EventType.ORDER_FILLED,
                        {
                            "strategy": "trend_follower",
                            "position_id": position_id,
                            "exit_reason": exit_reason,
                            "exit_price": str(self.current_price),
                        },
                    )

                    logger.info(
                        "trend_follower_position_closed",
                        position_id=position_id,
                        exit_reason=exit_reason,
                        exit_price=str(self.current_price),
                    )

        except Exception as e:
            logger.error("trend_follower_logic_error", error=str(e), exc_info=True)

    async def _execute_trend_follower_entry(self, signal: Any, position_size: Decimal) -> None:
        """Execute Trend-Follower entry order on exchange."""
        try:
            side = "buy" if signal.signal_type == SignalType.LONG else "sell"

            # Calculate amount in base currency
            amount = float(position_size / signal.entry_price)

            result = await self.exchange.create_order(
                symbol=self.config.symbol,
                order_type="market",
                side=side,
                amount=amount,
            )

            logger.info(
                "trend_follower_entry_executed",
                order_id=result["id"],
                side=side,
                amount=amount,
            )

        except Exception as e:
            logger.error("trend_follower_entry_failed", error=str(e), exc_info=True)
            raise

    async def _execute_trend_follower_exit(self, position: Any) -> None:
        """Execute Trend-Follower exit order on exchange."""
        try:
            # Determine side (opposite of entry)
            side = "sell" if position.signal_type == SignalType.LONG else "buy"

            # Calculate amount in base currency
            amount = float(position.size / position.entry_price)

            result = await self.exchange.create_order(
                symbol=self.config.symbol,
                order_type="market",
                side=side,
                amount=amount,
            )

            logger.info(
                "trend_follower_exit_executed",
                order_id=result["id"],
                side=side,
                amount=amount,
            )

        except Exception as e:
            logger.error("trend_follower_exit_failed", error=str(e), exc_info=True)
            raise

    async def _process_smc_logic(self) -> None:
        """Process SMC strategy logic: TP/SL every tick, analysis every 5 min."""
        if not self.smc_strategy or not self.current_price:
            return

        try:
            # --- Quick TP/SL check on every iteration (no OHLCV needed) ---
            exits = self.smc_strategy.update_positions(self.current_price, pd.DataFrame())

            for position_id, exit_reason in exits:
                self.smc_strategy.close_position(position_id, exit_reason, self.current_price)

                if not self.config.dry_run:
                    await self._execute_smc_exit(position_id, exit_reason)

                await self._publish_event(
                    EventType.ORDER_FILLED,
                    {
                        "strategy": "smc",
                        "position_id": position_id,
                        "exit_reason": exit_reason.value,
                        "exit_price": str(self.current_price),
                    },
                )

                logger.info(
                    "smc_position_closed",
                    position_id=position_id,
                    exit_reason=exit_reason.value,
                    exit_price=str(self.current_price),
                )

            # --- Full OHLCV analysis throttled to every _smc_analysis_interval ---
            now = time.monotonic()
            if now - self._smc_last_analysis < self._smc_analysis_interval:
                return
            self._smc_last_analysis = now

            # Fetch 4 timeframes of OHLCV data
            ohlcv_d1, ohlcv_h4, ohlcv_h1, ohlcv_m15 = await asyncio.gather(
                self.exchange.fetch_ohlcv(symbol=self.config.symbol, timeframe="1d", limit=200),
                self.exchange.fetch_ohlcv(symbol=self.config.symbol, timeframe="4h", limit=200),
                self.exchange.fetch_ohlcv(symbol=self.config.symbol, timeframe="1h", limit=200),
                self.exchange.fetch_ohlcv(symbol=self.config.symbol, timeframe="15m", limit=200),
            )

            # Convert each to DataFrame
            def _to_df(ohlcv_data: list) -> pd.DataFrame:
                df = pd.DataFrame(
                    ohlcv_data,
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                df.set_index("timestamp", inplace=True)
                return df

            df_d1 = _to_df(ohlcv_d1)
            df_h4 = _to_df(ohlcv_h4)
            df_h1 = _to_df(ohlcv_h1)
            df_m15 = _to_df(ohlcv_m15)

            # 1. Analyze market (multi-timeframe)
            analysis = self.smc_strategy.analyze_market(df_d1, df_h4, df_h1, df_m15)
            logger.info(
                "smc_market_analyzed",
                trend=analysis.trend,
                trend_strength=analysis.trend_strength,
            )

            # 2. Check for entry signals
            balance = self._cached_balance or await self._get_available_balance()
            signal = self.smc_strategy.generate_signal(df_m15, balance)

            if signal and self.state == BotState.RUNNING:
                # Check max positions
                active_positions = self.smc_strategy.get_active_positions()
                max_positions = self.config.smc.max_positions if self.config.smc else 3
                if len(active_positions) >= max_positions:
                    logger.debug(
                        "smc_max_positions_reached",
                        count=len(active_positions),
                    )
                else:
                    # Calculate position size from signal
                    position_size = min(
                        signal.entry_price * Decimal("0.1"),
                        (
                            Decimal(str(self.config.smc.max_position_size))
                            if self.config.smc
                            else Decimal("10000")
                        ),
                    )

                    # Risk check
                    if self.risk_manager:
                        current_position_value = sum(
                            (pos.size for pos in active_positions), Decimal(0)
                        )
                        risk_check = self.risk_manager.check_trade(
                            position_size, current_position_value, balance
                        )
                        if not risk_check.allowed:
                            logger.warning(
                                "smc_signal_blocked_by_risk",
                                reason=risk_check.reason,
                            )
                            signal = None

                    if signal and self.current_price:
                        # Reject stale signals: entry price too far from current price
                        price_diff_pct = (
                            abs(signal.entry_price - self.current_price) / self.current_price
                        )
                        if price_diff_pct > Decimal("0.02"):
                            self._smc_stale_count += 1
                            if self._smc_stale_count == 1:
                                logger.warning(
                                    "smc_signal_stale",
                                    entry_price=str(signal.entry_price),
                                    current_price=str(self.current_price),
                                    diff_pct=f"{float(price_diff_pct) * 100:.1f}%",
                                )
                            signal = None
                        else:
                            if self._smc_stale_count > 0:
                                logger.info(
                                    "smc_stale_cleared",
                                    rejected_count=self._smc_stale_count,
                                )
                            self._smc_stale_count = 0

                    if signal:
                        position_id = self.smc_strategy.open_position(signal, position_size)

                        if not self.config.dry_run:
                            await self._execute_smc_entry(signal, position_size)

                        await self._publish_event(
                            EventType.ORDER_PLACED,
                            {
                                "strategy": "smc",
                                "position_id": position_id,
                                "direction": signal.direction.value,
                                "entry_price": str(signal.entry_price),
                                "position_size": str(position_size),
                                "tp": str(signal.take_profit),
                                "sl": str(signal.stop_loss),
                                "confidence": signal.confidence,
                            },
                        )

        except Exception as e:
            logger.error("smc_logic_error", error=str(e), exc_info=True)

    async def _execute_smc_entry(self, signal: Any, position_size: Decimal) -> None:
        """Execute SMC entry order on exchange."""
        try:
            side = "buy" if signal.direction == BaseSignalDirection.LONG else "sell"

            # Calculate amount in base currency
            amount = float(position_size / signal.entry_price)

            result = await self.exchange.create_order(
                symbol=self.config.symbol,
                order_type="market",
                side=side,
                amount=amount,
            )

            logger.info(
                "smc_entry_executed",
                order_id=result["id"],
                side=side,
                amount=amount,
            )

        except Exception as e:
            logger.error("smc_entry_failed", error=str(e), exc_info=True)
            raise

    async def _execute_smc_exit(self, position_id: str, exit_reason: Any) -> None:
        """Execute SMC exit order on exchange."""
        try:
            # We need to determine the side from the closed trade records
            # The position was already closed in the adapter, check closed trades
            closed_trade = None
            for trade in reversed(self.smc_strategy._closed_trades if self.smc_strategy else []):
                if trade["position_id"] == position_id:
                    closed_trade = trade
                    break

            if not closed_trade:
                logger.warning("smc_exit_no_trade_found", position_id=position_id)
                return

            # Opposite side for exit
            side = "sell" if closed_trade["direction"] == "long" else "buy"
            amount = float(closed_trade["size"] / closed_trade["entry_price"])

            result = await self.exchange.create_order(
                symbol=self.config.symbol,
                order_type="market",
                side=side,
                amount=amount,
            )

            logger.info(
                "smc_exit_executed",
                order_id=result["id"],
                side=side,
                amount=amount,
                exit_reason=exit_reason.value,
            )

        except Exception as e:
            logger.error("smc_exit_failed", error=str(e), exc_info=True)
            raise

    async def _get_available_balance(self) -> Decimal:
        """Get available balance in quote currency."""
        balance = await self.exchange.fetch_balance()
        quote_currency = self.config.symbol.split("/")[1]
        # balance structure: {'free': {...}, 'total': {...}, 'used': {...}}
        free_balances = balance.get("free", {})
        return Decimal(str(free_balances.get(quote_currency, 0)))

    async def _publish_event(self, event_type: EventType, data: dict[str, Any]) -> None:
        """
        Publish event to Redis Pub/Sub.

        Args:
            event_type: Type of event
            data: Event data
        """
        if not self.redis_client:
            return

        event = TradingEvent.create(
            event_type=event_type,
            bot_name=self.config.name,
            data=data,
        )

        try:
            channel = f"trading_events:{self.config.name}"
            await self.redis_client.publish(channel, event.to_json())
            logger.debug("event_published", event_type=event_type.value)
        except Exception as e:
            logger.error("event_publish_failed", error=str(e))

    # =========================================================================
    # State Persistence
    # =========================================================================

    async def save_state(self) -> None:
        """Serialize all engine state and upsert into DB."""
        hybrid = getattr(self, "hybrid_strategy", None)
        snapshot = BotStateSnapshot(
            bot_name=self.config.name,
            bot_state=self.state.value,
            grid_state=sp.serialize_grid_state(self.grid_engine),
            dca_state=sp.serialize_dca_state(self.dca_engine),
            risk_state=sp.serialize_risk_state(self.risk_manager),
            trend_state=sp.serialize_trend_state(self.trend_follower_strategy),
            hybrid_state=sp.serialize_hybrid_state(hybrid),
            saved_at=datetime.now(timezone.utc),
        )
        await self.db.save_state_snapshot(snapshot)
        logger.debug("state_saved", bot_name=self.config.name)

    async def load_state(self) -> None:
        """Load persisted state from DB into engines."""
        snapshot = await self.db.load_state_snapshot(self.config.name)
        if snapshot is None:
            logger.info("no_persisted_state_found", bot_name=self.config.name)
            return

        restored_any = False
        if sp.deserialize_grid_state(self.grid_engine, snapshot.grid_state):
            restored_any = True
        if sp.deserialize_dca_state(self.dca_engine, snapshot.dca_state):
            restored_any = True
        if sp.deserialize_risk_state(self.risk_manager, snapshot.risk_state):
            restored_any = True
        if sp.deserialize_trend_state(self.trend_follower_strategy, snapshot.trend_state):
            restored_any = True

        hybrid = getattr(self, "hybrid_strategy", None)
        hybrid_json = getattr(snapshot, "hybrid_state", None)
        if sp.deserialize_hybrid_state(hybrid, hybrid_json):
            restored_any = True

        if restored_any:
            self._state_loaded = True
            logger.info(
                "state_loaded_from_db",
                bot_name=self.config.name,
                saved_at=str(snapshot.saved_at),
            )

    async def reset_state(self) -> None:
        """Delete persisted state so next start is a fresh start."""
        deleted = await self.db.delete_state_snapshot(self.config.name)
        self._state_loaded = False
        logger.info("state_reset", bot_name=self.config.name, deleted=deleted)

    async def reconcile_with_exchange(self) -> None:
        """Reconcile loaded state with live exchange data."""
        logger.info("reconcile_start", bot_name=self.config.name)

        # Grid: check which saved orders are still open on exchange
        if self.grid_engine and not self.grid_engine.active_orders and self.current_price:
            # Grid state was loaded but has no orders — re-initialize
            logger.info(
                "grid_reinit_empty_state",
                reason="loaded_state_has_no_orders",
                current_price=str(self.current_price),
            )
            try:
                grid_orders = self.grid_engine.initialize_grid(self.current_price)
                if grid_orders and not self.config.dry_run:
                    await self._place_grid_orders(grid_orders)
                logger.info(
                    "grid_reinitialized", order_count=len(grid_orders) if grid_orders else 0
                )
            except Exception as e:
                logger.error("grid_reinit_failed", error=str(e))
        elif self.grid_engine and self.grid_engine.active_orders:
            try:
                exchange_orders = await self.exchange.fetch_open_orders(self.config.symbol)
                exchange_ids = {o["id"] for o in exchange_orders}

                orphaned = []
                for order_id in list(self.grid_engine.active_orders.keys()):
                    if order_id not in exchange_ids:
                        # Order no longer open — check if filled
                        try:
                            info = await self.exchange.fetch_order(order_id, self.config.symbol)
                            status = info.get("status", "")
                        except Exception:
                            status = "unknown"

                        if status == "closed":
                            # Was filled while offline — handle it
                            grid_order = self.grid_engine.active_orders[order_id]
                            self.grid_engine.handle_order_filled(
                                order_id, grid_order.price, grid_order.amount
                            )
                            logger.info("reconcile_order_filled", order_id=order_id)
                        else:
                            orphaned.append(order_id)

                for oid in orphaned:
                    self.grid_engine.active_orders.pop(oid, None)
                    logger.info("reconcile_removed_orphan", order_id=oid)

                logger.info(
                    "grid_reconcile_done",
                    kept=len(self.grid_engine.active_orders),
                    orphaned=len(orphaned),
                )

                # Re-initialize grid if all orders were lost
                if not self.grid_engine.active_orders and self.current_price:
                    logger.info(
                        "grid_reinit_after_reconcile",
                        reason="all_orders_orphaned",
                        current_price=str(self.current_price),
                    )
                    grid_orders = self.grid_engine.initialize_grid(self.current_price)
                    if grid_orders and not self.config.dry_run:
                        await self._place_grid_orders(grid_orders)
                    logger.info(
                        "grid_reinitialized",
                        order_count=len(grid_orders) if grid_orders else 0,
                    )
            except Exception as e:
                logger.error("grid_reconcile_failed", error=str(e))

        # Risk: refresh balance from exchange (source of truth)
        if self.risk_manager:
            try:
                balance = await self._get_available_balance()
                self.risk_manager.update_balance(balance)
                logger.info("risk_balance_reconciled", balance=str(balance))
            except Exception as e:
                logger.error("risk_reconcile_failed", error=str(e))

    async def get_status(self) -> dict[str, Any]:
        """
        Get current bot status.

        Returns:
            Status dictionary with v2.0 components
        """
        status: dict[str, Any] = {
            "bot_name": self.config.name,
            "symbol": self.config.symbol,
            "strategy": self.config.strategy,
            "state": self.state.value,
            "current_price": str(self.current_price) if self.current_price else None,
            "dry_run": self.config.dry_run,
            "version": "2.0",
        }

        # Add grid status
        if self.grid_engine:
            status["grid"] = self.grid_engine.get_grid_status()

        # Add DCA status
        if self.dca_engine:
            status["dca"] = self.dca_engine.get_position_status()

        # Add Trend-Follower status
        if self.trend_follower_strategy:
            active_positions_count = len(
                self.trend_follower_strategy.position_manager.active_positions
            )
            statistics = {}
            if self.trend_follower_strategy.trade_logger:
                statistics = self.trend_follower_strategy.get_statistics()

            status["trend_follower"] = {
                "active_positions": active_positions_count,
                "statistics": statistics,
                "market_conditions": (
                    {
                        "phase": self.trend_follower_strategy.current_market_conditions.phase.value,
                        "trend_strength": self.trend_follower_strategy.current_market_conditions.trend_strength.value,
                        "rsi": str(self.trend_follower_strategy.current_market_conditions.rsi),
                    }
                    if self.trend_follower_strategy.current_market_conditions
                    else None
                ),
            }

        # Add SMC status
        if self.smc_strategy:
            status["smc"] = self.smc_strategy.get_status()

        # Add risk status
        if self.risk_manager:
            status["risk"] = self.risk_manager.get_risk_status()

        # v2.0: Strategy registry status
        status["strategy_registry"] = self.strategy_registry.get_registry_status()

        # v2.0: Market regime
        if self._current_regime:
            status["market_regime"] = self._current_regime.to_dict()

        # v2.0: Health monitor
        status["health"] = self.health_monitor.get_health_summary()

        # Strategy lock state
        status["strategy_lock"] = {
            "locked": self._strategy_locked,
            "strategies": sorted(self._locked_strategies) if self._locked_strategies else None,
        }
        status["active_strategies"] = sorted(self._active_strategies)

        return status

    # =========================================================================
    # v2.0: Multi-Strategy Management
    # =========================================================================

    def register_strategy(
        self,
        strategy_id: str,
        strategy_type: str,
        config: dict[str, Any] | None = None,
    ) -> StrategyInstance:
        """
        Register a new strategy with the orchestrator.

        Args:
            strategy_id: Unique identifier for the strategy.
            strategy_type: Type of strategy ('grid', 'dca', 'smc', 'trend_follower').
            config: Strategy-specific configuration.

        Returns:
            The registered StrategyInstance.
        """
        instance = self.strategy_registry.register(
            strategy_id=strategy_id,
            strategy_type=strategy_type,
            config=config,
        )

        self._publish_event_sync(
            EventType.STRATEGY_REGISTERED,
            {
                "strategy_id": strategy_id,
                "strategy_type": strategy_type,
            },
        )

        return instance

    async def start_strategy(self, strategy_id: str) -> bool:
        """Start a registered strategy."""
        result = await self.strategy_registry.start_strategy(strategy_id)

        if result:
            await self._publish_event(
                EventType.STRATEGY_STARTED,
                {"strategy_id": strategy_id},
            )

        return result

    async def stop_strategy(self, strategy_id: str) -> bool:
        """Stop a running strategy."""
        result = await self.strategy_registry.stop_strategy(strategy_id)

        if result:
            await self._publish_event(
                EventType.STRATEGY_STOPPED,
                {"strategy_id": strategy_id},
            )

        return result

    async def pause_strategy(self, strategy_id: str) -> bool:
        """Pause a running strategy."""
        result = await self.strategy_registry.pause_strategy(strategy_id)

        if result:
            await self._publish_event(
                EventType.STRATEGY_PAUSED,
                {"strategy_id": strategy_id},
            )

        return result

    async def resume_strategy(self, strategy_id: str) -> bool:
        """Resume a paused strategy."""
        result = await self.strategy_registry.resume_strategy(strategy_id)

        if result:
            await self._publish_event(
                EventType.STRATEGY_RESUMED,
                {"strategy_id": strategy_id},
            )

        return result

    def get_active_strategies(self) -> list[StrategyInstance]:
        """Get all currently active strategies."""
        return self.strategy_registry.get_active()

    def get_strategy_status(self, strategy_id: str) -> dict[str, Any] | None:
        """Get status of a specific strategy."""
        instance = self.strategy_registry.get(strategy_id)
        if instance:
            return instance.get_status()
        return None

    # =========================================================================
    # v2.0: Market Regime Detection
    # =========================================================================

    async def detect_market_regime(self) -> RegimeAnalysis | None:
        """
        Fetch market data and detect current regime.

        Returns:
            RegimeAnalysis or None on failure.
        """
        try:
            ohlcv = await self.exchange.fetch_ohlcv(
                symbol=self.config.symbol,
                timeframe="1h",
                limit=100,
            )

            df = pd.DataFrame(
                ohlcv,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

            analysis = self.market_regime_detector.analyze(df)

            # Check for regime change
            old_regime = self._current_regime
            self._current_regime = analysis
            self._last_regime_update_at = time.monotonic()

            if old_regime and old_regime.regime != analysis.regime:
                await self._publish_event(
                    EventType.REGIME_CHANGED,
                    {
                        "old_regime": old_regime.regime.value,
                        "new_regime": analysis.regime.value,
                        "confidence": analysis.confidence,
                        "recommended_strategy": analysis.recommended_strategy.value,
                    },
                )
                logger.info(
                    "market_regime_changed",
                    old=old_regime.regime.value,
                    new=analysis.regime.value,
                    recommended=analysis.recommended_strategy.value,
                )
            else:
                await self._publish_event(
                    EventType.REGIME_DETECTED,
                    analysis.to_dict(),
                )

            return analysis

        except Exception as e:
            logger.error("regime_detection_failed", error=str(e), exc_info=True)
            return None

    def get_strategy_recommendation(self) -> RecommendedStrategy | None:
        """Get current strategy recommendation based on market regime."""
        if self._current_regime:
            return self._current_regime.recommended_strategy
        return None

    async def _regime_monitor_loop(self) -> None:
        """Periodic market regime detection loop."""
        logger.info("regime_monitor_started")

        while self._running:
            try:
                await self.detect_market_regime()
                await asyncio.sleep(self._regime_check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("regime_monitor_error", error=str(e))
                await asyncio.sleep(self._regime_check_interval)

        logger.info("regime_monitor_stopped")

    # =========================================================================
    # v2.0: Health Monitoring Callbacks
    # =========================================================================

    async def _on_strategy_unhealthy(self, strategy_id: str, result: HealthCheckResult) -> None:
        """Handle unhealthy strategy event."""
        logger.warning(
            "strategy_unhealthy",
            strategy_id=strategy_id,
            message=result.message,
        )
        await self._publish_event(
            EventType.HEALTH_DEGRADED,
            {
                "strategy_id": strategy_id,
                "status": result.status.value,
                "message": result.message,
            },
        )

    async def _on_strategy_critical(self, strategy_id: str, result: HealthCheckResult) -> None:
        """Handle critical strategy health event."""
        logger.error(
            "strategy_critical",
            strategy_id=strategy_id,
            message=result.message,
        )
        await self._publish_event(
            EventType.HEALTH_CRITICAL,
            {
                "strategy_id": strategy_id,
                "status": result.status.value,
                "message": result.message,
            },
        )

    def _publish_event_sync(self, event_type: EventType, data: dict[str, Any]) -> None:
        """Fire-and-forget event publishing (for sync contexts)."""
        if not self.redis_client:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._publish_event(event_type, data))
        except RuntimeError:
            pass

    async def cleanup(self) -> None:
        """Cleanup resources including v2.0 components."""
        logger.info("cleaning_up_orchestrator")

        if self.state != BotState.STOPPED:
            await self.stop()

        # v2.0: Stop health monitor
        await self.health_monitor.stop()

        # v2.0: Stop all strategies
        await self.strategy_registry.stop_all()

        # v2.0: Cancel regime monitor
        if self._regime_monitor_task and not self._regime_monitor_task.done():
            self._regime_monitor_task.cancel()
            try:
                await self._regime_monitor_task
            except asyncio.CancelledError:
                pass

        # Close exchange client connection
        if self.exchange:
            try:
                await self.exchange.close()
                logger.info("exchange_client_closed")
            except Exception as e:
                logger.error("exchange_close_failed", error=str(e))

        if self.redis_client:
            await self.redis_client.aclose()

        logger.info("orchestrator_cleaned_up")
