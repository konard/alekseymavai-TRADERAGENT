"""
BacktestOrchestratorEngine V2.0 — mirrors BotOrchestrator._main_loop() on historical data.

Key differences from MultiTimeframeBacktestEngine (V1):
- Runs multiple strategy engines simultaneously (Grid + DCA + TrendFollower)
- Routes signals through StrategyRouter based on market regime
- Enforces cooldown between strategy switches
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
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from bot.core.risk_manager import RiskManager
from bot.orchestrator.market_regime import (
    MarketRegimeDetector,
    RegimeAnalysis,
)
from bot.strategies.base import BaseStrategy, ExitReason, SignalDirection
from bot.tests.backtesting.backtesting_engine import BacktestResult
from bot.tests.backtesting.market_simulator import MarketSimulator
from bot.tests.backtesting.multi_tf_data_loader import (
    MultiTimeframeData,
    MultiTimeframeDataLoader,
)
from bot.tests.backtesting.strategy_router import StrategyRouter

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorBacktestConfig:
    """Configuration for the V2.0 orchestrator backtest engine."""

    symbol: str = "BTC/USDT"
    initial_balance: Decimal = Decimal("10000")
    lookback: int = 100
    warmup_bars: int = 14400
    analyze_every_n: int = 4

    # Strategies to include
    enable_grid: bool = True
    enable_dca: bool = True
    enable_trend_follower: bool = True
    enable_smc: bool = False

    # Regime-based routing (key differentiator from V1)
    enable_strategy_router: bool = True
    router_cooldown_bars: int = 60
    regime_check_every_n: int = 12    # 12 M5 bars = 1 hour

    # Per-strategy parameters (passed to strategy factories)
    grid_params: dict[str, Any] = field(default_factory=dict)
    dca_params: dict[str, Any] = field(default_factory=dict)
    tf_params: dict[str, Any] = field(default_factory=dict)
    smc_params: dict[str, Any] = field(default_factory=dict)

    # Risk management — aligned with TradingCoreConfig defaults
    enable_risk_manager: bool = True
    max_position_size_pct: float = 0.25   # 25% per trade (TradingCoreConfig default)
    max_daily_loss_pct: float = 0.05      # 5% daily loss cap (was 25% — now matches bot)
    portfolio_stop_loss_pct: float = 0.15

    # Position sizing (fraction of balance per signal)
    risk_per_trade: Decimal = Decimal("0.02")
    max_position_pct: Decimal = Decimal("0.25")

    # Exchange fee simulation — aligned with TradingCoreConfig defaults (Bybit VIP0)
    maker_fee: Decimal = Decimal("0.0002")    # 0.02 % (was 0.1 % in old MarketSimulator default)
    taker_fee: Decimal = Decimal("0.00055")   # 0.055 %
    slippage: Decimal = Decimal("0.0003")     # 0.03 % average slippage


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

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base["orchestrator"] = {
            "strategy_switches": len(self.strategy_switches),
            "per_strategy_pnl": self.per_strategy_pnl,
            "regime_routing_stats": self.regime_routing_stats,
            "cooldown_events": self.cooldown_events,
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

        # Strategy router
        router = StrategyRouter(
            cooldown_bars=config.router_cooldown_bars,
            enable_smc=config.enable_smc,
            enable_trend_follower=config.enable_trend_follower,
        )

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

        # Per-strategy state tracking
        position_amounts: dict[str, dict[str, Decimal]] = {name: {} for name in strategies}
        position_directions: dict[str, dict[str, SignalDirection]] = {name: {} for name in strategies}
        per_strategy_pnl: dict[str, Decimal] = {name: Decimal("0") for name in strategies}
        regime_routing_stats: dict[str, int] = {}
        cooldown_events = 0
        current_regime: RegimeAnalysis | None = None

        # Execution loop
        equity_curve: list[dict[str, Any]] = []
        peak_value = config.initial_balance
        max_drawdown = Decimal("0")
        base_df = data.m5
        total_bars = len(base_df)

        for i in range(config.warmup_bars, total_bars):
            df_d1, df_h4, df_h1, df_m15, df_m5 = self.data_loader.get_context_at(
                data, base_index=i, lookback=config.lookback
            )
            current_price = Decimal(str(base_df.iloc[i]["close"]))
            await simulator.set_price(current_price)

            bars_since_warmup = i - config.warmup_bars

            # 1. Regime detection
            if bars_since_warmup % config.regime_check_every_n == 0 and len(df_h1) >= 60:
                current_regime = regime_detector.analyze(df_h1)
                regime_key = current_regime.regime.value
                regime_routing_stats[regime_key] = regime_routing_stats.get(regime_key, 0) + 1

            # 2. Strategy routing
            if config.enable_strategy_router:
                router_event = router.on_bar(current_regime, i)
                active_set = router_event.active_strategies
                if router_event.cooldown_remaining > 0:
                    cooldown_events += 1
            else:
                # No routing — all enabled strategies are always active
                active_set = set(strategies.keys())

            # Filter to only strategies we have built
            active_set = active_set & set(strategies.keys())

            # 3. Per-strategy signal generation and execution
            balance = simulator.get_portfolio_value()

            for strat_name, strategy in strategies.items():
                is_active = strat_name in active_set
                has_open_positions = bool(position_amounts[strat_name])

                # Always process exits for strategies with open positions,
                # even when deactivated — orphaned positions must close via TP/SL.
                # Only skip entirely if inactive AND no open positions.
                if not is_active and not has_open_positions:
                    continue

                if is_active:
                    # Periodically analyze market
                    if bars_since_warmup % config.analyze_every_n == 0:
                        try:
                            strategy.analyze_market(df_d1, df_h4, df_h1, df_m15, df_m5)
                        except Exception as e:
                            logger.debug("analyze_market error %s bar %d: %s", strat_name, i, e)

                    # Generate signal (only when strategy is active)
                    try:
                        signal = strategy.generate_signal(df_m5, balance)
                    except Exception as e:
                        logger.debug("generate_signal error %s bar %d: %s", strat_name, i, e)
                        signal = None

                    if signal is not None:
                        await self._handle_signal(
                            strat_name=strat_name,
                            strategy=strategy,
                            signal=signal,
                            current_price=current_price,
                            simulator=simulator,
                            position_amounts=position_amounts[strat_name],
                            position_directions=position_directions[strat_name],
                            risk_manager=risk_manager,
                            config=config,
                        )

                # 4. Update positions and handle exits (always, if open positions exist)
                try:
                    exits = strategy.update_positions(current_price, df_m5)
                except Exception as e:
                    logger.debug("update_positions error %s bar %d: %s", strat_name, i, e)
                    exits = []

                if exits:
                    pnl_delta = await self._handle_exits(
                        strat_name=strat_name,
                        strategy=strategy,
                        exits=exits,
                        current_price=current_price,
                        simulator=simulator,
                        position_amounts=position_amounts[strat_name],
                        position_directions=position_directions[strat_name],
                    )
                    per_strategy_pnl[strat_name] += pnl_delta

            # 5. Record equity
            portfolio_value = simulator.get_portfolio_value()
            ec_entry: dict[str, Any] = {
                "timestamp": base_df.index[i].isoformat(),
                "price": float(current_price),
                "portfolio_value": float(portfolio_value),
                "active_strategies": sorted(active_set),
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
            start_time=base_df.index[config.warmup_bars].to_pydatetime()
            if len(base_df) > config.warmup_bars
            else base_df.index[0].to_pydatetime(),
            end_time=base_df.index[-1].to_pydatetime(),
            per_strategy_pnl=per_strategy_pnl,
            regime_routing_stats=regime_routing_stats,
            strategy_switches=router.switch_history,
            cooldown_events=cooldown_events,
            risk_manager=risk_manager,
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
        risk_manager: RiskManager | None,
        config: OrchestratorBacktestConfig,
    ) -> None:
        """Open a position if signal passes risk checks."""
        balance = simulator.get_portfolio_value()
        position_value = balance * config.max_position_pct
        position_size = position_value / current_price if current_price > 0 else Decimal("0")

        # Check if we can afford it
        cost = position_size * current_price
        if cost > simulator.balance.quote or position_size <= 0:
            return

        # Risk manager gate
        if risk_manager:
            current_pos_val = sum(
                amt * current_price for amt in position_amounts.values()
            )
            if not risk_manager.check_trade(
                order_value=cost,
                current_position=current_pos_val,
                available_balance=simulator.balance.quote,
            ):
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
        except Exception as e:
            logger.debug("Signal execution failed for %s: %s", strat_name, e)

    async def _handle_exits(
        self,
        strat_name: str,
        strategy: BaseStrategy,
        exits: list[tuple[str, ExitReason]],
        current_price: Decimal,
        simulator: MarketSimulator,
        position_amounts: dict[str, Decimal],
        position_directions: dict[str, SignalDirection],
    ) -> Decimal:
        """Close positions and return approximate P&L delta."""
        pnl_delta = Decimal("0")
        # Always use the simulator's own symbol — strategies may store symbol
        # under different attribute names (_symbol, symbol, etc.) causing
        # a fallback to "BTC/USDT" which would be rejected by the simulator.
        trade_symbol = simulator.symbol
        for pos_id, exit_reason in exits:
            amount = position_amounts.pop(pos_id, None)
            direction = position_directions.pop(pos_id, SignalDirection.LONG)
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
                else:
                    await simulator.create_order(
                        symbol=trade_symbol,
                        order_type="market",
                        side="buy",
                        amount=amount,
                    )
                # Approximate P&L
                pnl_delta += amount * current_price * Decimal("0.001")
            except Exception as e:
                logger.debug("Exit failed for %s pos %s: %s", strat_name, pos_id, e)
        return pnl_delta

    # ------------------------------------------------------------------
    # Strategy construction
    # ------------------------------------------------------------------

    def _build_strategies(
        self, config: OrchestratorBacktestConfig
    ) -> dict[str, BaseStrategy]:
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
        max_dd_pct = (
            (max_drawdown / initial) * Decimal("100") if initial > 0 else Decimal("0")
        )

        buy_orders = [t for t in trade_history if t["side"] == "buy"]
        sell_orders = [t for t in trade_history if t["side"] == "sell"]
        winning_trades = losing_trades = 0
        gross_profit = gross_loss = Decimal("0")

        for b, s in zip(buy_orders, sell_orders):
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
