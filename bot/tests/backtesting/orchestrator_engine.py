"""
BacktestOrchestratorEngine V3.0 — mirrors BotOrchestrator._main_loop() on historical data.

V3.0 changes vs V2.0:
- All strategies run in PARALLEL every bar (like the live bot, not sequential)
- Router provides hard weights (1.0 active / 0.0 inactive), mirrors HybridCoordinator
- Real PnL: (exit_price - entry_price) × amount  (replaces × 0.001 stub)
- Proper portfolio tracking via position_entry_prices dict
- Per-strategy P&L correctly attributed

Key differences from MultiTimeframeBacktestEngine (V1):
- Runs multiple strategy engines simultaneously (Grid + DCA + TrendFollower + SMC)
- Routes signals through StrategyRouter based on market regime
- Tracks per-strategy P&L and strategy switch events
- Integrates PortfolioRiskManager for position sizing

Usage::

    config = OrchestratorBacktestConfig(symbol="BTC/USDT")
    engine = BacktestOrchestratorEngine()
    result = await engine.run(data, config)
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from bot.core.capital_arbiter import (
    ALLOCATION as _ARBITER_ALLOCATION,
    CapitalArbiter,
    _REGIME_FAMILY as _ARBITER_REGIME_FAMILY,
    _UNKNOWN as _ARBITER_UNKNOWN,
    _normalise_strategy as _arbiter_norm,
)
from bot.core.risk_manager import RiskManager
from bot.core.smc.analyzer import SMCAnalyzer
from bot.core.virtual_position_manager import VirtualPosition, VirtualPositionManager
from bot.orchestrator.market_regime import (
    MarketRegimeDetector,
    RegimeAnalysis,
)
from bot.orchestrator.routing_config import RoutingConfig
from bot.orchestrator.strategy_conductor import StrategyConductor
from bot.strategies.base import BaseStrategy, ExitReason, SignalDirection
from bot.tests.backtesting.backtesting_engine import BacktestResult
from bot.tests.backtesting.market_simulator import MarketSimulator
from bot.tests.backtesting.multi_tf_data_loader import (
    MultiTimeframeData,
    MultiTimeframeDataLoader,
)
from bot.strategies.hybrid.recovery_config import RecoveryConfig
from bot.strategies.hybrid.recovery_coordinator import (
    RecoveryCoordinator,
    RecoveryPhase,
    UnderwaterPosition,
)
from bot.tests.backtesting.strategy_router import StrategyRouter

logger = logging.getLogger(__name__)


@dataclass
class StrategyPeriodMetrics:
    """Per-strategy metrics collected only during bars where the strategy was active (routed)."""

    bars_active: int = 0
    trades: int = 0
    realized_pnl: float = 0.0
    sharpe: float | None = None
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "bars_active": self.bars_active,
            "trades": self.trades,
            "realized_pnl": self.realized_pnl,
            "sharpe": self.sharpe,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate": self.win_rate,
        }


@dataclass
class OrchestratorBacktestConfig:
    """Configuration for the V2.0 orchestrator backtest engine."""

    symbol: str = "BTC/USDT"
    initial_balance: Decimal = Decimal("10000")
    lookback: int = 100
    warmup_bars: int = 14400

    # analyze_every_n — per-strategy intervals (mirrors live bot behaviour):
    #   Grid/DCA/TF: called every bar (price-reactive, lightweight)
    #   SMC:         called every 60 M5 bars = 300 sec (mirrors live bot 5-min throttle)
    default_analyze_every_n: int = 1  # Grid, DCA, TrendFollower — every M5 bar
    smc_analyze_every_n: int = 60  # SMC — every 300 sec (60 × 5 min bars)
    # SMC generate_signal frequency: every 12 M5 bars (= 1 hour).
    # Rationale: analyze_market runs every 60 bars — calling generate_signal every bar
    # during that window repeats identical signals (same OBs/FVGs, same state) and
    # causes 60× over-trading. 12 bars (hourly) matches SMC's primary H1 timeframe
    # and gives at most 5 signal checks per analysis window, matching live behaviour.
    smc_generate_signal_every_n: int = 12  # SMC signal check every 12 M5 bars (1 hour)

    # Recovery (DCA cascade when Grid hits lower boundary)
    enable_recovery: bool = False  # opt-in; requires grid enabled
    recovery_params: dict[str, Any] = field(default_factory=dict)

    # Strategies to include
    enable_grid: bool = True
    enable_dca: bool = True
    enable_trend_follower: bool = True
    enable_smc: bool = True  # enabled by default — mirrors live bot (demo_btc_smc)

    # Regime-based routing (key differentiator from V1)
    enable_strategy_router: bool = True
    router_cooldown_bars: int = (
        2  # 600 sec cooldown / 300 sec per M5 bar = 2 bars (matches live bot)
    )
    regime_check_every_n: int = 1  # every M5 bar — mirrors live bot 60-sec continuous check
    # RoutingConfig: when set, the router uses the YAML-based routing rules (single source of
    # truth), mirroring the live StrategySelector.  When None, a default RoutingConfig is
    # loaded from "configs/strategy_routing.yaml" at engine startup.
    routing_config: RoutingConfig | None = None

    # Additive routing (mirrors live CapitalArbiter model).
    # True (default): multiple strategies active simultaneously per CapitalArbiter
    #   ALLOCATION matrix — each non-zero allocation strategy runs at full position
    #   size with the allocation as a capital ceiling.
    # False (legacy): exclusive binary routing from StrategyRouter (one winner per regime).
    use_additive_routing: bool = True

    # Two-phase PRE_SWITCH gate — mirrors StrategySelector (issue #360 / C1 parity fix).
    # When enabled, regime transitions require a timer + optional SMC signal before
    # strategies are switched.  Eliminates live-vs-backtest routing divergence.
    enable_pre_switch_gate: bool = True
    # When True (default), transitions to/from trend regimes also require a BOS/CHoCH
    # structural signal from SMCAnalyzer before the gate confirms.
    pre_switch_require_smc: bool = True

    # StrategyConductor: when enabled, pushes StrategyDirective (capital allocation,
    # price range, key levels, restrictions) to active strategies on regime change,
    # mirroring the live bot's BotOrchestrator → StrategyConductor integration.
    enable_strategy_conductor: bool = True

    # Force-close behaviour on strategy deactivation.
    # Live bot default: close_positions_on_switch=False (keep positions, only cancel orders).
    # Set to True to force-close all positions when a strategy is deactivated by the router.
    force_close_on_deactivation: bool = False

    # Per-strategy parameters (passed to strategy factories)
    grid_params: dict[str, Any] = field(default_factory=dict)
    dca_params: dict[str, Any] = field(default_factory=dict)
    tf_params: dict[str, Any] = field(default_factory=dict)
    smc_params: dict[str, Any] = field(default_factory=dict)

    # Risk management — aligned with TradingCoreConfig defaults
    # Note: max_daily_loss_pct uses *cumulative* downward movement tracking in
    # RiskManager.update_balance(), so set generously to avoid false halts
    # from normal intraday price oscillations.
    enable_risk_manager: bool = True
    enable_vpm: bool = True
    enable_capital_arbiter: bool = True
    # max_position_size_pct: per-strategy cumulative cap (RiskManager receives per-strategy
    # position totals, so this is the max exposure ONE strategy can hold at once).
    max_position_size_pct: float = 0.25  # 25% of portfolio per strategy
    max_daily_loss_pct: float = 0.25  # 25% — generous to avoid false halts in backtest
    portfolio_stop_loss_pct: float = 0.15

    # Position sizing (fraction of balance per INDIVIDUAL signal).
    # Must be smaller than max_position_size_pct to allow multiple concurrent positions.
    risk_per_trade: Decimal = Decimal("0.02")
    max_position_pct: Decimal = Decimal("0.05")  # 5% per trade → up to 5 positions per strategy

    # Grid-specific per-position fraction.  Grid manages many levels simultaneously
    # (num_levels=20), so each individual order must be much smaller than max_position_pct.
    # When set, _handle_signal uses this value for the "grid" strategy instead of
    # max_position_pct.  Computed in run_backtest_v2 as max_position_size_pct / num_levels.
    # None = fall back to max_position_pct (backward-compatible).
    grid_position_pct: Decimal | None = None

    # VPM aggregate Grid SL threshold.  Live default is 10% (conservative for real trading).
    # In backtest with a single pair a 10% aggregate loss closes all Grid positions too
    # quickly — set to 0.25 (25%) to allow the grid to breathe within its ±12% range.
    vpm_max_grid_loss_pct: float = 0.25

    # Exchange fee simulation — aligned with TradingCoreConfig defaults (Bybit VIP0)
    maker_fee: Decimal = Decimal("0.0002")  # 0.02 % (was 0.1 % in old MarketSimulator default)
    taker_fee: Decimal = Decimal("0.00055")  # 0.055 %
    slippage: Decimal = Decimal("0.0003")  # 0.03 % average slippage

    @classmethod
    def from_yaml_config(
        cls,
        path: str,
        symbol: str,
        initial_balance: Decimal = Decimal("10000"),
    ) -> OrchestratorBacktestConfig:
        """Load backtest config from a live YAML config file.

        Finds all bots for *symbol* and maps ``grid`` / ``dca`` /
        ``trend_follower`` / ``smc`` YAML sections to the corresponding
        ``*_params`` dicts so the backtest engine mirrors the live bot's
        exact settings.

        P1.1 — Unified strategy_params block
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        If the YAML contains a top-level ``strategy_params:`` block, its
        values are used as universal defaults for **all** strategies.
        Strategy-specific sections supplement / override these defaults.

        The mapping is:

        ``strategy_params.risk_per_trade_pct``
            → ``grid_params``, ``dca_params``, ``tf_params``, ``smc_params``

        ``strategy_params.max_position_pct``
            → ``max_position_pct`` (overrides risk_management calculation)

        ``strategy_params.max_daily_loss_pct``
            → ``max_daily_loss_pct`` (overrides risk_management calculation)

        ``strategy_params.max_positions``
            → ``smc_params``, ``tf_params`` (strategy-level limit)

        Backward compatibility
        ~~~~~~~~~~~~~~~~~~~~~~
        Old per-strategy ``risk_per_trade`` / ``risk_per_trade_pct`` fields
        inside ``smc:`` / ``trend_follower:`` sections continue to work as
        per-strategy overrides.  Old per-grid ``amount_per_grid`` is also
        preserved.

        Args:
            path: Path to the live YAML config file.
            symbol: Trading pair in ``BASE/QUOTE`` format (e.g. ``"BTC/USDT"``).
            initial_balance: Starting portfolio value used to convert absolute
                USD position/loss limits to percentages. Defaults to $10,000.

        Example::

            cfg = OrchestratorBacktestConfig.from_yaml_config(
                "configs/phase7_demo.yaml", "BTC/USDT"
            )
            engine = BacktestOrchestratorEngine()
            result = await engine.run(data, cfg)
        """
        import yaml  # optional dep — only needed when calling this helper

        with open(path) as fh:
            raw = yaml.safe_load(fh)

        bots = raw.get("bots", [])
        # Only include bots whose auto_start=true (or unset) and symbol matches.
        # Skip fully-disabled bots such as the _m5 dry-run variant.
        matching = [b for b in bots if b.get("symbol") == symbol and b.get("auto_start", True)]

        # --- P1.1: Read top-level strategy_params block (universal defaults) ---
        sp = raw.get("strategy_params") or {}
        sp_risk_pct: Decimal | None = (
            Decimal(str(sp["risk_per_trade_pct"])) if "risk_per_trade_pct" in sp else None
        )
        sp_max_position_pct: Decimal | None = (
            Decimal(str(sp["max_position_pct"])) if "max_position_pct" in sp else None
        )
        sp_max_daily_loss_pct: float | None = (
            float(str(sp["max_daily_loss_pct"])) if "max_daily_loss_pct" in sp else None
        )
        sp_max_positions: int | None = sp.get("max_positions")
        sp_require_volume: bool | None = sp.get("require_volume_confirmation")
        sp_volume_multiplier: Decimal | None = (
            Decimal(str(sp["min_volume_multiplier"])) if "min_volume_multiplier" in sp else None
        )

        grid_params: dict = {}
        dca_params: dict = {}
        tf_params: dict = {}
        smc_params: dict = {}
        risk_per_trade = sp_risk_pct if sp_risk_pct is not None else Decimal("0.02")
        max_position_pct = (
            sp_max_position_pct if sp_max_position_pct is not None else Decimal("0.25")
        )
        # Use strategy_params.max_daily_loss_pct if provided; else derive from risk_management below
        max_daily_loss_pct: float = (
            sp_max_daily_loss_pct if sp_max_daily_loss_pct is not None else 0.25
        )
        _ib = initial_balance  # alias for brevity in calculations
        # Track whether per-field defaults have been set by strategy_params already
        _pos_pct_from_sp = sp_max_position_pct is not None
        _daily_loss_from_sp = sp_max_daily_loss_pct is not None

        for bot in matching:
            rm = bot.get("risk_management") or {}

            # --- grid ---
            if "grid" in bot and not grid_params:
                g = bot["grid"]
                grid_params = {
                    k: v
                    for k, v in {
                        "num_levels": g.get("grid_levels"),
                        "amount_per_grid": (
                            Decimal(str(g["amount_per_grid"])) if "amount_per_grid" in g else None
                        ),
                        "profit_per_grid": (
                            Decimal(str(g["profit_per_grid"])) if "profit_per_grid" in g else None
                        ),
                        # Propagate universal risk_per_trade_pct to grid (P1.1)
                        "risk_per_trade_pct": sp_risk_pct,
                    }.items()
                    if v is not None
                }

            # --- dca ---
            if "dca" in bot and not dca_params:
                d = bot["dca"]
                dca_params = {
                    k: v
                    for k, v in {
                        "price_deviation_pct": (
                            Decimal(str(d["trigger_percentage"]))
                            if "trigger_percentage" in d
                            else None
                        ),
                        "safety_order_size": (
                            Decimal(str(d["amount_per_step"])) if "amount_per_step" in d else None
                        ),
                        "max_safety_orders": d.get("max_steps"),
                        "take_profit_pct": (
                            Decimal(str(d["take_profit_percentage"]))
                            if "take_profit_percentage" in d
                            else None
                        ),
                        # Propagate universal risk_per_trade_pct to dca (P1.1)
                        "risk_per_trade_pct": sp_risk_pct,
                    }.items()
                    if v is not None
                }

            # --- trend_follower ---
            if "trend_follower" in bot and not tf_params:
                tf = bot["trend_follower"]
                # Per-strategy risk_per_trade_pct overrides universal default
                _tf_risk = (
                    Decimal(str(tf["risk_per_trade_pct"]))
                    if "risk_per_trade_pct" in tf
                    else sp_risk_pct
                )
                tf_params = {
                    k: v
                    for k, v in {
                        "ema_fast_period": tf.get("ema_fast_period"),
                        "ema_slow_period": tf.get("ema_slow_period"),
                        "atr_period": tf.get("atr_period"),
                        "rsi_period": tf.get("rsi_period"),
                        "risk_per_trade_pct": _tf_risk,
                        "max_position_size_usd": (
                            Decimal(str(tf["max_position_size_usd"]))
                            if "max_position_size_usd" in tf
                            else None
                        ),
                        # Propagate universal max_positions if set (P1.1)
                        "max_positions": (
                            sp_max_positions
                            if sp_max_positions is not None
                            else tf.get("max_positions")
                        ),
                    }.items()
                    if v is not None
                }

            # --- smc (skip _m5 variant bots) ---
            if "smc" in bot and bot.get("strategy") == "smc" and not smc_params:
                if bot.get("name", "").endswith("_m5"):
                    continue
                s = bot["smc"]
                # Support both new unified key (risk_per_trade_pct) and legacy key (risk_per_trade)
                # Per-strategy value overrides universal default
                _smc_risk = (
                    Decimal(str(s["risk_per_trade_pct"]))
                    if "risk_per_trade_pct" in s
                    else Decimal(str(s["risk_per_trade"])) if "risk_per_trade" in s else sp_risk_pct
                )
                smc_params = {
                    k: v
                    for k, v in {
                        "swing_length": s.get("swing_length"),
                        "risk_per_trade_pct": _smc_risk,
                        "min_risk_reward": (
                            Decimal(str(s["min_risk_reward"])) if "min_risk_reward" in s else None
                        ),
                        "max_position_size": (
                            Decimal(str(s["max_position_size"]))
                            if "max_position_size" in s
                            else None
                        ),
                        # Propagate universal max_positions if set (P1.1)
                        "max_positions": (
                            sp_max_positions
                            if sp_max_positions is not None
                            else s.get("max_positions")
                        ),
                    }.items()
                    if v is not None
                }
                if _smc_risk is not None:
                    risk_per_trade = _smc_risk

            # --- risk_management: derive max_position_pct and max_daily_loss_pct ---
            # P0.2: convert absolute USD limits → % of initial_balance (not hardcoded $10k).
            # Take the FIRST (primary) bot's value — same convention as max_daily_loss_pct.
            # strategy_params.max_position_pct takes precedence over risk_management values.
            if (
                not _pos_pct_from_sp
                and max_position_pct == Decimal("0.25")
                and "max_position_size" in rm
            ):
                max_pos_usd = Decimal(str(rm["max_position_size"]))
                max_position_pct = min(max_pos_usd / _ib, Decimal("1.0"))

            # Sync max_daily_loss from live YAML (P0.3): take the FIRST bot's value.
            # strategy_params.max_daily_loss_pct takes precedence.
            # e.g. BTC hybrid: max_daily_loss=$600, initial_balance=$10k → 6%
            if not _daily_loss_from_sp and max_daily_loss_pct == 0.25:  # still at fallback default
                for key in ("max_daily_loss", "max_daily_loss_usd"):
                    if key in rm:
                        live_daily_loss_usd = float(str(rm[key]))
                        max_daily_loss_pct = min(live_daily_loss_usd / float(_ib), 0.25)
                        break

        return cls(
            symbol=symbol,
            initial_balance=initial_balance,
            grid_params=grid_params,
            dca_params=dca_params,
            tf_params=tf_params,
            smc_params=smc_params,
            risk_per_trade=risk_per_trade,
            max_position_pct=max_position_pct,
            # P0.2: RiskManager per-strategy cap should allow multiple concurrent
            # positions.  Default 0.25 (25%) allows 5 × 5% trades per strategy.
            # Only override if the YAML explicitly sets max_position_size.
            max_position_size_pct=min(float(max_position_pct) * 5, 0.80),
            max_daily_loss_pct=max_daily_loss_pct,
        )


@dataclass
class OrchestratorBacktestResult(BacktestResult):
    """Extended result from the orchestrator backtest engine."""

    # Strategy routing events
    strategy_switches: list[dict[str, Any]] = field(default_factory=list)

    # Per-strategy P&L (approximate: based on balance at switch points)
    per_strategy_pnl: dict[str, float] = field(default_factory=dict)

    # Regime routing statistics
    regime_routing_stats: dict[str, int] = field(default_factory=dict)

    # How many times cooldown blocked a switch
    cooldown_events: int = 0

    # Per-strategy detailed metrics (only during active/routed bars)
    per_strategy_metrics: dict[str, StrategyPeriodMetrics] = field(default_factory=dict)

    # Signal blocking diagnostics
    signal_stats: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["orchestrator"] = {
            "strategy_switches": len(self.strategy_switches),
            "per_strategy_pnl": self.per_strategy_pnl,
            "regime_routing_stats": self.regime_routing_stats,
            "cooldown_events": self.cooldown_events,
            "per_strategy_metrics": {k: v.to_dict() for k, v in self.per_strategy_metrics.items()},
            "signal_stats": self.signal_stats,
        }
        return base


class BacktestOrchestratorEngine:
    """
    V2.0 backtest engine that orchestrates multiple strategies simultaneously.

    Execution loop (per M5 bar after warmup):
    1. Detect market regime every regime_check_every_n bars
    2. Route to active strategies via StrategyRouter (with cooldown)
    3. For each active strategy: generate_signal → risk_check → execute
    4. update_positions → handle exits
    5. Track equity + per-strategy P&L
    6. Portfolio risk manager check

    The engine requires strategy factories to be registered before calling run().
    """

    def __init__(self) -> None:
        self.data_loader = MultiTimeframeDataLoader()
        self._strategy_factories: dict[str, Any] = {}  # name → callable(params) → BaseStrategy

    def register_strategy_factory(
        self,
        name: str,
        factory: Any,  # Callable[[dict], BaseStrategy]
    ) -> None:
        """Register a strategy factory for a given strategy name."""
        self._strategy_factories[name] = factory

    async def run(
        self,
        data: MultiTimeframeData,
        config: OrchestratorBacktestConfig,
        progress_callback=None,  # async (pct, bars_done, total_bars, portfolio_value) -> None
    ) -> OrchestratorBacktestResult:
        """
        Run the full orchestrator backtest.

        Args:
            data:   Pre-loaded multi-timeframe data.
            config: Engine configuration.

        Returns:
            OrchestratorBacktestResult with full metrics.
        """
        # Build strategy instances from factories
        strategies = self._build_strategies(config)
        if not strategies:
            raise ValueError(
                "No strategies could be built. Register factories with "
                "register_strategy_factory() or pass strategies via config."
            )

        # Simulator — use fees from config (Bybit VIP0 by default)
        simulator = MarketSimulator(
            symbol=config.symbol,
            initial_balance_quote=config.initial_balance,
            maker_fee=config.maker_fee,
            taker_fee=config.taker_fee,
        )

        # Regime detector
        regime_detector = MarketRegimeDetector()

        # SMC analyzer — provides structural context (BOS/CHoCH) to regime detector
        # and PRE_SWITCH gate, mirroring the live bot's SMCStructureAnalyzer usage.
        # swing_length=10 matches the live SMCConfig default for H1 analysis.
        smc_analyzer = SMCAnalyzer(swing_strength=10)

        # Strategy router — uses RoutingConfig as single source of truth to mirror
        # the live StrategySelector.  Fall back to loading the default YAML when
        # no explicit routing_config was supplied.
        _routing_config = config.routing_config
        if _routing_config is None:
            import os

            _default_yaml = os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "..",
                "configs",
                "strategy_routing.yaml",
            )
            _routing_config = RoutingConfig(_default_yaml)

        router = StrategyRouter(
            routing_config=_routing_config,
            cooldown_bars=config.router_cooldown_bars,
            enable_pre_switch_gate=config.enable_pre_switch_gate,
            require_smc_confirmation=config.pre_switch_require_smc,
        )

        # Strategy conductor — pushes directives (capital allocation, price range,
        # restrictions) to strategies on regime change, mirroring live bot behaviour.
        conductor: StrategyConductor | None = None
        if config.enable_strategy_conductor:
            # StrategyConductor needs a StrategyRegistry but we bypass it by
            # passing strategy_instances directly to on_regime_change().
            # Create a minimal registry that won't be used.
            from bot.orchestrator.strategy_registry import StrategyRegistry

            _registry = StrategyRegistry(max_strategies=len(strategies))
            conductor = StrategyConductor(registry=_registry)

        # Risk manager
        risk_manager: RiskManager | None = None
        if config.enable_risk_manager:
            max_pos = config.initial_balance * Decimal(str(config.max_position_size_pct))
            max_daily = config.initial_balance * Decimal(str(config.max_daily_loss_pct))
            risk_manager = RiskManager(
                max_position_size=max_pos,
                min_order_size=Decimal("10"),
                max_daily_loss=max_daily,
            )
            risk_manager.initialize_balance(config.initial_balance)

        # VirtualPositionManager and CapitalArbiter
        vpm: VirtualPositionManager | None = None
        capital_arbiter: CapitalArbiter | None = None
        if config.enable_vpm:
            vpm = VirtualPositionManager(max_grid_loss_pct=config.vpm_max_grid_loss_pct)
        if config.enable_capital_arbiter and vpm is not None:
            capital_arbiter = CapitalArbiter(vpm)

        # Recovery coordinator — opt-in DCA cascade when Grid hits lower boundary
        recovery_coordinator: RecoveryCoordinator | None = None
        if config.enable_recovery and config.enable_grid and "grid" in strategies:
            from bot.strategies.grid_adapter import GridAdapter

            _grid_strat = strategies["grid"]
            if isinstance(_grid_strat, GridAdapter):
                _rcfg = RecoveryConfig(enabled=True, **config.recovery_params)
                _rcfg.validate()
                recovery_coordinator = RecoveryCoordinator(_rcfg)
                _grid_strat.set_recovery_enabled(True)
                logger.info("Recovery coordinator enabled for Grid strategy")

        # Per-strategy state tracking
        position_amounts: dict[str, dict[str, Decimal]] = {name: {} for name in strategies}
        position_directions: dict[str, dict[str, SignalDirection]] = {
            name: {} for name in strategies
        }
        position_entry_prices: dict[str, dict[str, Decimal]] = {name: {} for name in strategies}
        vpm_pos_ids: dict[str, dict[str, str]] = {name: {} for name in strategies}
        per_strategy_pnl: dict[str, Decimal] = {name: Decimal("0") for name in strategies}
        regime_routing_stats: dict[str, int] = {}
        cooldown_events = 0
        current_regime: RegimeAnalysis | None = None

        # Per-strategy period metrics accumulators
        strat_bars_active: dict[str, int] = dict.fromkeys(strategies, 0)
        strat_trades: dict[str, int] = dict.fromkeys(strategies, 0)
        # Per-strategy individual trade PnLs for accurate win_rate calculation
        strat_trade_pnls: dict[str, list[float]] = {name: [] for name in strategies}
        # Per-strategy equity snapshots (only when active) for Sharpe + drawdown
        strat_equity: dict[str, list[float]] = {name: [] for name in strategies}

        # Signal blocking diagnostics — count signals blocked at each gate
        signal_stats: dict[str, dict[str, int]] = {
            name: {
                "signals_generated": 0,
                "blocked_by_router": 0,  # weight=0.0
                "blocked_by_arbiter": 0,  # CapitalArbiter zero allocation
                "blocked_by_balance": 0,  # insufficient quote balance
                "blocked_by_risk_mgr": 0,  # RiskManager.check_trade() rejected
                "blocked_by_error": 0,  # open_position/create_order exception
                "executed": 0,  # successfully opened
            }
            for name in strategies
        }

        # Execution loop
        equity_curve: list[dict[str, Any]] = []
        peak_value = config.initial_balance
        max_drawdown = Decimal("0")
        base_df = data.m5
        total_bars = len(base_df)

        # P0.5 — DCA warmup catch-up: mirror live DCAStartupAnalyzer._run_dca_catchup()
        # The live bot tracks price from day 1; the backtest starts fresh after warmup_bars.
        # Without catch-up, DCA._recent_high = 0 and can never trigger until a new high
        # is observed, causing the first ~50 bars to be DCA-silent even if price already
        # dropped far enough. Fix: set _recent_high from warmup data and, if price has
        # fallen enough, pre-open catch-up orders as if they had been placed during warmup.
        if "dca" in strategies and config.enable_dca and config.warmup_bars > 0:
            await self._run_dca_warmup_catchup(
                dca_strategy=strategies["dca"],
                base_df=base_df,
                warmup_bars=config.warmup_bars,
                config=config,
                simulator=simulator,
                position_amounts=position_amounts["dca"],
                position_directions=position_directions["dca"],
                position_entry_prices=position_entry_prices["dca"],
            )

        tradeable_bars = total_bars - config.warmup_bars
        _progress_interval = max(tradeable_bars // 20, 1000)  # log every 5%
        _progress_interval_sec = 300.0  # 5 minutes between progress reports
        _last_progress_t = _time.monotonic()
        _run_start_t = _last_progress_t

        for i in range(config.warmup_bars, total_bars):
            df_d1, df_h4, df_h1, df_m15, df_m5 = self.data_loader.get_context_at(
                data, base_index=i, lookback=config.lookback
            )
            current_price = Decimal(str(base_df.iloc[i]["close"]))
            await simulator.set_price(current_price)

            bars_since_warmup = i - config.warmup_bars

            # Progress logging — time-based (every _progress_interval_sec) OR every 5%
            _now_t = _time.monotonic()
            _bar_pct_hit = (
                bars_since_warmup > 0 and bars_since_warmup % _progress_interval == 0
            )
            _time_hit = (_now_t - _last_progress_t) >= _progress_interval_sec
            if _bar_pct_hit or _time_hit:
                _last_progress_t = _now_t
                pct = bars_since_warmup / tradeable_bars * 100 if tradeable_bars > 0 else 0
                pv = float(simulator.get_portfolio_value())
                elapsed_min = (_now_t - _run_start_t) / 60.0
                logger.info(
                    "progress: %.1f%% (%d/%d bars) | price=%.2f | portfolio=$%.2f | elapsed=%.1fm",
                    pct, bars_since_warmup, tradeable_bars, float(current_price), pv, elapsed_min,
                )
                if progress_callback is not None:
                    try:
                        await progress_callback(pct, bars_since_warmup, tradeable_bars, pv)
                    except Exception:
                        pass

            # Extract bar timestamp for PRE_SWITCH gate timer evaluation.
            # The DataFrame uses timestamp as its index (set_index("timestamp") in loader),
            # so we read from base_df.index[i], not from a column.
            bar_timestamp: datetime | None = None
            try:
                _idx_val = base_df.index[i]
                if hasattr(_idx_val, "to_pydatetime"):
                    bar_timestamp = _idx_val.to_pydatetime()
                elif isinstance(_idx_val, (int, float)):
                    from datetime import timezone as _tz
                    bar_timestamp = datetime.fromtimestamp(_idx_val / 1000, tz=_tz.utc)
            except Exception:
                pass

            # 1. Regime detection (with SMC structural context for smc_signal population)
            if bars_since_warmup % config.regime_check_every_n == 0 and len(df_h1) >= 60:
                # Compute SMC context from H1 data — populates smc_signal in
                # analysis_details so the PRE_SWITCH gate can check BOS/CHoCH.
                # Mirrors live bot: SMCStructureAnalyzer.get_context() → analyze_with_smc()
                try:
                    smc_ctx = smc_analyzer.analyze(df_h1)
                except Exception:
                    smc_ctx = None  # degrade gracefully if H1 data is insufficient
                current_regime = regime_detector.analyze(df_h1, smc_context=smc_ctx)
                regime_key = current_regime.regime.value
                regime_routing_stats[regime_key] = regime_routing_stats.get(regime_key, 0) + 1

                # 1b. Push StrategyConductor directives on regime change
                # Mirrors live: BotOrchestrator → StrategyConductor.on_regime_change()
                if conductor is not None:
                    conductor.on_regime_change(
                        current_regime, strategy_instances=strategies
                    )

            # 2. Strategy routing
            # Two modes controlled by config.use_additive_routing:
            #
            # ADDITIVE (default, mirrors live CapitalArbiter):
            #   Multiple strategies active simultaneously.  Weights (0/1) derived
            #   from CapitalArbiter.ALLOCATION matrix — any strategy with non-zero
            #   allocation in the current regime is active at full position size.
            #
            # EXCLUSIVE (legacy):
            #   Binary on/off from StrategyRouter (one winner per regime).
            regime_weights: dict[str, float] = {}
            if config.enable_strategy_router:
                router_event = router.on_bar(current_regime, i, current_timestamp=bar_timestamp)
                if router_event.cooldown_remaining > 0:
                    cooldown_events += 1

                if config.use_additive_routing and current_regime is not None:
                    # Additive: activity determined by CapitalArbiter allocation fractions.
                    _regime_str = current_regime.regime.value
                    _family = _ARBITER_REGIME_FAMILY.get(_regime_str, _ARBITER_UNKNOWN)
                    _alloc = _ARBITER_ALLOCATION[_family]
                    regime_weights = {
                        name: (
                            1.0
                            if _alloc.get(_arbiter_norm(name), Decimal("0")) > Decimal("0")
                            else 0.0
                        )
                        for name in strategies
                    }
                    # Deactivated: previously weight>0, now weight=0 (for force_close)
                    _deactivated_additive = {
                        name
                        for name in strategies
                        if regime_weights.get(name, 0.0) == 0.0
                        and name in router_event.active_strategies
                    }
                else:
                    # Exclusive: binary weights from StrategyRouter
                    regime_weights = {
                        name: (1.0 if name in router_event.active_strategies else 0.0)
                        for name in strategies
                    }
                    _deactivated_additive = router_event.deactivated

                # Handle deactivated strategies — mirrors live bot graceful_transition:
                # close_positions_on_switch=False (default) → keep positions open
                # close_positions_on_switch=True → force_close_all positions
                if config.force_close_on_deactivation:
                    for deact_name in _deactivated_additive:
                        deact_strat = strategies.get(deact_name)
                        if deact_strat is not None and hasattr(deact_strat, "force_close_all"):
                            forced = deact_strat.force_close_all()
                            if forced:
                                pnl_delta = await self._handle_exits(
                                    strat_name=deact_name,
                                    strategy=deact_strat,
                                    exits=forced,
                                    current_price=current_price,
                                    simulator=simulator,
                                    position_amounts=position_amounts[deact_name],
                                    position_directions=position_directions[deact_name],
                                    position_entry_prices=position_entry_prices[deact_name],
                                    vpm=vpm,
                                    vpm_pos_ids=vpm_pos_ids[deact_name],
                                )
                                per_strategy_pnl[deact_name] += pnl_delta
                                strat_trades[deact_name] += len(forced)
                                # Record individual trade PnLs for win_rate
                                if len(forced) > 0:
                                    per_trade = pnl_delta / len(forced)
                                    strat_trade_pnls[deact_name].extend(
                                        [float(per_trade)] * len(forced)
                                    )

            # 2b. Grid direction switching based on regime
            # BEAR_TREND / DISTRIBUTION → SHORT grid, everything else → LONG
            if "grid" in strategies and current_regime is not None:
                from bot.strategies.grid_adapter import GridAdapter as _GridDir

                _grid_dir = strategies["grid"]
                if isinstance(_grid_dir, _GridDir):
                    _regime_val = current_regime.regime.value
                    _desired_dir = (
                        SignalDirection.SHORT
                        if _regime_val in ("bear_trend", "distribution")
                        else SignalDirection.LONG
                    )
                    if _desired_dir != _grid_dir.direction:
                        _dir_forced = _grid_dir.set_direction(_desired_dir)
                        if _dir_forced:
                            _dir_pnl = await self._handle_exits(
                                strat_name="grid",
                                strategy=_grid_dir,
                                exits=_dir_forced,
                                current_price=current_price,
                                simulator=simulator,
                                position_amounts=position_amounts["grid"],
                                position_directions=position_directions["grid"],
                                position_entry_prices=position_entry_prices["grid"],
                                vpm=vpm,
                                vpm_pos_ids=vpm_pos_ids["grid"],
                            )
                            per_strategy_pnl["grid"] += _dir_pnl
                            strat_trades["grid"] += len(_dir_forced)

            # 3. Per-strategy signal generation and execution (ALL strategies, always)
            # Mirrors live BotOrchestrator: every strategy runs every bar,
            # router only adjusts position size via weight.
            balance = simulator.get_portfolio_value()

            # VPM aggregate exit scan (fires Grid aggregate SL, etc.)
            if vpm is not None:
                vpm_bar_exits: list[tuple[VirtualPosition, str]] = await vpm.check_exits(
                    current_price
                )
            else:
                vpm_bar_exits = []

            for strat_name, strategy in strategies.items():
                weight = regime_weights.get(strat_name, 1.0)

                # Count this bar as "active" when router allows this strategy
                is_active = weight > 0.0
                if is_active:
                    strat_bars_active[strat_name] += 1
                    strat_equity[strat_name].append(float(balance))

                # analyze_market — always, at per-strategy intervals:
                # SMC: every 60 bars (300 sec, mirrors live 5-min throttle)
                # Grid/DCA/TF: every bar (price-reactive, lightweight)
                _n = (
                    config.smc_analyze_every_n
                    if strat_name == "smc"
                    else config.default_analyze_every_n
                )
                if _n == 0 or bars_since_warmup % _n == 0:
                    try:
                        strategy.analyze_market(df_d1, df_h4, df_h1, df_m15, df_m5)
                    except Exception as e:
                        logger.debug("analyze_market error %s bar %d: %s", strat_name, i, e)

                # generate_signal — skip entirely if router deactivated this strategy
                # (weight=0.0 mirrors live HybridCoordinator suspending inactive strategy)
                if weight == 0.0:
                    signal = None
                else:
                    # CapitalArbiter gate — mirrors live BotOrchestrator
                    if capital_arbiter is not None and current_regime is not None:
                        _norm = strat_name.replace("trend_follower", "tf")
                        _allowed = capital_arbiter.get_allowed_capital(
                            _norm, current_regime.regime, balance
                        )
                        if _allowed <= Decimal("0"):
                            signal_stats[strat_name]["blocked_by_arbiter"] += 1
                            signal = None
                            # Skip to update_positions below (fall through with signal=None)
                        else:
                            signal = None  # will be set by generate_signal below
                            _gen_n = (
                                config.smc_generate_signal_every_n if strat_name == "smc" else 1
                            )
                            if _gen_n <= 1 or bars_since_warmup % _gen_n == 0:
                                try:
                                    signal = strategy.generate_signal(df_m5, balance)
                                except Exception as e:
                                    logger.debug(
                                        "generate_signal error %s bar %d: %s", strat_name, i, e
                                    )
                                    signal = None
                    else:
                        _gen_n = config.smc_generate_signal_every_n if strat_name == "smc" else 1
                        if _gen_n <= 1 or bars_since_warmup % _gen_n == 0:
                            try:
                                signal = strategy.generate_signal(df_m5, balance)
                            except Exception as e:
                                logger.debug(
                                    "generate_signal error %s bar %d: %s", strat_name, i, e
                                )
                                signal = None
                        else:
                            signal = None

                if signal is not None:
                    signal_stats[strat_name]["signals_generated"] += 1
                    await self._handle_signal(
                        strat_name=strat_name,
                        strategy=strategy,
                        signal=signal,
                        current_price=current_price,
                        simulator=simulator,
                        position_amounts=position_amounts[strat_name],
                        position_directions=position_directions[strat_name],
                        position_entry_prices=position_entry_prices[strat_name],
                        risk_manager=risk_manager,
                        config=config,
                        position_weight=weight,
                        signal_stats=signal_stats[strat_name],
                        vpm=vpm,
                        vpm_pos_ids=vpm_pos_ids[strat_name],
                    )

                # 4. update_positions — always (each strategy manages its own positions)
                try:
                    exits = strategy.update_positions(current_price, df_m5)
                except Exception as e:
                    logger.debug("update_positions error %s bar %d: %s", strat_name, i, e)
                    exits = []

                # Check if VPM fired an exit for this strategy
                if vpm_bar_exits:
                    _norm_strat = strat_name.replace("trend_follower", "tf")
                    for vpos, _reason in vpm_bar_exits:
                        vpos_strat = getattr(vpos, "strategy", None)
                        if vpos_strat == _norm_strat:
                            strategy_pos_id = (getattr(vpos, "meta", None) or {}).get("pos_id")
                            if strategy_pos_id and strategy_pos_id in position_amounts.get(
                                strat_name, {}
                            ):
                                if not any(p == strategy_pos_id for p, _ in (exits or [])):
                                    exits = list(exits) if exits else []
                                    exits.append((strategy_pos_id, ExitReason.STOP_LOSS))

                if exits:
                    # Only count exits where the position was actually tracked
                    # (pos_id in position_amounts). Phantom exits from stuck
                    # positions (where close_position silently failed) have
                    # amount=None and should not be counted as completed trades.
                    actual_exit_count = sum(
                        1 for pos_id, _ in exits if pos_id in position_amounts[strat_name]
                    )
                    pnl_delta = await self._handle_exits(
                        strat_name=strat_name,
                        strategy=strategy,
                        exits=exits,
                        current_price=current_price,
                        simulator=simulator,
                        position_amounts=position_amounts[strat_name],
                        position_directions=position_directions[strat_name],
                        position_entry_prices=position_entry_prices[strat_name],
                        vpm=vpm,
                        vpm_pos_ids=vpm_pos_ids[strat_name],
                    )
                    per_strategy_pnl[strat_name] += pnl_delta
                    # Count only actually-processed exits (not phantom repeats)
                    strat_trades[strat_name] += actual_exit_count
                    # Record individual trade PnLs for win_rate
                    if len(exits) > 0:
                        per_trade = pnl_delta / len(exits)
                        strat_trade_pnls[strat_name].extend([float(per_trade)] * len(exits))

            # 4b. Recovery handling — check if Grid triggered recovery
            if recovery_coordinator is not None and "grid" in strategies:
                from bot.strategies.grid_adapter import GridAdapter as _GA

                _grid = strategies["grid"]
                if isinstance(_grid, _GA) and _grid.recovery_triggered:
                    _grid.clear_recovery_trigger()
                    # Enter recovery: snapshot underwater Grid positions
                    _snap = _grid.get_underwater_snapshot()
                    _underwater = [
                        UnderwaterPosition(
                            pos_id=p["pos_id"],
                            entry_price=p["entry_price"],
                            size=p["size"],
                        )
                        for p in _snap
                    ]
                    # Try to find SMC support/resistance from analyzer
                    _recovery_dir = _grid.direction
                    _smc_support = None
                    if smc_analyzer is not None:
                        try:
                            _ctx = smc_analyzer.context
                            if _recovery_dir == SignalDirection.SHORT:
                                _smc_support = RecoveryCoordinator.find_smc_resistance(
                                    _ctx, current_price
                                )
                            else:
                                _smc_support = RecoveryCoordinator.find_smc_support(
                                    _ctx, current_price
                                )
                        except Exception:
                            pass
                    _base_size = _grid._amount_per_grid
                    recovery_coordinator.enter_recovery(
                        grid_positions=_underwater,
                        current_price=current_price,
                        current_bar=i,
                        smc_support=_smc_support,
                        base_order_size=_base_size,
                        direction=_recovery_dir,
                    )
                elif recovery_coordinator.is_active:
                    # Update recovery state — DCA signals are auto-tracked
                    # by RecoveryCoordinator via price levels.
                    # First collect fills from signals that trigger this bar.
                    recovery_action = recovery_coordinator.on_price_update(current_price)
                    _dca_fills: list[UnderwaterPosition] = []
                    # Process DCA signals from recovery
                    for _rsig in recovery_action.dca_signals:
                        await self._handle_signal(
                            strat_name="grid",
                            strategy=_grid,
                            signal=_rsig,
                            current_price=current_price,
                            simulator=simulator,
                            position_amounts=position_amounts["grid"],
                            position_directions=position_directions["grid"],
                            position_entry_prices=position_entry_prices["grid"],
                            risk_manager=risk_manager,
                            config=config,
                            position_weight=1.0,
                            signal_stats=signal_stats["grid"],
                            vpm=vpm,
                            vpm_pos_ids=vpm_pos_ids["grid"],
                        )
                        # Track the fill for blended avg recalculation
                        _mult = Decimal(
                            str(_rsig.metadata.get("multiplier", 1.0))
                        ) if _rsig.metadata else Decimal("1")
                        _dca_fills.append(
                            UnderwaterPosition(
                                pos_id=f"rdca_{i}_{len(_dca_fills)}",
                                entry_price=current_price,
                                size=_mult * _grid._amount_per_grid,
                            )
                        )
                    # Register DCA fills for blended avg tracking
                    if _dca_fills:
                        recovery_coordinator.on_price_update(
                            current_price, new_fills=_dca_fills
                        )

                    if recovery_action.should_close_all:
                        # Close all Grid positions
                        _all_grid_pos = list(position_amounts["grid"].keys())
                        if _all_grid_pos:
                            _force_exits = [
                                (pid, ExitReason.MANUAL) for pid in _all_grid_pos
                            ]
                            _pnl = await self._handle_exits(
                                strat_name="grid",
                                strategy=_grid,
                                exits=_force_exits,
                                current_price=current_price,
                                simulator=simulator,
                                position_amounts=position_amounts["grid"],
                                position_directions=position_directions["grid"],
                                position_entry_prices=position_entry_prices["grid"],
                                vpm=vpm,
                                vpm_pos_ids=vpm_pos_ids["grid"],
                            )
                            per_strategy_pnl["grid"] += _pnl
                            strat_trades["grid"] += len(_all_grid_pos)
                        # Resume Grid with new range
                        if recovery_action.new_grid_range:
                            _new_lo, _new_hi = recovery_action.new_grid_range
                            _grid.resume(_new_lo, _new_hi)

            # 5. Record equity
            # simulator.get_portfolio_value() = quote + base * current_price
            # This correctly accounts for open positions (cost was deducted from quote,
            # base coins are now worth current_price). DD > 100% artifacts are resolved
            # by parallel strategy execution (no orphaned cross-strategy positions).
            portfolio_value = simulator.get_portfolio_value()
            ec_entry: dict[str, Any] = {
                "timestamp": base_df.index[i].isoformat(),
                "price": float(current_price),
                "portfolio_value": float(portfolio_value),
                "active_strategies": sorted(strategies.keys()),
            }
            if current_regime:
                ec_entry["regime"] = current_regime.regime.value
            equity_curve.append(ec_entry)

            # Update drawdown
            if portfolio_value > peak_value:
                peak_value = portfolio_value
            else:
                dd = peak_value - portfolio_value
                if dd > max_drawdown:
                    max_drawdown = dd

            # 6. Portfolio risk manager balance update
            if risk_manager:
                risk_manager.update_balance(portfolio_value)
                if bars_since_warmup > 0 and bars_since_warmup % 288 == 0:
                    risk_manager.reset_daily_loss()
                # In backtesting, do not break on RM halt — let existing
                # positions complete their TP/SL exits. New entries are
                # still blocked by check_trade() returning False.

            await asyncio.sleep(0)

        # Build result
        result = self._build_result(
            config=config,
            strategies=strategies,
            simulator=simulator,
            equity_curve=equity_curve,
            max_drawdown=max_drawdown,
            start_time=(
                base_df.index[config.warmup_bars].to_pydatetime()
                if len(base_df) > config.warmup_bars
                else base_df.index[0].to_pydatetime()
            ),
            end_time=base_df.index[-1].to_pydatetime(),
            per_strategy_pnl=per_strategy_pnl,
            regime_routing_stats=regime_routing_stats,
            strategy_switches=router.switch_history,
            cooldown_events=cooldown_events,
            risk_manager=risk_manager,
            strat_bars_active=strat_bars_active,
            strat_trades=strat_trades,
            strat_equity=strat_equity,
            strat_trade_pnls=strat_trade_pnls,
            signal_stats=signal_stats,
        )
        return result

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    async def _handle_signal(
        self,
        strat_name: str,
        strategy: BaseStrategy,
        signal: Any,
        current_price: Decimal,
        simulator: MarketSimulator,
        position_amounts: dict[str, Decimal],
        position_directions: dict[str, SignalDirection],
        position_entry_prices: dict[str, Decimal],
        risk_manager: RiskManager | None,
        config: OrchestratorBacktestConfig,
        position_weight: float = 1.0,
        signal_stats: dict[str, int] | None = None,
        vpm: VirtualPositionManager | None = None,
        vpm_pos_ids: dict[str, str] | None = None,
    ) -> None:
        """Open a position if signal passes risk checks."""
        balance = simulator.get_portfolio_value()
        # Grid uses its own per-level sizing (max_position_size_pct / num_levels) to allow
        # all 20 levels to fit within the aggregate cap.  Other strategies use max_position_pct.
        _per_pos_pct = (
            config.grid_position_pct
            if strat_name == "grid" and config.grid_position_pct is not None
            else config.max_position_pct
        )
        # position_weight: 1.0 = full size (router-preferred), 0.5 = reduced (advisory)
        position_value = balance * _per_pos_pct * Decimal(str(position_weight))
        position_size = position_value / current_price if current_price > 0 else Decimal("0")

        # Check if we can afford it
        cost = position_size * current_price
        if cost > simulator.balance.quote or position_size <= 0:
            if signal_stats is not None:
                signal_stats["blocked_by_balance"] += 1
            return

        # Risk manager gate
        if risk_manager:
            current_pos_val = sum(amt * current_price for amt in position_amounts.values())
            if not risk_manager.check_trade(
                order_value=cost,
                current_position=current_pos_val,
                available_balance=simulator.balance.quote,
            ):
                if signal_stats is not None:
                    signal_stats["blocked_by_risk_mgr"] += 1
                return

        try:
            pos_id = strategy.open_position(signal, cost)
            side = "buy" if signal.direction == SignalDirection.LONG else "sell"
            await simulator.create_order(
                symbol=config.symbol,
                order_type="market",
                side=side,
                amount=position_size,
            )
            position_amounts[pos_id] = position_size
            position_directions[pos_id] = signal.direction
            position_entry_prices[pos_id] = current_price  # for real PnL calculation
            if signal_stats is not None:
                signal_stats["executed"] += 1

            # Register in VirtualPositionManager
            if vpm is not None and vpm_pos_ids is not None:
                import uuid as _uuid

                vpm_id = _uuid.uuid4().hex[:8]
                vpos = VirtualPosition(
                    pos_id=vpm_id,
                    strategy=strat_name.replace("trend_follower", "tf"),
                    symbol=config.symbol,
                    side="long" if signal.direction == SignalDirection.LONG else "short",
                    qty=position_size,
                    entry_price=current_price,
                    tp_price=(
                        signal.take_profit
                        if getattr(signal, "take_profit", None)
                        and signal.take_profit > Decimal("0")
                        else None
                    ),
                    sl_price=(
                        signal.stop_loss
                        if getattr(signal, "stop_loss", None)
                        and signal.stop_loss > Decimal("0")
                        else None
                    ),
                    meta={"pos_id": pos_id},
                )
                await vpm.open(vpos)
                vpm_pos_ids[pos_id] = vpm_id
        except Exception as e:
            logger.debug("Signal execution failed for %s: %s", strat_name, e)
            if signal_stats is not None:
                signal_stats["blocked_by_error"] += 1

    async def _handle_exits(
        self,
        strat_name: str,
        strategy: BaseStrategy,
        exits: list[tuple[str, ExitReason]],
        current_price: Decimal,
        simulator: MarketSimulator,
        position_amounts: dict[str, Decimal],
        position_directions: dict[str, SignalDirection],
        position_entry_prices: dict[str, Decimal],
        vpm: VirtualPositionManager | None = None,
        vpm_pos_ids: dict[str, str] | None = None,
    ) -> Decimal:
        """Close positions and return real P&L delta: (exit - entry) × amount."""
        pnl_delta = Decimal("0")
        # Always use the simulator's own symbol — strategies may store symbol
        # under different attribute names (_symbol, symbol, etc.) causing
        # a fallback to "BTC/USDT" which would be rejected by the simulator.
        trade_symbol = simulator.symbol
        for pos_id, exit_reason in exits:
            amount = position_amounts.pop(pos_id, None)
            direction = position_directions.pop(pos_id, SignalDirection.LONG)
            entry_price = position_entry_prices.pop(pos_id, current_price)

            # Always close VPM position when an exit is triggered — even when
            # the strategy-level position is already gone (amount=None).  Not
            # closing the VPM position here leaves it open so VPM keeps firing
            # the same SL exit on every subsequent bar, inflating strat_trades
            # by ~N_remaining_bars and preventing the VPM slot from being freed.
            if vpm is not None and vpm_pos_ids is not None:
                vpm_id = vpm_pos_ids.pop(pos_id, None)
                if vpm_id:
                    try:
                        await vpm.close(vpm_id, current_price, exit_reason.value)
                    except Exception:
                        pass

            if amount is None:
                continue
            try:
                strategy.close_position(pos_id, exit_reason, current_price)
                if direction == SignalDirection.LONG:
                    sell_amount = min(amount, simulator.balance.base)
                    if sell_amount > Decimal("0"):
                        await simulator.create_order(
                            symbol=trade_symbol,
                            order_type="market",
                            side="sell",
                            amount=sell_amount,
                        )
                    pnl_delta += (current_price - entry_price) * amount
                else:
                    await simulator.create_order(
                        symbol=trade_symbol,
                        order_type="market",
                        side="buy",
                        amount=amount,
                    )
                    pnl_delta += (entry_price - current_price) * amount
            except Exception as e:
                logger.warning("Exit failed for %s pos %s: %s", strat_name, pos_id, e)
        return pnl_delta

    # ------------------------------------------------------------------
    # DCA warmup catch-up (P0.5)
    # ------------------------------------------------------------------

    async def _run_dca_warmup_catchup(
        self,
        dca_strategy: Any,
        base_df: Any,
        warmup_bars: int,
        config: OrchestratorBacktestConfig,
        simulator: MarketSimulator,
        position_amounts: dict[str, Decimal],
        position_directions: dict[str, SignalDirection],
        position_entry_prices: dict[str, Decimal],
    ) -> None:
        """Initialize DCA strategy state from warmup data (mirrors live DCAStartupAnalyzer).

        Step 1 — always: set ``_recent_high`` from the last 500 warmup bars so the
        DCA adapter can trigger on bar 0 if price has already dropped enough.

        Step 2 — catch-up orders: if price at warmup end is below the trigger
        threshold from the recent high, pre-open DCA positions that would have
        been placed during the warmup period.
        """

        warmup_df = base_df.iloc[:warmup_bars]
        if warmup_df.empty:
            return

        # Reference window — last 500 M5 bars (~41 hours) of warmup, same as live
        lookback = min(500, len(warmup_df))
        window = warmup_df.iloc[-lookback:]
        recent_high = Decimal(str(window["high"].max()))
        warmup_end_price = Decimal(str(base_df.iloc[warmup_bars - 1]["close"]))

        # Step 1: set _recent_high so DCA can trigger naturally from bar 0
        if hasattr(dca_strategy, "_recent_high"):
            dca_strategy._recent_high = recent_high

        # Step 2: simulate catch-up orders using DCAStartupAnalyzer
        try:
            from datetime import timezone

            from bot.strategies.base import BaseSignal
            from bot.strategies.base import SignalDirection as _SD
            from bot.strategies.dca.startup_analyzer import DCAStartupAnalyzer

            if not (
                hasattr(dca_strategy, "_price_deviation_pct")
                and hasattr(dca_strategy, "_safety_order_size")
                and hasattr(dca_strategy, "_max_safety_orders")
            ):
                return

            # Build OHLCV list expected by DCAStartupAnalyzer [[ts, o, h, l, c, v], ...]
            ohlcv_rows = [
                [
                    int(ts.timestamp() * 1000),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row.get("volume", 0)),
                ]
                for ts, row in window.iterrows()
            ]

            analyzer = DCAStartupAnalyzer(
                trigger_pct=dca_strategy._price_deviation_pct,
                amount_per_step=dca_strategy._safety_order_size,
                max_steps=dca_strategy._max_safety_orders,
                catch_up_max_orders=dca_strategy._max_safety_orders,
                catch_up_reference="last_high",
                catch_up_lookback_bars=lookback,
            )
            plan = analyzer.analyze(
                ohlcv=ohlcv_rows,
                current_price=warmup_end_price,
                open_orders=[],
            )

            if not plan.orders_to_place:
                return

            await simulator.set_price(warmup_end_price)
            for level in plan.orders_to_place:
                cost = level.amount_usd
                if cost > simulator.balance.quote or cost <= Decimal("0"):
                    continue
                amount = cost / level.price
                take_profit = level.price * (
                    Decimal("1") + getattr(dca_strategy, "_take_profit_pct", Decimal("0.08"))
                )
                catchup_signal = BaseSignal(
                    direction=_SD.LONG,
                    entry_price=level.price,
                    stop_loss=level.price * Decimal("0.88"),
                    take_profit=take_profit,
                    confidence=0.7,
                    timestamp=datetime.now(timezone.utc),
                    strategy_type="dca",
                    signal_reason="dca_warmup_catchup",
                )
                try:
                    pos_id = dca_strategy.open_position(catchup_signal, cost)
                    await simulator.create_order(
                        symbol=config.symbol,
                        order_type="market",
                        side="buy",
                        amount=amount,
                    )
                    position_amounts[pos_id] = amount
                    position_directions[pos_id] = _SD.LONG
                    position_entry_prices[pos_id] = level.price
                    logger.debug(
                        "DCA warmup catchup: level %d @ %.4f, cost=%.2f",
                        level.level_num,
                        float(level.price),
                        float(cost),
                    )
                except Exception as e:
                    logger.debug("DCA warmup catchup order failed: %s", e)

        except ImportError:
            pass
        except Exception as e:
            logger.debug("DCA warmup catch-up failed: %s", e)

    # ------------------------------------------------------------------
    # Strategy construction
    # ------------------------------------------------------------------

    def _build_strategies(self, config: OrchestratorBacktestConfig) -> dict[str, BaseStrategy]:
        """Build strategy instances from registered factories."""
        strategies: dict[str, BaseStrategy] = {}

        # Map strategy names to (enabled, params_key)
        desired = {
            "grid": (config.enable_grid, config.grid_params),
            "dca": (config.enable_dca, config.dca_params),
            "trend_follower": (config.enable_trend_follower, config.tf_params),
            "smc": (config.enable_smc, config.smc_params),
        }

        for name, (enabled, params) in desired.items():
            if not enabled:
                continue
            factory = self._strategy_factories.get(name)
            if factory is None:
                logger.debug("No factory for strategy '%s', skipping", name)
                continue
            try:
                strategies[name] = factory(params)
            except Exception as e:
                logger.warning("Failed to build strategy '%s': %s", name, e)

        return strategies

    # ------------------------------------------------------------------
    # Result construction
    # ------------------------------------------------------------------

    def _build_result(
        self,
        config: OrchestratorBacktestConfig,
        strategies: dict[str, BaseStrategy],
        simulator: MarketSimulator,
        equity_curve: list[dict[str, Any]],
        max_drawdown: Decimal,
        start_time: datetime,
        end_time: datetime,
        per_strategy_pnl: dict[str, Decimal],
        regime_routing_stats: dict[str, int],
        strategy_switches: list[dict[str, Any]],
        cooldown_events: int,
        risk_manager: RiskManager | None,
        strat_bars_active: dict[str, int] | None = None,
        strat_trades: dict[str, int] | None = None,
        strat_equity: dict[str, list[float]] | None = None,
        strat_trade_pnls: dict[str, list[float]] | None = None,
        signal_stats: dict[str, dict[str, int]] | None = None,
    ) -> OrchestratorBacktestResult:
        """Assemble OrchestratorBacktestResult from simulation state."""
        from datetime import timedelta

        trade_history = simulator.get_trade_history()
        final_balance = simulator.get_portfolio_value()
        initial = config.initial_balance
        total_return = final_balance - initial
        total_return_pct = (
            (total_return / initial) * Decimal("100") if initial > 0 else Decimal("0")
        )
        max_dd_pct = (max_drawdown / initial) * Decimal("100") if initial > 0 else Decimal("0")

        buy_orders = [t for t in trade_history if t["side"] == "buy"]
        sell_orders = [t for t in trade_history if t["side"] == "sell"]
        winning_trades = losing_trades = 0
        gross_profit = gross_loss = Decimal("0")

        for b, s in zip(buy_orders, sell_orders, strict=False):
            bp = Decimal(str(b["price"]))
            sp = Decimal(str(s["price"]))
            amt = Decimal(str(b["amount"]))
            profit = (sp - bp) * amt
            if profit > 0:
                winning_trades += 1
                gross_profit += profit
            else:
                losing_trades += 1
                gross_loss += abs(profit)

        total_trades = winning_trades + losing_trades
        win_rate = (
            Decimal(winning_trades) / Decimal(total_trades) * Decimal("100")
            if total_trades > 0
            else Decimal("0")
        )
        avg_profit = (
            (gross_profit - gross_loss) / Decimal(total_trades)
            if total_trades > 0
            else Decimal("0")
        )
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
        sharpe = self._calculate_sharpe(equity_curve)

        duration = end_time - start_time if end_time > start_time else timedelta(0)

        # Build per-strategy detailed metrics
        per_strategy_metrics: dict[str, StrategyPeriodMetrics] = {}
        for name in strategies:
            bars = (strat_bars_active or {}).get(name, 0)
            trades = (strat_trades or {}).get(name, 0)
            pnl = float(per_strategy_pnl.get(name, Decimal("0")))
            eq_series = (strat_equity or {}).get(name, [])
            strat_sharpe = self._calculate_sharpe_from_values(eq_series)
            strat_dd_pct = self._calculate_max_drawdown_pct(eq_series)
            trade_pnls = (strat_trade_pnls or {}).get(name, [])
            strat_win_rate = self._calculate_win_rate_from_trades(trade_pnls, trades)
            per_strategy_metrics[name] = StrategyPeriodMetrics(
                bars_active=bars,
                trades=trades,
                realized_pnl=pnl,
                sharpe=strat_sharpe,
                max_drawdown_pct=strat_dd_pct,
                win_rate=strat_win_rate,
            )

        result = OrchestratorBacktestResult(
            strategy_name="orchestrator_v2",
            symbol=config.symbol,
            start_time=start_time,
            end_time=end_time,
            duration=duration,
            initial_balance=initial,
            final_balance=final_balance,
            total_return=total_return,
            total_return_pct=total_return_pct,
            max_drawdown=max_drawdown,
            max_drawdown_pct=max_dd_pct,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            total_buy_orders=len(buy_orders),
            total_sell_orders=len(sell_orders),
            avg_profit_per_trade=avg_profit,
            sharpe_ratio=sharpe,
            profit_factor=profit_factor,
            trade_history=trade_history,
            equity_curve=equity_curve,
            # V2.0 extensions
            strategy_switches=strategy_switches,
            per_strategy_pnl={k: float(v) for k, v in per_strategy_pnl.items()},
            regime_routing_stats=regime_routing_stats,
            cooldown_events=cooldown_events,
            per_strategy_metrics=per_strategy_metrics,
            signal_stats=signal_stats or {},
        )

        if risk_manager:
            result.risk_halted = risk_manager.is_halted
            result.risk_halt_reason = risk_manager.halt_reason

        return result

    @staticmethod
    def _calculate_sharpe(equity_curve: list[dict[str, Any]]) -> Decimal | None:
        """Annualised Sharpe ratio from M5 equity curve."""
        if len(equity_curve) < 2:
            return None
        returns = []
        for i in range(1, len(equity_curve)):
            prev = Decimal(str(equity_curve[i - 1]["portfolio_value"]))
            curr = Decimal(str(equity_curve[i]["portfolio_value"]))
            if prev > 0:
                returns.append((curr - prev) / prev)
        if not returns:
            return None
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        std_r = variance.sqrt() if variance > 0 else Decimal("0")
        if std_r > 0:
            return (mean_r / std_r) * Decimal(str((365 * 24 * 12) ** 0.5))
        return None

    @staticmethod
    def _calculate_sharpe_from_values(values: list[float]) -> float | None:
        """Annualised Sharpe ratio from a list of equity values (M5 frequency)."""
        if len(values) < 2:
            return None
        returns = []
        for i in range(1, len(values)):
            prev = values[i - 1]
            curr = values[i]
            if prev > 0:
                returns.append((curr - prev) / prev)
        if not returns:
            return None
        n = len(returns)
        mean_r = sum(returns) / n
        variance = sum((r - mean_r) ** 2 for r in returns) / n
        std_r = variance**0.5
        if std_r > 0:
            return (mean_r / std_r) * ((365 * 24 * 12) ** 0.5)
        return None

    @staticmethod
    def _calculate_max_drawdown_pct(values: list[float]) -> float:
        """Maximum drawdown percentage from a list of equity values."""
        if len(values) < 2:
            return 0.0
        peak = values[0]
        max_dd = 0.0
        for v in values[1:]:
            if v > peak:
                peak = v
            elif peak > 0:
                dd = (peak - v) / peak * 100.0
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    @staticmethod
    def _calculate_win_rate_from_trades(trade_pnls: list[float], trades: int) -> float:
        """Per-trade win rate: percentage of trades with positive PnL.

        Uses individual trade PnL records for accurate calculation instead of
        the previous binary heuristic (0% or 100% based on aggregate PnL).
        """
        if trades == 0 or not trade_pnls:
            return 0.0
        winning = sum(1 for pnl in trade_pnls if pnl > 0)
        return (winning / len(trade_pnls)) * 100.0
