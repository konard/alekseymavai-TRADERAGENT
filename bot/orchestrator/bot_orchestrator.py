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
from bot.core.capital_arbiter import CapitalArbiter
from bot.core.dca_engine import DCAEngine
from bot.core.grid_engine import GridDirection, GridEngine, GridType
from bot.core.portfolio_risk_manager import PortfolioRiskManager
from bot.core.price_zone_allocator import PriceZoneAllocator
from bot.core.risk_manager import RiskManager
from bot.core.smc.structure_analyzer import SMCStructureAnalyzer
from bot.core.trading_core import TradingCore, TradingCoreConfig
from bot.core.virtual_position_manager import VirtualPositionManager
from bot.data.candle_ws_feed import CandleWSFeed
from bot.data.history_manager import HistoryManager
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
from bot.orchestrator.strategy_conductor import StrategyConductor
from bot.orchestrator.strategy_registry import (
    StrategyInstance,
    StrategyRegistry,
)
from bot.orchestrator.strategy_selector import StrategySelector
from bot.strategies.base import SignalDirection as BaseSignalDirection
from bot.strategies.dca.dca_signal_generator import MarketState
from bot.strategies.dca.startup_analyzer import DCAStartupAnalyzer
from bot.strategies.grid.grid_risk_manager import GridRiskManager
from bot.strategies.hybrid.hybrid_config import HybridConfig
from bot.strategies.hybrid.recovery_config import RecoveryConfig
from bot.strategies.hybrid.recovery_coordinator import (
    RecoveryCoordinator,
    UnderwaterPosition,
)
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
        portfolio_risk_manager: PortfolioRiskManager | None = None,
        history_manager: HistoryManager | None = None,
    ):
        """
        Initialize Bot Orchestrator.

        Args:
            bot_config: Bot configuration
            exchange_client: Exchange API client
            db_manager: Database manager
            redis_url: Redis connection URL
            portfolio_risk_manager: Optional cross-pair portfolio risk manager
            history_manager: Optional HistoryManager for persistent OHLCV cache
        """
        self.config = bot_config
        self.exchange = exchange_client
        self.db = db_manager
        self.redis_url = redis_url

        # HistoryManager for persistent OHLCV (TimescaleDB); optional
        self.history_manager: HistoryManager | None = history_manager
        self._candle_ws_task: asyncio.Task | None = None

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
        self._recovery_coordinator: RecoveryCoordinator | None = None
        self._recovery_in_progress: bool = False
        self.risk_manager: RiskManager | None = None
        # Cross-pair portfolio risk (None for single-bot deployments → no overhead)
        self._portfolio_rm: PortfolioRiskManager | None = portfolio_risk_manager

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

        # Grid direction (bidirectional support)
        self._grid_direction: GridDirection = GridDirection.LONG
        self._hedge_mode_enabled: bool = False  # set True after set_position_mode succeeds

        # SMC analysis throttle (entry timeframe is M15 → analyze every 5 min)
        self._smc_last_analysis: float = 0.0
        self._smc_analysis_interval: float = 300.0  # 5 minutes
        self._smc_stale_count: int = 0  # count consecutive stale rejections

        # Unified TradingCore kernel — shared config/coordinator with backtest engine.
        # Created here (before derived attributes) so _regime_check_interval and
        # _strategy_switch_cooldown can read from it instead of duplicating defaults.
        self._trading_core: TradingCore = TradingCore.from_config(
            TradingCoreConfig(
                symbol=str(getattr(bot_config, "symbol", "BTC/USDT")),
                cooldown_seconds=int(getattr(bot_config, "strategy_switch_cooldown_seconds", 600)),
                regime_check_interval_seconds=int(
                    getattr(bot_config, "regime_check_interval_seconds", 60)
                ),
            )
        )

        # v2.0: Multi-strategy components
        self.strategy_registry = StrategyRegistry(max_strategies=10)
        self.market_regime_detector = MarketRegimeDetector()
        # SMC structure analyzer: caches SMCContext per symbol with 5-minute TTL.
        # Provides SMC phase/structural levels to MarketRegimeDetector on every
        # regime check, independently of whether the SMC trading strategy is active.
        self.smc_structure_analyzer = SMCStructureAnalyzer()
        self.health_monitor = HealthMonitor(
            registry=self.strategy_registry,
            thresholds=HealthThresholds(),
            check_interval=30.0,
        )
        self._current_regime: RegimeAnalysis | None = None
        # Read interval from TradingCore so bot and backtest share the same value.
        self._regime_check_interval: float = float(
            self._trading_core.config.regime_check_interval_seconds
        )
        self._last_regime_update_at: float = 0.0  # monotonic ts of last successful regime update
        self._regime_stale_threshold: float = 2.0 * self._regime_check_interval
        self._active_strategies: set[str] = set()  # strategies active for current regime
        self._last_strategy_switch_at: float = 0.0  # monotonic timestamp of last switch
        self._last_active_strategies_update_at: float = 0.0  # throttle _update_active_strategies
        # Cooldown also sourced from TradingCore (single source of truth with backtest).
        self._strategy_switch_cooldown: float = float(self._trading_core.config.cooldown_seconds)

        # StrategySelector: single source of truth for regime→strategy mapping.
        # Replaces the inline _REGIME_TO_STRATEGIES hardcode.
        self.strategy_selector = StrategySelector(
            registry=self.strategy_registry,
            transition_cooldown_seconds=self._strategy_switch_cooldown,
            min_regime_duration_seconds=float(self._MIN_REGIME_DURATION_SECONDS),
        )

        # StrategyConductor: distributes a shared SMC context (key levels,
        # liquidity zones, working range) to every active strategy so they
        # operate with a unified market picture instead of independently
        # recomputing their own levels.
        self.strategy_conductor = StrategyConductor(registry=self.strategy_registry)

        # Phase 4 — Capital Arbiter & Price Zone Allocator (issue #381).
        # VirtualPositionManager: single source of truth for all open positions.
        # CapitalArbiter: enforces per-regime capital allocation limits.
        # PriceZoneAllocator: vertical price zones separating each strategy.
        self.virtual_position_manager = VirtualPositionManager()
        self.capital_arbiter = CapitalArbiter(self.virtual_position_manager)
        self.price_zone_allocator = PriceZoneAllocator()
        # Track ATR bar count for zone recalculation (every 14 bars)
        self._atr_bar_count: int = 0

        # Manual strategy lock (prevents auto-switching when locked)
        self._strategy_locked: bool = False
        self._locked_strategies: set[str] | None = None

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
                max_position_size_per_trade=(
                    Decimal(str(self.config.risk_management.max_position_size_per_trade))
                    if self.config.risk_management.max_position_size_per_trade
                    else None
                ),
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

        # Initialize Recovery Coordinator for hybrid/grid strategy
        if (
            self.grid_engine is not None
            and self.config.recovery is not None
            and self.config.recovery.enabled
        ):
            _rcfg = RecoveryConfig(
                enabled=True,
                tp_target_pct=Decimal(str(self.config.recovery.tp_target_pct)),
                max_dca_orders=self.config.recovery.max_dca_orders,
                dca_step_pct=Decimal(str(self.config.recovery.dca_step_pct)),
                dca_volume_multiplier=Decimal(str(self.config.recovery.dca_volume_multiplier)),
                timeout_bars=self.config.recovery.timeout_bars,
                timeout_action=self.config.recovery.timeout_action,
                fallback_support_pct=Decimal(str(self.config.recovery.fallback_support_pct)),
                max_recovery_capital_pct=Decimal(str(self.config.recovery.max_recovery_capital_pct)),
                cooldown_after_recovery_bars=self.config.recovery.cooldown_after_recovery_bars,
            )
            _rcfg.validate()
            self._recovery_coordinator = RecoveryCoordinator(_rcfg)
            if self.hybrid_strategy is not None:
                self.hybrid_strategy._recovery_coordinator = self._recovery_coordinator
            logger.info("recovery_coordinator_initialized")

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
                swing_length_m5=pydantic_smc.swing_length_m5,
                swing_length_h1=pydantic_smc.swing_length_h1,
                m5_limit=pydantic_smc.m5_limit,
                h1_limit=pydantic_smc.h1_limit,
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

        # Register strategy position providers with PortfolioRiskManager for
        # per-symbol global stop-loss aggregation.
        if self._portfolio_rm is not None:
            symbol = str(self.config.symbol)
            for strategy_name, adapter in [
                (f"{self.config.name}:dca", self.dca_engine),
                (f"{self.config.name}:grid", self.grid_engine),
                (f"{self.config.name}:trend_follower", self.trend_follower_strategy),
                (f"{self.config.name}:smc", self.smc_strategy),
            ]:
                if adapter is not None and hasattr(adapter, "get_open_positions"):
                    self._portfolio_rm.register_symbol_provider(
                        symbol=symbol,
                        strategy_name=strategy_name,
                        provider=adapter,  # type: ignore[arg-type]
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

                # Enable hedge mode on Bybit so LONG and SHORT can coexist
                if self.grid_engine:
                    await self._ensure_hedge_mode()

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

                # DCA catch-up: place missing levels below current price
                if self.dca_engine and self.config.dca and self.config.dca.catch_up_enabled:
                    await self._run_dca_catchup()

                # Start HistoryManager backfill + WebSocket feed for SMC / TrendFollower
                if self.history_manager and self.config.strategy in (
                    StrategyType.SMC,
                    StrategyType.TREND_FOLLOWER,
                ):
                    await self._start_history_feed()

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

            # Stop WebSocket candle feed if running
            if self._candle_ws_task and not self._candle_ws_task.done():
                self._candle_ws_task.cancel()
                try:
                    await self._candle_ws_task
                except asyncio.CancelledError:
                    pass

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

    # Minimum confidence score [0.0–1.0] required before a strategy switch is
    # allowed.  Below this threshold the regime classification is too uncertain
    # to warrant disrupting running strategies.
    _MIN_REGIME_CONFIDENCE: float = 0.3

    # Minimum age of the current regime (seconds) before a switch is allowed.
    # A freshly-detected regime may still be noisy; waiting 120 s lets it
    # stabilise before we act on it.
    _MIN_REGIME_DURATION_SECONDS: int = 120

    # Maximum ATR-as-%-of-price above which adding new strategies is blocked.
    # Prevents opening fresh positions during extreme volatility spikes.
    # Reduction (deactivating strategies) is always allowed.
    _MAX_VOLATILITY_ATR_PCT: float = 3.0

    async def _update_active_strategies(self) -> None:
        """Update which strategies should run based on current regime.

        Thin wrapper around StrategySelector — the single source of truth for
        regime→strategy routing.  All mapping logic lives in
        strategy_selector.py (DEFAULT_REGIME_STRATEGIES).

        Called every iteration of _main_loop.  When no regime has been
        detected yet all configured engines remain active so the bot
        behaves exactly as before the feature was introduced.
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

        analysis = self._current_regime
        if analysis is None:
            # No regime data yet — keep everything active (backward-compat)
            self._active_strategies = {"grid", "dca", "trend_follower", "smc"}
            return

        # Volatility guard: don't activate new strategies during extreme spikes.
        # Only block expansion; reductions always pass through to StrategySelector.
        if analysis.atr_pct > self._MAX_VOLATILITY_ATR_PCT:
            result = self.strategy_selector.select(analysis)
            target = {w.strategy_type for w in result.strategies_to_start} | set(
                result.strategies_to_keep
            )
            if target > self._active_strategies:
                logger.info(
                    "strategy_switch_blocked_high_volatility",
                    atr_pct=round(analysis.atr_pct, 3),
                    threshold=self._MAX_VOLATILITY_ATR_PCT,
                    current_strategies=sorted(self._active_strategies),
                    blocked_strategies=sorted(target - self._active_strategies),
                )
                return

        result = self.strategy_selector.select(analysis)

        if result.transition_needed:
            # Compute the intended active set from the selection result.
            # This correctly handles HYBRID and REDUCE_EXPOSURE modes where
            # _get_current_weights() alone would give the wrong answer.
            intended = {w.strategy_type for w in result.strategies_to_start} | set(
                result.strategies_to_keep
            )
            deactivated = set(result.strategies_to_stop)
            if deactivated:
                await self._graceful_transition(deactivated, intended)

            await self.strategy_selector.execute_transition(result)

            logger.info(
                "active_strategies_updated",
                regime=analysis.regime.value,
                recommendation=analysis.recommended_strategy.value,
                active=sorted(intended),
                deactivated=sorted(deactivated),
            )
            self._last_strategy_switch_at = time.monotonic()
            self._active_strategies = intended

        # Switch grid direction based on regime (bear/distribution → SHORT, else → LONG).
        # Only affects the grid engine; other strategies handle directionality internally.
        # Guard with getattr for backward-compatibility with test stubs.
        grid_engine = getattr(self, "grid_engine", None)
        if grid_engine:
            from bot.orchestrator.market_regime import MarketRegime
            bear_regimes = {MarketRegime.BEAR_TREND, MarketRegime.DISTRIBUTION}
            target_direction = (
                GridDirection.SHORT if analysis.regime in bear_regimes else GridDirection.LONG
            )
            current_direction = getattr(self, "_grid_direction", GridDirection.LONG)
            if target_direction != current_direction:
                await self._switch_grid_direction(target_direction)

        # Dispatch shared SMC context (key levels, liquidity zones, price range)
        # to every active strategy via StrategyConductor regardless of whether a
        # transition occurred — regime data may have refreshed with new levels.
        # Guard with getattr for backward-compatibility with test stubs that
        # create BotOrchestrator via object.__new__() without all attributes.
        conductor = getattr(self, "strategy_conductor", None)
        if conductor is not None:
            conductor.on_regime_change(analysis)
        # If no transition needed, _active_strategies stays unchanged.

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

    async def _graceful_transition(self, deactivated: set[str], new_strategies: set[str]) -> None:
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
                                params={"reduceOnly": True},
                            )
                            pm.close_position(pos_id, self.current_price)
                    logger.info("transition_trend_follower_positions_closed")
                except Exception as e:
                    logger.error("transition_tf_close_failed", error=str(e))

            # Close SMC positions if being deactivated
            if "smc" in deactivated and self.smc_strategy:
                try:
                    adapter = self.smc_strategy
                    for _pos_id, pos in list(adapter._positions.items()):
                        if self.current_price:
                            base_amount = float(
                                Decimal(str(pos.get("size", 0))) / self.current_price
                            )
                            side = "sell" if pos.get("direction") == "long" else "buy"
                            await self.exchange.create_order(
                                symbol=self.config.symbol,
                                order_type="market",
                                side=side,
                                amount=base_amount,
                                params={"reduceOnly": True},
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

                # Update which strategies should run based on regime (#283, #292).
                # Throttled to _regime_check_interval so we don't re-evaluate every tick —
                # regime data itself only refreshes that often.  The first iteration always
                # runs because _last_active_strategies_update_at starts at 0.0.
                _now = time.monotonic()
                if (
                    self._last_active_strategies_update_at == 0.0
                    or _now - self._last_active_strategies_update_at >= self._regime_check_interval
                ):
                    await self._update_active_strategies()
                    self._last_active_strategies_update_at = _now

                # Capital Arbiter — check regime-based capital allowance before each
                # strategy.  "Resting" strategies (zero allocation) are skipped.
                _arb_regime = self._current_regime
                _arb_balance = self._cached_balance or Decimal("0")

                # Recalculate price zones every 14 ATR bars (≈ every regime update)
                if self.current_price is not None:
                    self._atr_bar_count += 1
                    if self._atr_bar_count >= 14:
                        self._atr_bar_count = 0
                        # atr_pct from last analysis → approx ATR in price units
                        _atr_abs = (
                            Decimal(str(_arb_regime.atr_pct / 100)) * self.current_price
                            if _arb_regime is not None
                            else Decimal("0")
                        )
                        self.price_zone_allocator.get_zones(self.current_price, _atr_abs)

                # Process Grid + DCA (hybrid coordination or independent)
                _grid_capital_ok = _arb_regime is None or self.capital_arbiter.get_allowed_capital(
                    "grid", _arb_regime.regime, _arb_balance
                ) > Decimal("0")
                _dca_capital_ok = _arb_regime is None or self.capital_arbiter.get_allowed_capital(
                    "dca", _arb_regime.regime, _arb_balance
                ) > Decimal("0")
                grid_active = (
                    self.grid_engine and self._is_strategy_active("grid") and _grid_capital_ok
                )
                dca_active = (
                    self.dca_engine
                    and self.current_price
                    and self._is_strategy_active("dca")
                    and _dca_capital_ok
                )

                if self._recovery_in_progress:
                    await self._process_recovery_logic()
                elif self._recovery_coordinator is not None and await self._check_recovery_trigger():
                    await self._process_recovery_logic()
                elif grid_active and dca_active and self.hybrid_strategy:
                    await self._process_hybrid_logic()
                else:
                    if grid_active:
                        await self._process_grid_orders()
                    if dca_active:
                        await self._process_dca_logic()

                # Process Trend-Follower logic
                _tf_capital_ok = _arb_regime is None or self.capital_arbiter.get_allowed_capital(
                    "tf", _arb_regime.regime, _arb_balance
                ) > Decimal("0")
                if (
                    self.trend_follower_strategy
                    and self.current_price
                    and self._is_strategy_active("trend_follower")
                    and _tf_capital_ok
                ):
                    await self._process_trend_follower_logic()

                # Process SMC logic
                _smc_capital_ok = _arb_regime is None or self.capital_arbiter.get_allowed_capital(
                    "smc", _arb_regime.regime, _arb_balance
                ) > Decimal("0")
                if (
                    self.smc_strategy
                    and self.current_price
                    and self._is_strategy_active("smc")
                    and _smc_capital_ok
                ):
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

    # =========================================================================
    # Recovery: DCA cascade when Grid hits lower boundary
    # =========================================================================

    async def _check_recovery_trigger(self) -> bool:
        """Check if price breached grid lower and trigger recovery.

        Returns True if recovery was triggered (caller should skip normal grid).
        All data is local — no exchange queries.
        """
        if (
            self._recovery_coordinator is None
            or self.grid_engine is None
            or self.current_price is None
            or self._recovery_in_progress
        ):
            return False

        grid_lower = self.grid_engine.lower_price
        if not self._recovery_coordinator.should_trigger(grid_lower, self.current_price):
            return False

        # SMC support from cached context (no df needed)
        smc_support: Decimal | None = None
        smc_ctx = self.smc_structure_analyzer.get_cached_context(self.config.symbol)
        if smc_ctx is not None:
            smc_support = RecoveryCoordinator.find_smc_support(smc_ctx, self.current_price)

        # Underwater positions from local tracker
        underwater_raw = self.grid_engine.get_underwater_positions(self.current_price)
        if not underwater_raw:
            logger.warning("recovery_trigger_no_underwater_positions")
            return False

        underwater = [
            UnderwaterPosition(
                pos_id=p["order_id"],
                entry_price=p["entry_price"],
                size=p["size"],
            )
            for p in underwater_raw
        ]

        self._recovery_coordinator.enter_recovery(
            grid_positions=underwater,
            current_price=self.current_price,
            current_bar=0,
            smc_support=smc_support,
            base_order_size=self.grid_engine.amount_per_grid,
        )
        self._recovery_in_progress = True

        # Cancel all active grid orders on exchange
        for order_id in list(self.grid_engine.active_orders.keys()):
            try:
                await self.exchange.cancel_order(order_id, self.config.symbol)
                self.grid_engine.cancel_order(order_id)
            except Exception as e:
                logger.warning("recovery_cancel_grid_order_failed", order_id=order_id, error=str(e))

        await self._publish_event(EventType.RECOVERY_ENTERED, {
            "price": str(self.current_price),
            "grid_lower": str(grid_lower),
            "underwater_positions": len(underwater),
            "smc_support": str(smc_support) if smc_support else None,
        })
        logger.info(
            "recovery_entered_live",
            price=float(self.current_price),
            grid_lower=float(grid_lower),
            underwater=len(underwater),
        )
        return True

    async def _process_recovery_logic(self) -> None:
        """Process one tick of recovery DCA cascade."""
        if (
            self._recovery_coordinator is None
            or not self._recovery_coordinator.is_active
            or self.current_price is None
        ):
            return

        recovery_action = self._recovery_coordinator.on_price_update(self.current_price)

        # Place DCA signals as market orders
        dca_fills: list[UnderwaterPosition] = []
        balance = self._cached_balance or Decimal("0")
        state = self._recovery_coordinator.state

        for signal in recovery_action.dca_signals:
            # Recovery-specific capital cap
            if state is not None:
                total_spent = sum(f.entry_price * f.size for f in state.dca_fills)
                max_capital = balance * self._recovery_coordinator.config.max_recovery_capital_pct
                mult = Decimal(str(signal.metadata.get("multiplier", 1.0))) if signal.metadata else Decimal("1")
                next_cost = self.current_price * mult * state.base_order_size / self.current_price
                if total_spent + (next_cost * self.current_price) > max_capital:
                    logger.warning("recovery_dca_capital_limit",
                                   spent=float(total_spent), limit=float(max_capital))
                    break

            # Standard risk manager gate
            if self.risk_manager:
                exposure = self._get_total_open_exposure()
                order_value = state.base_order_size * mult if state else Decimal("0")
                risk_check = self.risk_manager.check_trade(
                    order_value=order_value,
                    current_position=exposure,
                    available_balance=balance,
                )
                if not risk_check.allowed:
                    logger.warning("recovery_dca_blocked_by_risk", reason=risk_check.reason)
                    break

            # Place market buy order
            try:
                mult_val = Decimal(str(signal.metadata.get("multiplier", 1.0))) if signal.metadata else Decimal("1")
                base_amount = (
                    state.base_order_size * mult_val / self.current_price
                ).quantize(Decimal("0.000001")) if state else Decimal("0")

                if not self.config.dry_run and base_amount > 0:
                    result = await self.exchange.create_order(
                        symbol=self.config.symbol,
                        order_type="market",
                        side="buy",
                        amount=float(base_amount),
                    )
                    fill_price = Decimal(str(
                        result.get("average", result.get("price", self.current_price))
                    ))
                else:
                    fill_price = self.current_price

                fill = UnderwaterPosition(
                    pos_id=f"recovery_dca_{signal.metadata.get('dca_order_num', 0) if signal.metadata else 0}",
                    entry_price=fill_price,
                    size=base_amount,
                )
                dca_fills.append(fill)

                await self._publish_event(EventType.RECOVERY_DCA_PLACED, {
                    "price": str(fill_price),
                    "dca_level": signal.metadata.get("dca_order_num", 0) if signal.metadata else 0,
                    "amount": str(base_amount),
                })
            except Exception as e:
                logger.error("recovery_dca_order_failed", error=str(e))

        # Register fills for blended avg recalculation
        if dca_fills:
            self._recovery_coordinator.on_price_update(self.current_price, new_fills=dca_fills)

        # Handle exit conditions
        if recovery_action.should_close_all:
            await self._execute_recovery_exit(recovery_action)

    async def _execute_recovery_exit(self, recovery_action) -> None:
        """Close all recovery positions and restart grid."""
        if self.current_price is None:
            return

        # Close all open positions for this symbol
        if not self.config.dry_run:
            try:
                positions = await self.exchange.fetch_positions([self.config.symbol])
                for pos in positions:
                    contracts = abs(float(pos.get("contracts", 0)))
                    if contracts > 0:
                        side = "sell" if pos.get("side") == "long" else "buy"
                        await self.exchange.create_order(
                            symbol=self.config.symbol,
                            order_type="market",
                            side=side,
                            amount=contracts,
                            params={"reduceOnly": True},
                        )
            except Exception as e:
                logger.error("recovery_exit_close_failed", error=str(e))

        event_type = (
            EventType.RECOVERY_TP_HIT
            if "tp_hit" in (recovery_action.close_reason or "")
            else EventType.RECOVERY_TIMEOUT
        )
        await self._publish_event(event_type, {
            "reason": recovery_action.close_reason,
            "new_grid_range": (
                [str(x) for x in recovery_action.new_grid_range]
                if recovery_action.new_grid_range else None
            ),
        })

        # Restart grid with new range
        if recovery_action.new_grid_range and self.grid_engine is not None:
            new_lower, new_upper = recovery_action.new_grid_range
            self.grid_engine = GridEngine(
                symbol=self.config.symbol,
                upper_price=new_upper,
                lower_price=new_lower,
                grid_levels=self.grid_engine.grid_levels,
                amount_per_grid=self.grid_engine.amount_per_grid,
                profit_per_grid=self.grid_engine.profit_per_grid,
                grid_type=self.grid_engine.grid_type,
            )
            grid_orders = self.grid_engine.initialize_grid(self.current_price)
            await self._place_grid_orders(grid_orders)

            await self._publish_event(EventType.RECOVERY_EXITED, {
                "new_lower": str(new_lower),
                "new_upper": str(new_upper),
            })

        self._recovery_in_progress = False
        logger.info("recovery_exit_complete", reason=recovery_action.close_reason)

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

    async def _start_history_feed(self) -> None:
        """Backfill OHLCV history and launch a WebSocket kline feed.

        Called from ``start()`` when a HistoryManager is available and the
        strategy is SMC or TrendFollower.
        """
        if not self.history_manager:
            return

        symbol = self.config.symbol
        # Choose entry timeframe: SMC uses 5m, TrendFollower uses 1h
        if self.config.strategy == StrategyType.SMC:
            entry_tf = "5m"
            backfill_bars = getattr(self.config.smc, "m5_limit", 1000) if self.config.smc else 1000
        else:
            entry_tf = "1h"
            backfill_bars = 500

        try:
            logger.info(
                "history_backfill_starting",
                symbol=symbol,
                interval=entry_tf,
                target_bars=backfill_bars,
            )
            await self.history_manager.backfill(symbol, entry_tf, target_bars=backfill_bars)
            logger.info("history_backfill_done", symbol=symbol, interval=entry_tf)
        except Exception as e:
            logger.warning("history_backfill_failed", error=str(e))

        # Launch background WebSocket feed
        sandbox = bool(getattr(self.config.exchange, "sandbox", False))
        feed = CandleWSFeed(
            history_manager=self.history_manager,
            symbol=symbol,
            interval=entry_tf,
            exchange_name=getattr(self.config.exchange, "exchange_id", "bybit"),
            sandbox=sandbox,
        )
        self._candle_ws_task = asyncio.create_task(feed.run())
        logger.info("candle_ws_feed_launched", symbol=symbol, interval=entry_tf)

    async def _run_dca_catchup(self) -> None:
        """Place missing DCA levels below current price on startup (catch-up mode).

        Called once from ``start()`` after reconciliation, only when
        ``config.dca.catch_up_enabled`` is True.
        """
        if not self.dca_engine or not self.config.dca or not self.current_price:
            return

        dca_cfg = self.config.dca
        min_order_size = (
            Decimal(str(self.config.risk_management.min_order_size))
            if self.config.risk_management
            else Decimal("10")
        )

        try:
            # 1. Fetch historical OHLCV (1h) for reference-price analysis
            ohlcv = await self.exchange.fetch_ohlcv(
                symbol=self.config.symbol,
                timeframe="1h",
                limit=dca_cfg.catch_up_lookback_bars,
            )

            # 2. Fetch open orders to avoid duplicates
            open_orders = await self.exchange.fetch_open_orders(self.config.symbol)

            # 3. Build catch-up plan
            analyzer = DCAStartupAnalyzer(
                trigger_pct=Decimal(str(dca_cfg.trigger_percentage)),
                amount_per_step=Decimal(str(dca_cfg.amount_per_step)),
                max_steps=dca_cfg.max_steps,
                catch_up_max_orders=dca_cfg.catch_up_max_orders,
                catch_up_reference=dca_cfg.catch_up_reference,
                catch_up_lookback_bars=dca_cfg.catch_up_lookback_bars,
            )
            plan = analyzer.analyze(
                ohlcv=ohlcv,
                current_price=self.current_price,
                open_orders=open_orders,
                min_order_size=min_order_size,
            )

            # 4. Place orders
            placed = 0
            for level in plan.orders_to_place:
                logger.info(
                    "catchup_order_placing",
                    level=level.level_num,
                    price=float(level.price),
                    amount_usd=float(level.amount_usd),
                    dry_run=self.config.dry_run,
                )
                if not self.config.dry_run:
                    try:
                        base_amount = float(level.amount_usd / level.price)
                        await self.exchange.create_order(
                            symbol=self.config.symbol,
                            order_type="market",
                            side="buy",
                            amount=base_amount,
                        )
                        # Advance DCA state
                        self.dca_engine.execute_dca_step(level.price)
                    except Exception as e:
                        logger.error("catchup_order_failed", level=level.level_num, error=str(e))
                        continue
                placed += 1

            logger.info(
                "catchup_completed",
                placed=placed,
                skipped_covered=plan.skipped_covered,
                skipped_above=plan.skipped_above,
            )

        except Exception as e:
            logger.error("dca_catchup_error", error=str(e), exc_info=True)

    async def _process_dca_logic(self) -> None:
        """Process DCA triggers and take profit logic."""
        if not self.dca_engine or not self.current_price:
            return

        # Update DCA engine with current price
        dca_actions = self.dca_engine.update_price(self.current_price)

        # Handle DCA trigger
        if dca_actions["dca_triggered"] and self.state == BotState.RUNNING:
            dca_step_amount = self.dca_engine.amount_per_step

            # Check per-bot risk limits (uses cross-strategy exposure)
            if self.risk_manager:
                current_position = self._get_total_open_exposure()
                balance = self._cached_balance or await self._get_available_balance()

                risk_check = self.risk_manager.check_trade(
                    dca_step_amount, current_position, balance
                )
                if not risk_check:
                    logger.warning("dca_blocked_by_risk", reason=risk_check.reason)
                    return

            # Check cross-pair portfolio limits (multi-bot deployments)
            if not self._portfolio_rm_check(dca_step_amount):
                return

            # Place order on exchange first, then advance state (#231)
            if not self.config.dry_run:
                try:
                    await self._place_dca_order()
                except Exception as e:
                    logger.error("dca_order_failed_skipping_state", error=str(e))
                    return
                # Commit allocation in portfolio pool after confirmed order
                if self._portfolio_rm is not None:
                    self._portfolio_rm.confirm_allocation(
                        self.config.name,
                        dca_step_amount,
                        symbol=str(self.config.symbol),
                    )

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
            # Capture position size before closing (used for portfolio release below)
            closed_amount = (
                self.dca_engine.position.amount if self.dca_engine.position else Decimal("0")
            )
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

            # Release allocated capital back to portfolio pool
            if self._portfolio_rm is not None and closed_amount > 0:
                self._portfolio_rm.release_allocation(
                    self.config.name,
                    closed_amount,
                    symbol=str(self.config.symbol),
                )

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

        # Per-symbol global stop-loss check (PortfolioRiskManager)
        if self._portfolio_rm is not None:
            triggered = self._portfolio_rm.tick_global_stop_loss(symbols=[str(self.config.symbol)])
            for symbol in triggered:
                logger.critical(
                    "global_stop_loss_force_close",
                    symbol=symbol,
                    bot_name=self.config.name,
                )
                await self._force_close_all_positions(symbol)

    async def _force_close_all_positions(self, symbol: str) -> None:
        """
        Force-close all open positions for *symbol* across every active strategy.

        Called when ``PortfolioRiskManager.tick_global_stop_loss`` detects that
        aggregate per-symbol risk has exceeded ``global_stop_loss_pct``.

        Actions:
        1. Cancel all open orders for the symbol.
        2. Close all open positions via market ``reduceOnly=True`` orders.
        3. Publish a ``FORCE_CLOSE_ALL`` event (for Telegram notifications etc.).
        """
        logger.critical(
            "force_close_all_positions",
            symbol=symbol,
            bot_name=self.config.name,
        )

        # Cancel all open exchange orders for the symbol
        try:
            await self.exchange.cancel_all_orders(symbol)
            logger.info("force_close_all_orders_cancelled", symbol=symbol)
        except Exception as exc:
            logger.error("force_close_cancel_orders_failed", symbol=symbol, error=str(exc))

        # Close all positions known to internal strategy adapters.
        # We use get_active_positions() from each strategy then send a
        # reduceOnly market order for each open position.
        all_positions = []
        for strategy_obj in [
            self.smc_strategy,
            self.trend_follower_strategy,
        ]:
            if strategy_obj is not None and hasattr(strategy_obj, "get_active_positions"):
                all_positions.extend(strategy_obj.get_active_positions())

        for pos_info in all_positions:
            try:
                close_side = "sell" if pos_info.direction.value == "long" else "buy"
                await self.exchange.create_order(
                    symbol=symbol,
                    order_type="market",
                    side=close_side,
                    amount=float(pos_info.size),
                    params={"reduceOnly": True},
                )
                logger.info(
                    "force_close_position_executed",
                    position_id=pos_info.position_id,
                    symbol=symbol,
                    side=close_side,
                    size=str(pos_info.size),
                )
            except Exception as exc:
                logger.error(
                    "force_close_position_failed",
                    position_id=pos_info.position_id,
                    symbol=symbol,
                    error=str(exc),
                )

        # Close Grid and DCA positions as well
        if self.grid_engine:
            try:
                await self.exchange.cancel_all_orders(symbol)
            except Exception as exc:
                logger.error("force_close_grid_cancel_failed", symbol=symbol, error=str(exc))

        if self.dca_engine and self.dca_engine.position is not None:
            try:
                await self._close_dca_position()
                logger.info("force_close_dca_position_closed", symbol=symbol)
            except Exception as exc:
                logger.error("force_close_dca_failed", symbol=symbol, error=str(exc))

        await self._publish_event(
            EventType.EMERGENCY,
            {
                "type": "force_close_all",
                "symbol": symbol,
                "bot_name": self.config.name,
                "reason": "global_stop_loss_triggered",
            },
        )

    def _portfolio_rm_check(self, amount: Decimal) -> bool:
        """Check portfolio-level risk before committing capital.

        Returns True when no PortfolioRiskManager is configured (single-bot
        deployments) so existing behaviour is fully preserved.
        """
        if self._portfolio_rm is None:
            return True
        balance = self._cached_balance  # snapshot from current loop iteration
        result = self._portfolio_rm.check_allocation(
            bot_name=self.config.name,
            amount=amount,
            balance=balance,
            symbol=str(self.config.symbol),
        )
        if not result.approved:
            logger.warning(
                "portfolio_rm_blocked_order",
                bot_name=self.config.name,
                amount=float(amount),
                reason=result.reason,
                status=result.status,
            )
        return result.approved

    def _get_total_open_exposure(self) -> Decimal:
        """Return total capital currently exposed across all active strategies.

        Sums buy-side notional for Grid, invested capital for DCA, and
        position sizes for TrendFollower and SMC.  Used as ``current_position``
        in :meth:`~bot.core.risk_manager.RiskManager.check_trade` so that every
        strategy sees combined cross-strategy exposure before opening new trades.
        """
        exposure = Decimal("0")

        # Grid: all active buy-side filled positions (open buy orders represent
        # capital already committed / earmarked for the position)
        if self.grid_engine:
            for order in self.grid_engine.active_orders.values():
                if order.side == "buy":
                    exposure += order.price * order.amount

        # DCA: total capital in the current open position
        if self.dca_engine:
            exposure += self.dca_engine.get_total_invested()

        # TrendFollower: sum of all active position sizes (quote currency)
        if self.trend_follower_strategy:
            for pos in self.trend_follower_strategy.position_manager.active_positions.values():
                exposure += pos.size

        # SMC: sum of all active position sizes (quote currency)
        if self.smc_strategy:
            for pos in self.smc_strategy.get_active_positions():
                exposure += pos.size

        return exposure

    async def _ensure_hedge_mode(self) -> None:
        """Enable hedge mode (both-side) on Bybit for the grid symbol.

        Called once during bot start.  Idempotent — Bybit returns retCode=0
        if the mode is already set.  Failures are logged but non-fatal.
        """
        if not self.grid_engine:
            return
        try:
            if hasattr(self.exchange, "set_position_mode"):
                await self.exchange.set_position_mode(self.config.symbol, mode=3)
                self._hedge_mode_enabled = True
                logger.info("hedge_mode_enabled", symbol=self.config.symbol)
            else:
                logger.warning("exchange_no_set_position_mode", exchange=type(self.exchange).__name__)
        except Exception as e:
            logger.warning("hedge_mode_enable_failed", error=str(e))

    async def _switch_grid_direction(self, new_direction: GridDirection) -> None:
        """Switch grid direction on regime change.

        1. Gets order IDs to cancel from GridEngine.set_direction().
        2. Cancels them on exchange.
        3. Reinitializes grid in new direction.
        """
        grid_engine = getattr(self, "grid_engine", None)
        current_price = getattr(self, "current_price", None)
        if not grid_engine or not current_price:
            return
        current_direction = getattr(self, "_grid_direction", GridDirection.LONG)
        if new_direction == current_direction:
            return

        logger.info(
            "grid_direction_switch_start",
            old=current_direction,
            new=new_direction,
            symbol=self.config.symbol,
        )

        cancel_ids = grid_engine.set_direction(new_direction)
        self._grid_direction = new_direction

        # Cancel all existing grid orders on exchange
        for order_id in cancel_ids:
            try:
                await self.exchange.cancel_order(order_id, self.config.symbol)
            except Exception as e:
                logger.warning("grid_cancel_failed_on_switch", order_id=order_id, error=str(e))

        # Rebuild grid in new direction
        new_orders = grid_engine.initialize_grid(current_price)
        await self._place_grid_orders(new_orders)

        logger.info(
            "grid_direction_switch_complete",
            direction=new_direction,
            orders_placed=len(new_orders),
        )

    async def _place_grid_orders(self, orders: list) -> None:
        """Place grid orders on exchange.

        Checks portfolio-level halt before placing.  Buy-side notional is
        checked against the shared capital pool so a halted portfolio
        (e.g. global drawdown > stop-loss) prevents new grid deployments.
        """
        if getattr(self, "_portfolio_rm", None) is not None:
            buy_notional = sum(
                o.amount * o.price for o in orders if getattr(o, "side", "") == "buy"
            )
            if buy_notional > 0 and not self._portfolio_rm_check(buy_notional):
                logger.warning(
                    "grid_orders_blocked_by_portfolio_rm",
                    buy_notional=float(buy_notional),
                    order_count=len(orders),
                )
                return
        for order in orders:
            await self._place_single_order(order)

    async def _place_single_order(self, order: Any) -> None:
        """Place a single order on exchange."""
        try:
            # Determine positionIdx for hedge mode:
            # 1 = Buy/Long side, 2 = Sell/Short side (hedge mode only)
            position_idx = 0
            if self._hedge_mode_enabled:
                if self._grid_direction == GridDirection.SHORT:
                    # SHORT grid: sell opens short (idx=2), buy closes short (idx=2)
                    position_idx = 2
                else:
                    # LONG grid: buy opens long (idx=1), sell closes long (idx=1)
                    position_idx = 1

            result = await self.exchange.create_order(
                symbol=self.config.symbol,
                order_type="limit",
                side=order.side,
                amount=float(order.amount),
                price=float(order.price),
                position_idx=position_idx,
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
            # Fetch OHLCV data — HistoryManager (DB-first) when available
            if self.history_manager:
                df = await self.history_manager.get_candles(self.config.symbol, "1h", limit=100)
                if "time" in df.columns:
                    df = df.rename(columns={"time": "timestamp"})
            else:
                ohlcv = await self.exchange.fetch_ohlcv(
                    symbol=self.config.symbol,
                    timeframe="1h",
                    limit=100,
                )
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

                # Risk check (uses cross-strategy exposure)
                if self.risk_manager:
                    current_position_value = self._get_total_open_exposure()
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

                    self.trend_follower_strategy.close_position(
                        position_id, exit_reason, self.current_price
                    )

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
                params={"reduceOnly": True},
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
            # HistoryManager (DB-first) when available; otherwise direct REST call
            smc_cfg = self.config.smc
            m5_limit = getattr(smc_cfg, "m5_limit", 1000) if smc_cfg else 1000
            h1_limit = getattr(smc_cfg, "h1_limit", 200) if smc_cfg else 200

            def _to_df(ohlcv_data: list) -> pd.DataFrame:
                df = pd.DataFrame(
                    ohlcv_data,
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                df.set_index("timestamp", inplace=True)
                return df

            if self.history_manager:
                df_d1, df_h4, df_h1, df_m5 = await asyncio.gather(
                    self.history_manager.get_candles(self.config.symbol, "1d", limit=200),
                    self.history_manager.get_candles(self.config.symbol, "4h", limit=200),
                    self.history_manager.get_candles(self.config.symbol, "1h", limit=h1_limit),
                    self.history_manager.get_candles(self.config.symbol, "5m", limit=m5_limit),
                )
                # Rename "time" column to match downstream expectations
                for df_item in (df_d1, df_h4, df_h1, df_m5):
                    if "time" in df_item.columns:
                        df_item.set_index("time", inplace=True)
                df_m15 = df_m5  # alias: SMC adapter receives M5 data as entry TF
            else:
                ohlcv_d1, ohlcv_h4, ohlcv_h1, ohlcv_m15 = await asyncio.gather(
                    self.exchange.fetch_ohlcv(symbol=self.config.symbol, timeframe="1d", limit=200),
                    self.exchange.fetch_ohlcv(symbol=self.config.symbol, timeframe="4h", limit=200),
                    self.exchange.fetch_ohlcv(
                        symbol=self.config.symbol, timeframe="1h", limit=h1_limit
                    ),
                    self.exchange.fetch_ohlcv(
                        symbol=self.config.symbol, timeframe="5m", limit=m5_limit
                    ),
                )
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

                    # Risk check (uses cross-strategy exposure)
                    if self.risk_manager:
                        current_position_value = self._get_total_open_exposure()
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
                params={"reduceOnly": True},
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

        # Recovery status
        if self._recovery_coordinator is not None:
            status["recovery"] = self._recovery_coordinator.get_statistics()
            if self._recovery_coordinator.state:
                status["recovery_state"] = self._recovery_coordinator.state.to_dict()

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

            # Use SMCStructureAnalyzer as primary SMC source (independent of the
            # SMC trading strategy).  It maintains a 5-minute TTL cache per symbol,
            # so multiple callers share one context without redundant computation.
            smc_ctx = self.smc_structure_analyzer.get_context(self.config.symbol, df)

            # Pass context to the unified two-level classifier; it handles
            # ACCUMULATION/DISTRIBUTION detection and the indicator-noise freeze.
            analysis = self.market_regime_detector.analyze(df, smc_context=smc_ctx)

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
