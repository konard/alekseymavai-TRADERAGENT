"""Backtesting engine for evaluating trading strategies"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .market_simulator import MarketSimulator
from .test_data import HistoricalDataProvider


@dataclass
class BacktestResult:
    """Results from a backtest run"""

    strategy_name: str
    symbol: str
    start_time: datetime
    end_time: datetime
    duration: timedelta

    # Performance metrics
    initial_balance: Decimal
    final_balance: Decimal
    total_return: Decimal
    total_return_pct: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal

    # Trading statistics
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal

    # Order statistics
    total_buy_orders: int
    total_sell_orders: int
    avg_profit_per_trade: Decimal

    # Risk metrics
    sharpe_ratio: Decimal | None = None
    sortino_ratio: Decimal | None = None
    calmar_ratio: Decimal | None = None
    profit_factor: Decimal | None = None
    capital_efficiency: Decimal | None = None
    max_position_value: Decimal = Decimal("0")

    trade_history: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)

    # Regime tracking
    regime_history: list[dict[str, Any]] = field(default_factory=list)
    regime_changes: int = 0
    regime_filter_blocks: int = 0

    # Risk management tracking
    risk_manager_blocks: int = 0
    risk_halted: bool = False
    risk_halt_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary"""
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "duration_hours": self.duration.total_seconds() / 3600,
            "performance": {
                "initial_balance": float(self.initial_balance),
                "final_balance": float(self.final_balance),
                "total_return": float(self.total_return),
                "total_return_pct": float(self.total_return_pct),
                "max_drawdown": float(self.max_drawdown),
                "max_drawdown_pct": float(self.max_drawdown_pct),
            },
            "trading_stats": {
                "total_trades": self.total_trades,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "win_rate": float(self.win_rate),
                "total_buy_orders": self.total_buy_orders,
                "total_sell_orders": self.total_sell_orders,
                "avg_profit_per_trade": float(self.avg_profit_per_trade),
            },
            "risk_metrics": {
                "sharpe_ratio": float(self.sharpe_ratio) if self.sharpe_ratio else None,
                "sortino_ratio": float(self.sortino_ratio) if self.sortino_ratio else None,
                "calmar_ratio": float(self.calmar_ratio) if self.calmar_ratio else None,
                "profit_factor": float(self.profit_factor) if self.profit_factor else None,
                "capital_efficiency": (
                    float(self.capital_efficiency) if self.capital_efficiency else None
                ),
                "max_position_value": float(self.max_position_value),
            },
            "trade_count": len(self.trade_history),
            "data_points": len(self.equity_curve),
            "regime_tracking": {
                "regime_history_count": len(self.regime_history),
                "regime_changes": self.regime_changes,
                "regime_filter_blocks": self.regime_filter_blocks,
            },
            "risk_management": {
                "risk_manager_blocks": self.risk_manager_blocks,
                "risk_halted": self.risk_halted,
                "risk_halt_reason": self.risk_halt_reason,
            },
        }

    def print_summary(self) -> None:
        """Print formatted backtest summary"""
        print("=" * 70)
        print(f"Backtest Results: {self.strategy_name}")
        print("=" * 70)
        print(f"\nSymbol: {self.symbol}")
        print(
            f"Period: {self.start_time.strftime('%Y-%m-%d')} to {self.end_time.strftime('%Y-%m-%d')}"
        )
        print(
            f"Duration: {self.duration.days} days ({self.duration.total_seconds() / 3600:.1f} hours)"
        )
        print("\nPerformance Metrics:")
        print(f"  Initial Balance:  ${self.initial_balance:,.2f}")
        print(f"  Final Balance:    ${self.final_balance:,.2f}")
        print(f"  Total Return:     ${self.total_return:,.2f} ({self.total_return_pct:.2f}%)")
        print(f"  Max Drawdown:     ${self.max_drawdown:,.2f} ({self.max_drawdown_pct:.2f}%)")
        print("\nTrading Statistics:")
        print(f"  Total Trades:     {self.total_trades}")
        print(f"  Winning Trades:   {self.winning_trades}")
        print(f"  Losing Trades:    {self.losing_trades}")
        print(f"  Win Rate:         {self.win_rate:.2f}%")
        print(f"  Buy Orders:       {self.total_buy_orders}")
        print(f"  Sell Orders:      {self.total_sell_orders}")
        print(f"  Avg Profit/Trade: ${self.avg_profit_per_trade:.2f}")
        risk_lines = []
        if self.sharpe_ratio:
            risk_lines.append(f"  Sharpe Ratio:     {self.sharpe_ratio:.4f}")
        if self.sortino_ratio:
            risk_lines.append(f"  Sortino Ratio:    {self.sortino_ratio:.4f}")
        if self.calmar_ratio:
            risk_lines.append(f"  Calmar Ratio:     {self.calmar_ratio:.4f}")
        if self.profit_factor:
            risk_lines.append(f"  Profit Factor:    {self.profit_factor:.4f}")
        if self.capital_efficiency:
            risk_lines.append(f"  Capital Effic.:   {self.capital_efficiency:.4f}")
        if risk_lines:
            print("\nRisk Metrics:")
            for line in risk_lines:
                print(line)
        if self.regime_changes or self.regime_filter_blocks:
            print("\nRegime Tracking:")
            print(f"  Regime Changes:    {self.regime_changes}")
            print(f"  Signals Blocked:   {self.regime_filter_blocks}")
        if self.risk_manager_blocks or self.risk_halted:
            print("\nRisk Management:")
            print(f"  Trades Blocked:    {self.risk_manager_blocks}")
            if self.risk_halted:
                print(f"  HALTED: {self.risk_halt_reason}")
        print("=" * 70)


class BacktestingEngine:
    """
    Engine for running backtests on trading strategies.

    Features:
    - Historical data replay
    - Strategy performance evaluation
    - Risk metrics calculation
    - Trade analysis
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        initial_balance: Decimal = Decimal("10000"),
    ):
        self.symbol = symbol
        self.initial_balance = initial_balance
        self.simulator = MarketSimulator(
            symbol=symbol,
            initial_balance_quote=initial_balance,
        )
        self.data_provider = HistoricalDataProvider()

    async def run_backtest(
        self,
        strategy_name: str,
        strategy_config: dict[str, Any],
        start_date: datetime,
        end_date: datetime,
        data_interval: str = "1h",
    ) -> BacktestResult:
        """
        Run backtest for a trading strategy.

        Args:
            strategy_name: Name of the strategy
            strategy_config: Strategy configuration parameters
            start_date: Backtest start date
            end_date: Backtest end date
            data_interval: Price data interval (e.g., "1h", "4h", "1d")

        Returns:
            BacktestResult with performance metrics
        """
        # Reset simulator
        self.simulator.reset(self.initial_balance)

        # Get historical data
        price_data = self.data_provider.get_historical_prices(
            symbol=self.symbol,
            start_date=start_date,
            end_date=end_date,
            interval=data_interval,
        )

        if not price_data:
            raise ValueError("No historical data available for the specified period")

        # Initialize tracking
        equity_curve = []
        peak_value = self.initial_balance
        max_drawdown = Decimal("0")

        # Simulate strategy over historical data
        for _i, candle in enumerate(price_data):
            timestamp = candle["timestamp"]
            price = Decimal(str(candle["close"]))

            # Update market price
            await self.simulator.set_price(price)

            # Record portfolio value
            portfolio_value = self.simulator.get_portfolio_value()
            equity_curve.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "price": float(price),
                    "portfolio_value": float(portfolio_value),
                }
            )

            # Update drawdown
            if portfolio_value > peak_value:
                peak_value = portfolio_value
            else:
                drawdown = peak_value - portfolio_value
                if drawdown > max_drawdown:
                    max_drawdown = drawdown

            # Strategy would execute here in real implementation
            # For now, we'll let the test create orders manually
            await asyncio.sleep(0)  # Allow other tasks to run

        # Calculate results
        return self._calculate_results(
            strategy_name=strategy_name,
            start_time=start_date,
            end_time=end_date,
            max_drawdown=max_drawdown,
            equity_curve=equity_curve,
        )

    def _calculate_results(
        self,
        strategy_name: str,
        start_time: datetime,
        end_time: datetime,
        max_drawdown: Decimal,
        equity_curve: list[dict[str, Any]],
    ) -> BacktestResult:
        """Calculate backtest results"""
        # Get trade history
        trade_history = self.simulator.get_trade_history()

        # Calculate final balance
        final_balance = self.simulator.get_portfolio_value()
        total_return = final_balance - self.initial_balance
        total_return_pct = (total_return / self.initial_balance) * Decimal("100")

        # Calculate max drawdown percentage
        max_drawdown_pct = (
            (max_drawdown / self.initial_balance) * Decimal("100")
            if self.initial_balance > 0
            else Decimal("0")
        )

        # Analyze trades
        winning_trades = 0
        losing_trades = 0
        total_profit = Decimal("0")

        # Group trades by order pairs (buy-sell)
        buy_orders = [t for t in trade_history if t["side"] == "buy"]
        sell_orders = [t for t in trade_history if t["side"] == "sell"]

        # Simple profit calculation (each sell closes a previous buy)
        for i in range(min(len(buy_orders), len(sell_orders))):
            buy_price = Decimal(str(buy_orders[i]["price"]))
            sell_price = Decimal(str(sell_orders[i]["price"]))
            amount = Decimal(str(buy_orders[i]["amount"]))

            profit = (sell_price - buy_price) * amount
            total_profit += profit

            if profit > 0:
                winning_trades += 1
            else:
                losing_trades += 1

        total_trades = winning_trades + losing_trades
        win_rate = (
            (Decimal(winning_trades) / Decimal(total_trades) * Decimal("100"))
            if total_trades > 0
            else Decimal("0")
        )
        avg_profit = total_profit / Decimal(total_trades) if total_trades > 0 else Decimal("0")

        # Calculate Sharpe ratio (simplified)
        sharpe_ratio = self._calculate_sharpe_ratio(equity_curve)

        return BacktestResult(
            strategy_name=strategy_name,
            symbol=self.symbol,
            start_time=start_time,
            end_time=end_time,
            duration=end_time - start_time,
            initial_balance=self.initial_balance,
            final_balance=final_balance,
            total_return=total_return,
            total_return_pct=total_return_pct,
            max_drawdown=max_drawdown,
            max_drawdown_pct=max_drawdown_pct,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            total_buy_orders=len(buy_orders),
            total_sell_orders=len(sell_orders),
            avg_profit_per_trade=avg_profit,
            sharpe_ratio=sharpe_ratio,
            trade_history=trade_history,
            equity_curve=equity_curve,
        )

    def _calculate_sharpe_ratio(self, equity_curve: list[dict[str, Any]]) -> Decimal | None:
        """Calculate Sharpe ratio from equity curve"""
        if len(equity_curve) < 2:
            return None

        # Calculate returns
        returns = []
        for i in range(1, len(equity_curve)):
            prev_value = Decimal(str(equity_curve[i - 1]["portfolio_value"]))
            curr_value = Decimal(str(equity_curve[i]["portfolio_value"]))
            if prev_value > 0:
                ret = (curr_value - prev_value) / prev_value
                returns.append(ret)

        if not returns:
            return None

        # Calculate mean and std of returns
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        std_return = variance.sqrt() if variance > 0 else Decimal("0")

        # Sharpe ratio (assuming 0 risk-free rate for simplicity)
        if std_return > 0:
            sharpe = mean_return / std_return
            # Annualize (assuming hourly returns)
            sharpe = sharpe * Decimal(str((365 * 24) ** 0.5))
            return sharpe

        return None

    async def run_grid_backtest(
        self,
        grid_config: dict[str, Any],
        start_date: datetime,
        end_date: datetime,
    ) -> BacktestResult:
        """Run backtest specifically for grid trading strategy"""
        # Simplified grid strategy simulation
        upper_price = Decimal(str(grid_config["upper_price"]))
        lower_price = Decimal(str(grid_config["lower_price"]))
        grid_levels = grid_config["grid_levels"]
        Decimal(str(grid_config["amount_per_grid"]))

        # Calculate grid prices
        price_step = (upper_price - lower_price) / Decimal(grid_levels - 1)
        [lower_price + price_step * Decimal(i) for i in range(grid_levels)]

        # Run backtest
        return await self.run_backtest(
            strategy_name="Grid Trading",
            strategy_config=grid_config,
            start_date=start_date,
            end_date=end_date,
        )

    def save_results(self, result: BacktestResult, filepath: str) -> None:
        """Save backtest results to file"""
        with open(filepath, "w") as f:
            json.dump(result.to_dict(), f, indent=2)
        print(f"Results saved to {filepath}")
