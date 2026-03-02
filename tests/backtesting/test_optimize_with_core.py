"""Tests for optimize_with_core() in ParameterOptimizer (Phase 5)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.core.trading_core import TradingCore, TradingCoreConfig
from bot.tests.backtesting.optimization import (
    OptimizationConfig,
    OptimizationResult,
    ParameterOptimizer,
)
from bot.tests.backtesting.orchestrator_engine import OrchestratorBacktestConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_optimizer(objective: str = "total_return_pct") -> ParameterOptimizer:
    return ParameterOptimizer(OptimizationConfig(objective=objective))


def _make_core(**kwargs: Any) -> TradingCore:
    return TradingCore.from_config(TradingCoreConfig(**kwargs))


def _make_mock_result() -> MagicMock:
    """Synthetic backtest result for mocking engine.run()."""
    result = MagicMock()
    result.total_return_pct = 5.0
    result.sharpe_ratio = 1.2
    result.max_drawdown_pct = 8.0
    result.total_trades = 10
    result.profitable_trades = 7
    result.to_dict.return_value = {}
    return result


# ---------------------------------------------------------------------------
# _apply_orchestrator_params: fee copy-over fix
# ---------------------------------------------------------------------------


class TestApplyOrchestratorParamsFeeCarryover:
    """Verify fees survive _apply_orchestrator_params (regression for Phase 4 fix)."""

    def test_fees_carried_from_template(self) -> None:
        template = OrchestratorBacktestConfig(
            maker_fee=Decimal("0.0002"),
            taker_fee=Decimal("0.00055"),
            slippage=Decimal("0.0003"),
        )
        result = ParameterOptimizer._apply_orchestrator_params(template, {})
        assert result.maker_fee == Decimal("0.0002")
        assert result.taker_fee == Decimal("0.00055")
        assert result.slippage == Decimal("0.0003")

    def test_fees_override_via_params(self) -> None:
        template = OrchestratorBacktestConfig(
            maker_fee=Decimal("0.0002"),
        )
        result = ParameterOptimizer._apply_orchestrator_params(
            template, {"maker_fee": Decimal("0.001")}
        )
        assert result.maker_fee == Decimal("0.001")


# ---------------------------------------------------------------------------
# optimize_with_core
# ---------------------------------------------------------------------------


class TestOptimizeWithCore:
    """Verify optimize_with_core() uses TradingCore for parity-safe parameter derivation."""

    @pytest.mark.asyncio
    async def test_returns_optimization_result(self) -> None:
        """optimize_with_core returns OptimizationResult (mocked engine)."""
        optimizer = _make_optimizer()
        core = _make_core()
        data = MagicMock()

        mock_result = _make_mock_result()

        with patch(
            "bot.tests.backtesting.unified_engine.BacktestOrchestratorEngine.run",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await optimizer.optimize_with_core(
                param_grid={"router_cooldown_bars": [1, 2]},
                data=data,
                core=core,
                warmup_bars=10,
            )

        assert isinstance(result, OptimizationResult)
        assert len(result.all_trials) == 2  # 2 values in param_grid

    @pytest.mark.asyncio
    async def test_base_config_derived_from_core(self) -> None:
        """Verify the config passed to engine has TradingCore-derived params."""
        optimizer = _make_optimizer()
        core = _make_core(
            cooldown_seconds=600,
            max_daily_loss_pct=0.05,
        )
        data = MagicMock()
        captured_configs: list[OrchestratorBacktestConfig] = []

        async def _capture_run(data: Any, config: OrchestratorBacktestConfig) -> MagicMock:
            captured_configs.append(config)
            return _make_mock_result()

        with patch(
            "bot.tests.backtesting.unified_engine.BacktestOrchestratorEngine.run",
            new_callable=AsyncMock,
            side_effect=_capture_run,
        ):
            await optimizer.optimize_with_core(
                param_grid={"router_cooldown_bars": [2]},
                data=data,
                core=core,
                bar_duration_seconds=300,
                warmup_bars=10,
            )

        assert len(captured_configs) == 1
        cfg = captured_configs[0]
        # Cooldown should be 2 bars (600s/300s), not the old default 60
        assert cfg.router_cooldown_bars == 2
        # Daily loss 5%, not 25%
        assert cfg.max_daily_loss_pct == pytest.approx(0.05)
        # Bybit VIP0 fees
        assert cfg.maker_fee == Decimal("0.0002")

    @pytest.mark.asyncio
    async def test_param_grid_overrides_cooldown(self) -> None:
        """param_grid can override router_cooldown_bars derived from TradingCore."""
        optimizer = _make_optimizer()
        core = _make_core(cooldown_seconds=600)
        data = MagicMock()
        captured_configs: list[OrchestratorBacktestConfig] = []

        async def _capture_run(data: Any, config: OrchestratorBacktestConfig) -> MagicMock:
            captured_configs.append(config)
            return _make_mock_result()

        with patch(
            "bot.tests.backtesting.unified_engine.BacktestOrchestratorEngine.run",
            new_callable=AsyncMock,
            side_effect=_capture_run,
        ):
            await optimizer.optimize_with_core(
                param_grid={"router_cooldown_bars": [5, 10, 20]},
                data=data,
                core=core,
                bar_duration_seconds=300,
                warmup_bars=10,
            )

        cooldowns = {cfg.router_cooldown_bars for cfg in captured_configs}
        assert cooldowns == {5, 10, 20}

    @pytest.mark.asyncio
    async def test_best_trial_selected(self) -> None:
        """Optimizer picks the trial with best objective."""
        optimizer = _make_optimizer(objective="total_return_pct")
        core = _make_core()
        data = MagicMock()
        call_count = [0]

        async def _varying_return(data: Any, config: OrchestratorBacktestConfig) -> MagicMock:
            call_count[0] += 1
            result = _make_mock_result()
            result.total_return_pct = float(config.router_cooldown_bars)
            return result

        with patch(
            "bot.tests.backtesting.unified_engine.BacktestOrchestratorEngine.run",
            new_callable=AsyncMock,
            side_effect=_varying_return,
        ):
            opt_result = await optimizer.optimize_with_core(
                param_grid={"router_cooldown_bars": [1, 3, 7]},
                data=data,
                core=core,
                warmup_bars=10,
            )

        # Best = highest router_cooldown_bars (=7) because total_return = cooldown_bars
        assert opt_result.best_params["router_cooldown_bars"] == 7
        assert call_count[0] == 3

    @pytest.mark.asyncio
    async def test_empty_param_grid_returns_single_trial(self) -> None:
        """Empty param_grid → single trial with all defaults from TradingCore."""
        optimizer = _make_optimizer()
        core = _make_core()
        data = MagicMock()

        with patch(
            "bot.tests.backtesting.unified_engine.BacktestOrchestratorEngine.run",
            new_callable=AsyncMock,
            return_value=_make_mock_result(),
        ):
            result = await optimizer.optimize_with_core(
                param_grid={},
                data=data,
                core=core,
                warmup_bars=10,
            )

        assert len(result.all_trials) == 1
