"""
Parameter Optimization Framework — Grid search over strategy parameters.

Evaluates each parameter combination by running a backtest and ranks
results by a chosen objective metric.

Usage:
    optimizer = ParameterOptimizer()
    result = await optimizer.optimize(
        strategy_factory=lambda params: MyStrategy(**params),
        param_grid={"take_profit_pct": [0.01, 0.02, 0.03], "stop_loss_pct": [0.01, 0.02]},
        data=data,
    )
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

# ---------------------------------------------------------------------------
# Module-level state for ProcessPoolExecutor workers (set via initializer).
# Must live at module level so worker processes can pickle/access them.
# ---------------------------------------------------------------------------
_ORCH_WORKER_DATA: Any = None
_ORCH_WORKER_CONFIG: Any = None
_ORCH_WORKER_OBJECTIVE: str = "sharpe_ratio"
_ORCH_WORKER_SYMBOL: str = ""
_ORCH_WORKER_TIMEOUT: float = 120.0  # seconds per trial before asyncio.TimeoutError


def _init_orchestrator_worker(
    data: Any, config_template: Any, objective: str, symbol: str, timeout: float = 120.0
) -> None:
    """Initializer called once per worker process — loads shared read-only state."""
    import logging as _logging

    global _ORCH_WORKER_DATA, _ORCH_WORKER_CONFIG, _ORCH_WORKER_OBJECTIVE
    global _ORCH_WORKER_SYMBOL, _ORCH_WORKER_TIMEOUT
    _ORCH_WORKER_DATA = data
    _ORCH_WORKER_CONFIG = config_template
    _ORCH_WORKER_OBJECTIVE = objective
    _ORCH_WORKER_SYMBOL = symbol
    _ORCH_WORKER_TIMEOUT = timeout

    # Suppress WARNING-level noise (SMC "insufficient data", TF "insufficient_data")
    _logging.root.setLevel(_logging.ERROR)
    for _h in _logging.root.handlers:
        _h.setLevel(_logging.ERROR)
    try:
        import structlog as _sl

        _sl.configure(wrapper_class=_sl.make_filtering_bound_logger(_logging.ERROR))
    except Exception:
        pass


def _build_worker_factories(engine: Any, symbol: str, cfg: Any) -> None:
    """
    Register strategy factories on engine inside a worker process.
    Closures are fine here — they live entirely within the subprocess.
    Mirrors the factory logic from scripts/run_backtest_v2.py.
    """
    from decimal import Decimal as _D

    try:
        from bot.strategies.grid_adapter import GridAdapter

        def _grid(p: dict) -> Any:
            return GridAdapter(symbol=symbol, **{**cfg.grid_params, **p})

        engine.register_strategy_factory("grid", _grid)
    except Exception:
        pass

    try:
        from bot.strategies.dca_adapter import DCAAdapter

        def _dca(p: dict) -> Any:
            merged = {**cfg.dca_params, **p}
            trigger = merged.pop("trigger_pct", None)
            tp = merged.pop("tp_pct", None)
            if trigger is not None:
                merged["price_deviation_pct"] = _D(str(trigger))
            if tp is not None:
                merged["take_profit_pct"] = _D(str(tp))
            return DCAAdapter(symbol=symbol, **merged)

        engine.register_strategy_factory("dca", _dca)
    except Exception:
        pass

    try:
        from bot.strategies.trend_follower.config import TrendFollowerConfig
        from bot.strategies.trend_follower_adapter import TrendFollowerAdapter

        _tf_fields = {f.name for f in TrendFollowerConfig.__dataclass_fields__.values()}
        _key_map = {
            "ema_fast": "ema_fast_period",
            "ema_slow": "ema_slow_period",
            "ema_fast_period": "ema_fast_period",
            "ema_slow_period": "ema_slow_period",
            "atr_period": "atr_period",
            "rsi_period": "rsi_period",
            "risk_per_trade_pct": "risk_per_trade_pct",
            "max_positions": "max_positions",
        }

        def _tf(p: dict) -> Any:
            merged = {**cfg.tf_params, **p}
            kw: dict = {}
            for k, v in merged.items():
                mapped = _key_map.get(k, k)
                if mapped in _tf_fields and mapped not in ("tp_multipliers", "sl_multipliers"):
                    kw[mapped] = v
            tp_s = merged.get("tp_atr_multiplier_sideways")
            tp_w = merged.get("tp_atr_multiplier_weak")
            tp_st = merged.get("tp_atr_multiplier_strong")
            if tp_s is not None and tp_w is not None and tp_st is not None:
                kw["tp_multipliers"] = (_D(str(tp_s)), _D(str(tp_w)), _D(str(tp_st)))
            sl_s = merged.get("sl_atr_multiplier_sideways")
            sl_t = merged.get("sl_atr_multiplier_trend")
            if sl_s is not None and sl_t is not None:
                kw["sl_multipliers"] = (_D(str(sl_s)), _D(str(sl_t)), _D(str(sl_t)))
            kw.setdefault("require_volume_confirmation", False)
            kw.setdefault("max_atr_filter_pct", _D("0.15"))
            kw.setdefault("log_all_signals", False)
            kw.setdefault("debug_mode", False)
            return TrendFollowerAdapter(config=TrendFollowerConfig(**kw))

        engine.register_strategy_factory("trend_follower", _tf)
    except Exception:
        pass

    try:
        from bot.strategies.smc.config import SMCConfig
        from bot.strategies.smc_adapter import SMCStrategyAdapter

        _smc_fields = {f.name for f in SMCConfig.__dataclass_fields__.values()}

        def _smc(p: dict) -> Any:
            merged = {**cfg.smc_params, **p}
            merged.setdefault("warmup_bars", 0)
            merged.setdefault("require_volume_confirmation", False)
            merged.setdefault("debug_mode", False)
            merged.setdefault("log_all_signals", False)
            kw = {k: v for k, v in merged.items() if k in _smc_fields}
            return SMCStrategyAdapter(
                config=SMCConfig(**kw),
                account_balance=_D("10000"),
                name="smc-backtest",
            )

        engine.register_strategy_factory("smc", _smc)
    except Exception:
        pass


def _run_orchestrator_trial(params: dict[str, Any]) -> Any:
    """
    Module-level worker function for ProcessPoolExecutor.
    Runs one backtest trial and returns an OptimizationTrial.
    Must be at module level (not a closure) to be picklable.
    """
    import asyncio as _asyncio

    from bot.tests.backtesting.optimization import (
        _ORCH_WORKER_CONFIG,
        _ORCH_WORKER_DATA,
        _ORCH_WORKER_OBJECTIVE,
        _ORCH_WORKER_SYMBOL,
        _ORCH_WORKER_TIMEOUT,
        OptimizationTrial,
        ParameterOptimizer,
        _build_worker_factories,
    )
    from bot.tests.backtesting.orchestrator_engine import BacktestOrchestratorEngine

    cfg = ParameterOptimizer._apply_orchestrator_params(_ORCH_WORKER_CONFIG, params)
    engine = BacktestOrchestratorEngine()
    _build_worker_factories(engine, _ORCH_WORKER_SYMBOL, cfg)

    async def _run_with_timeout() -> Any:
        return await _asyncio.wait_for(
            engine.run(_ORCH_WORKER_DATA, cfg), timeout=_ORCH_WORKER_TIMEOUT
        )

    result = _asyncio.run(_run_with_timeout())
    obj_val = float(getattr(result, _ORCH_WORKER_OBJECTIVE, None) or 0.0)
    return OptimizationTrial(params=params, result=result, objective_value=obj_val)


from bot.strategies.base import BaseStrategy  # noqa: E402
from bot.tests.backtesting.backtesting_engine import BacktestResult  # noqa: E402
from bot.tests.backtesting.multi_tf_data_loader import MultiTimeframeData  # noqa: E402
from bot.tests.backtesting.multi_tf_engine import (  # noqa: E402
    MultiTFBacktestConfig,
    MultiTimeframeBacktestEngine,
)

if TYPE_CHECKING:
    from bot.core.trading_core.core import TradingCore
    from bot.tests.backtesting.checkpoint import OptimizationCheckpoint
    from bot.tests.backtesting.orchestrator_engine import OrchestratorBacktestConfig


@dataclass
class OptimizationConfig:
    """Configuration for parameter optimization."""

    objective: str = "total_return_pct"
    higher_is_better: bool = True
    backtest_config: MultiTFBacktestConfig = field(default_factory=MultiTFBacktestConfig)


@dataclass
class OptimizationTrial:
    """Result of a single parameter combination trial."""

    params: dict[str, Any]
    result: BacktestResult
    objective_value: float


@dataclass
class OptimizationResult:
    """Results from parameter optimization."""

    best_params: dict[str, Any]
    best_result: BacktestResult
    best_objective: float
    all_trials: list[OptimizationTrial]
    objective_metric: str
    param_grid: dict[str, list[Any]]

    def top_n(self, n: int = 5) -> list[OptimizationTrial]:
        """Return top N trials by objective value."""
        return self.all_trials[:n]

    def get_param_impact(self, param_name: str) -> dict[Any, float]:
        """
        Average objective value for each value of a parameter.

        Useful for understanding parameter impact.
        """
        from collections import defaultdict

        groups: dict[Any, list[float]] = defaultdict(list)
        for trial in self.all_trials:
            val = trial.params.get(param_name)
            if val is not None:
                groups[val].append(trial.objective_value)

        return {k: sum(v) / len(v) for k, v in groups.items()}

    def get_param_impact_correlation(self) -> dict[str, float]:
        """
        Compute absolute Pearson correlation between each parameter and objective.

        Returns dict mapping parameter name to abs(correlation).
        """
        if len(self.all_trials) < 2:
            return {}

        result: dict[str, float] = {}
        objectives = [t.objective_value for t in self.all_trials]
        obj_mean = sum(objectives) / len(objectives)

        for param_name in self.param_grid:
            values = []
            for trial in self.all_trials:
                val = trial.params.get(param_name)
                try:
                    values.append(float(val))
                except (TypeError, ValueError):
                    values = None
                    break

            if values is None or len(values) < 2:
                continue

            val_mean = sum(values) / len(values)
            cov = sum(
                (v - val_mean) * (o - obj_mean) for v, o in zip(values, objectives, strict=False)
            ) / len(values)
            std_v = (sum((v - val_mean) ** 2 for v in values) / len(values)) ** 0.5
            std_o = (sum((o - obj_mean) ** 2 for o in objectives) / len(objectives)) ** 0.5

            if std_v > 0 and std_o > 0:
                result[param_name] = abs(cov / (std_v * std_o))
            else:
                result[param_name] = 0.0

        return result


class ParameterOptimizer:
    """
    Grid-search parameter optimizer for trading strategies.

    Takes a strategy factory callable and a parameter grid,
    evaluates each combination via backtesting, and returns ranked results.
    """

    def __init__(
        self,
        config: OptimizationConfig | None = None,
        checkpoint: OptimizationCheckpoint | None = None,
    ) -> None:
        self.config = config or OptimizationConfig()
        self.checkpoint = checkpoint

    async def optimize(
        self,
        strategy_factory: Callable[[dict[str, Any]], BaseStrategy],
        param_grid: dict[str, list[Any]],
        data: MultiTimeframeData,
        max_workers: int | None = None,
    ) -> OptimizationResult:
        """
        Run grid search optimization.

        Args:
            strategy_factory: Callable that takes a dict of parameters
                              and returns a BaseStrategy instance.
            param_grid: Dictionary mapping parameter names to lists of values.
            data: MultiTimeframeData to backtest on.
            max_workers: If > 1, run trials in parallel via ThreadPoolExecutor.

        Returns:
            OptimizationResult with ranked trials and best parameters.
        """
        combinations = self._generate_combinations(param_grid)
        run_id = str(uuid.uuid4())[:8]

        # Load checkpoint data if available
        completed: dict[str, dict] = {}
        if self.checkpoint:
            completed = self.checkpoint.load_completed(run_id)

        if max_workers and max_workers > 1:
            trials = await self._run_trials_parallel(
                combinations,
                strategy_factory,
                data,
                max_workers,
                run_id=run_id,
                completed=completed,
            )
        else:
            trials = await self._run_trials_sequential(
                combinations,
                strategy_factory,
                data,
                run_id=run_id,
                completed=completed,
            )

        # Sort by objective
        trials.sort(
            key=lambda t: t.objective_value,
            reverse=self.config.higher_is_better,
        )

        best = trials[0] if trials else None

        # Cleanup checkpoint on success
        if self.checkpoint:
            self.checkpoint.cleanup(run_id)

        return OptimizationResult(
            best_params=best.params if best else {},
            best_result=best.result if best else self._empty_result(),
            best_objective=best.objective_value if best else 0.0,
            all_trials=trials,
            objective_metric=self.config.objective,
            param_grid=param_grid,
        )

    async def two_phase_optimize(
        self,
        strategy_factory: Callable[[dict[str, Any]], BaseStrategy],
        param_grid: dict[str, list[Any]],
        data: MultiTimeframeData,
        coarse_steps: int = 3,
        fine_steps: int = 3,
        max_workers: int | None = None,
    ) -> OptimizationResult:
        """
        Two-phase coarse-to-fine parameter optimization.

        Phase 1 (Coarse): Sample evenly from each parameter range.
        Phase 2 (Fine): Narrow around best values from Phase 1.

        Args:
            strategy_factory: Callable that creates a strategy from params.
            param_grid: Full parameter grid.
            data: MultiTimeframeData.
            coarse_steps: Number of evenly-spaced samples per param in Phase 1.
            fine_steps: Number of samples in refined range in Phase 2.
            max_workers: If > 1, run trials in parallel.

        Returns:
            OptimizationResult with combined trials from both phases.
        """
        # Phase 1: Coarse search
        coarse_grid = {k: self._sample_evenly(v, coarse_steps) for k, v in param_grid.items()}
        coarse_result = await self.optimize(
            strategy_factory, coarse_grid, data, max_workers=max_workers
        )

        # Phase 2: Fine search around best params
        fine_grid: dict[str, list[Any]] = {}
        best = coarse_result.best_params

        for param_name, original_values in param_grid.items():
            best_val = best.get(param_name)
            if best_val is None:
                fine_grid[param_name] = original_values
                continue

            # Check if numeric
            try:
                best_float = float(best_val)
                orig_floats = [float(v) for v in original_values]
                lo = min(orig_floats)
                hi = max(orig_floats)

                # Narrow range: [best*0.7, best*1.3] clamped to original range
                fine_lo = max(lo, best_float * 0.7)
                fine_hi = min(hi, best_float * 1.3)

                # Determine type from original values
                if all(isinstance(v, int) for v in original_values):
                    fine_grid[param_name] = self._linspace(fine_lo, fine_hi, fine_steps, int)
                else:
                    fine_grid[param_name] = self._linspace(fine_lo, fine_hi, fine_steps, float)
            except (TypeError, ValueError):
                # Non-numeric: lock to best value
                fine_grid[param_name] = [best_val]

        fine_result = await self.optimize(
            strategy_factory, fine_grid, data, max_workers=max_workers
        )

        # Combine all trials
        all_trials = coarse_result.all_trials + fine_result.all_trials
        all_trials.sort(
            key=lambda t: t.objective_value,
            reverse=self.config.higher_is_better,
        )

        best_trial = all_trials[0] if all_trials else None

        return OptimizationResult(
            best_params=best_trial.params if best_trial else {},
            best_result=best_trial.result if best_trial else self._empty_result(),
            best_objective=best_trial.objective_value if best_trial else 0.0,
            all_trials=all_trials,
            objective_metric=self.config.objective,
            param_grid=param_grid,
        )

    async def _run_trials_sequential(
        self,
        combinations: list[dict[str, Any]],
        strategy_factory: Callable[[dict[str, Any]], BaseStrategy],
        data: MultiTimeframeData,
        run_id: str = "",
        completed: dict[str, dict] | None = None,
    ) -> list[OptimizationTrial]:
        """Run trials sequentially."""
        trials: list[OptimizationTrial] = []
        completed = completed or {}

        for params in combinations:
            # Check checkpoint
            if self.checkpoint and completed:
                from bot.tests.backtesting.checkpoint import OptimizationCheckpoint

                h = OptimizationCheckpoint.config_hash(params)
                if h in completed:
                    continue

            strategy = strategy_factory(params)
            engine = MultiTimeframeBacktestEngine(config=self.config.backtest_config)
            result = await engine.run(strategy, data)

            objective_val = self._get_objective_value(result)
            trial = OptimizationTrial(
                params=params,
                result=result,
                objective_value=objective_val,
            )
            trials.append(trial)

            # Save checkpoint
            if self.checkpoint:
                from bot.tests.backtesting.checkpoint import OptimizationCheckpoint

                h = OptimizationCheckpoint.config_hash(params)
                self.checkpoint.save_trial(run_id, h, h, result.to_dict())

        return trials

    async def _run_trials_parallel(
        self,
        combinations: list[dict[str, Any]],
        strategy_factory: Callable[[dict[str, Any]], BaseStrategy],
        data: MultiTimeframeData,
        max_workers: int,
        run_id: str = "",
        completed: dict[str, dict] | None = None,
    ) -> list[OptimizationTrial]:
        """Run trials in parallel using ThreadPoolExecutor."""
        completed = completed or {}
        combos_to_run = combinations

        if self.checkpoint and completed:
            from bot.tests.backtesting.checkpoint import OptimizationCheckpoint

            combos_to_run = [
                p for p in combinations if OptimizationCheckpoint.config_hash(p) not in completed
            ]

        loop = asyncio.get_event_loop()

        def run_sync(params: dict[str, Any]) -> tuple[dict[str, Any], BacktestResult]:
            strategy = strategy_factory(params)
            engine = MultiTimeframeBacktestEngine(config=self.config.backtest_config)
            result = asyncio.run(engine.run(strategy, data))
            return params, result

        trials: list[OptimizationTrial] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [loop.run_in_executor(pool, run_sync, p) for p in combos_to_run]
            results = await asyncio.gather(*futures)

        for params, result in results:
            objective_val = self._get_objective_value(result)
            trials.append(
                OptimizationTrial(
                    params=params,
                    result=result,
                    objective_value=objective_val,
                )
            )

        return trials

    def _generate_combinations(self, param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
        """Generate all parameter combinations from grid."""
        if not param_grid:
            return [{}]

        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = []
        for combo in itertools.product(*values):
            combinations.append(dict(zip(keys, combo, strict=False)))
        return combinations

    def _get_objective_value(self, result: BacktestResult) -> float:
        """Extract objective metric value from backtest result."""
        obj = self.config.objective

        value = getattr(result, obj, None)
        if value is None:
            return 0.0

        return float(value)

    @staticmethod
    def _sample_evenly(values: list[Any], n: int) -> list[Any]:
        """Sample n evenly-spaced values from a list."""
        if n >= len(values):
            return list(values)
        if n <= 0:
            return []
        if n == 1:
            return [values[len(values) // 2]]

        indices = [round(i * (len(values) - 1) / (n - 1)) for i in range(n)]
        seen = set()
        result = []
        for idx in indices:
            if idx not in seen:
                seen.add(idx)
                result.append(values[idx])
        return result

    @staticmethod
    def _linspace(lo: float, hi: float, steps: int, dtype: type = float) -> list[Any]:
        """Generate evenly-spaced values between lo and hi."""
        if steps <= 0:
            return []
        if steps == 1:
            mid = (lo + hi) / 2
            return [dtype(mid)]

        result = []
        for i in range(steps):
            val = lo + (hi - lo) * i / (steps - 1)
            result.append(dtype(val))

        # Deduplicate (important for int dtype)
        seen: list[Any] = []
        for v in result:
            if v not in seen:
                seen.append(v)
        return seen

    def _empty_result(self) -> BacktestResult:
        """Create a minimal empty BacktestResult."""
        from datetime import datetime, timedelta

        now = datetime.now()
        return BacktestResult(
            strategy_name="none",
            symbol="",
            start_time=now,
            end_time=now,
            duration=timedelta(0),
            initial_balance=Decimal("0"),
            final_balance=Decimal("0"),
            total_return=Decimal("0"),
            total_return_pct=Decimal("0"),
            max_drawdown=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=Decimal("0"),
            total_buy_orders=0,
            total_sell_orders=0,
            avg_profit_per_trade=Decimal("0"),
        )

    # ------------------------------------------------------------------
    # V2.0: Orchestrator optimization
    # ------------------------------------------------------------------

    async def optimize_orchestrator(
        self,
        param_grid: dict[str, list[Any]],
        data: MultiTimeframeData,
        config_template: OrchestratorBacktestConfig,
        max_workers: int | None = None,
        trial_timeout_sec: float = 120.0,
        progress_every_n: int = 100,
        checkpoint_path: str | None = None,
        progress_callback=None,  # Callable[[done, total, best_val, eta_sec], None] | None
    ) -> OptimizationResult:
        """
        Grid-search optimization targeting OrchestratorBacktestConfig params.

        The param_grid may contain keys from any sub-config namespace::

            {
                # Router params
                "router_cooldown_bars": [30, 60, 120],
                "regime_check_every_n": [6, 12, 24],
                # DCA params (written into config_template.dca_params)
                "dca_trigger_pct": [0.03, 0.05, 0.07],
                "dca_tp_pct": [0.05, 0.08, 0.10],
                # Risk params
                "max_position_size_pct": [0.15, 0.20, 0.25],
            }

        Top-level keys that match OrchestratorBacktestConfig fields are applied
        directly; others are forwarded into the appropriate sub-config dict
        based on their prefix (``dca_`` → dca_params, ``tf_`` → tf_params, etc.).

        Args:
            param_grid:       Parameter grid (name → list of values).
            data:             Multi-timeframe data to backtest on.
            config_template:  Base OrchestratorBacktestConfig to apply params to.
            max_workers:      Parallel workers (None = sequential).

        Returns:
            OptimizationResult with best params and all trials.
        """
        from bot.tests.backtesting.orchestrator_engine import (
            BacktestOrchestratorEngine,
        )

        combinations = self._generate_combinations(param_grid)
        trials: list[OptimizationTrial] = []

        async def _run_trial(params: dict[str, Any]) -> OptimizationTrial:
            cfg = self._apply_orchestrator_params(config_template, params)
            engine = BacktestOrchestratorEngine()
            # Re-use any registered factories from the optimizer config (if present)
            if hasattr(self, "_strategy_factories"):
                for name, factory in self._strategy_factories.items():
                    engine.register_strategy_factory(name, factory)
            result = await engine.run(data, cfg)
            obj_val = self._get_objective_value(result)
            return OptimizationTrial(params=params, result=result, objective_value=obj_val)

        if max_workers and max_workers > 1:
            import concurrent.futures
            import json
            import logging as _logging
            import time

            _logger = _logging.getLogger(__name__)
            loop = asyncio.get_event_loop()
            total = len(combinations)

            def _run_all_in_process_pool() -> list[OptimizationTrial]:
                _trials: list[OptimizationTrial] = []
                _timed_out = 0
                _failed = 0
                _t0 = time.monotonic()

                with concurrent.futures.ProcessPoolExecutor(
                    max_workers=max_workers,
                    initializer=_init_orchestrator_worker,
                    initargs=(
                        data,
                        config_template,
                        self.config.objective,
                        config_template.symbol,
                        trial_timeout_sec,
                    ),
                ) as pool:
                    futures = {pool.submit(_run_orchestrator_trial, p): p for p in combinations}

                    for future in concurrent.futures.as_completed(futures):
                        try:
                            trial = future.result()
                            _trials.append(trial)
                        except asyncio.TimeoutError:
                            _timed_out += 1
                            _logger.warning(
                                "[Phase 2] Trial timed out (>%.0fs): %s",
                                trial_timeout_sec,
                                futures[future],
                            )
                        except Exception as e:
                            _failed += 1
                            _logger.warning("[Phase 2] Trial failed: %s — %s", futures[future], e)

                        done = len(_trials) + _timed_out + _failed
                        if done % progress_every_n == 0 or done == total:
                            elapsed = time.monotonic() - _t0
                            rate = done / elapsed if elapsed > 0 else 0
                            eta = (total - done) / rate if rate > 0 else 0
                            best_val = (
                                max(_trials, key=lambda t: t.objective_value).objective_value
                                if _trials
                                else 0.0
                            )
                            _logger.info(
                                "[Phase 2] %d/%d (%.0f%%) | best=%.3f | "
                                "timeout=%d | failed=%d | ETA=%.0fs",
                                done,
                                total,
                                done / total * 100,
                                best_val,
                                _timed_out,
                                _failed,
                                eta,
                            )

                            # Fire external progress callback (used for Telegram etc.)
                            if progress_callback is not None:
                                try:
                                    progress_callback(done, total, best_val, eta)
                                except Exception:
                                    pass

                            # Save intermediate checkpoint
                            if checkpoint_path and _trials:
                                _sorted = sorted(
                                    _trials,
                                    key=lambda t: t.objective_value,
                                    reverse=True,
                                )
                                _top = _sorted[:20]
                                _cp = {
                                    "completed": done,
                                    "total": total,
                                    "timed_out": _timed_out,
                                    "failed": _failed,
                                    "top_trials": [
                                        {"params": t.params, "objective": t.objective_value}
                                        for t in _top
                                    ],
                                }
                                try:
                                    with open(checkpoint_path, "w") as _f:
                                        json.dump(_cp, _f, indent=2, default=str)
                                except Exception:
                                    pass

                return _trials

            trials = await loop.run_in_executor(None, _run_all_in_process_pool)
        else:
            for params in combinations:
                trial = await _run_trial(params)
                trials.append(trial)

        trials.sort(key=lambda t: t.objective_value, reverse=self.config.higher_is_better)
        best = trials[0] if trials else None

        return OptimizationResult(
            best_params=best.params if best else {},
            best_result=best.result if best else self._empty_result(),
            best_objective=best.objective_value if best else 0.0,
            all_trials=trials,
            objective_metric=self.config.objective,
            param_grid=param_grid,
        )

    @staticmethod
    def _apply_orchestrator_params(
        template: OrchestratorBacktestConfig,
        params: dict[str, Any],
    ) -> OrchestratorBacktestConfig:
        """
        Create a new OrchestratorBacktestConfig from template with params applied.

        Routing:
        - Keys that match top-level fields → set directly.
        - ``dca_*`` → dca_params dict.
        - ``tf_*`` → tf_params dict.
        - ``grid_*`` → grid_params dict.
        - ``smc_*`` → smc_params dict.
        """
        from bot.tests.backtesting.orchestrator_engine import OrchestratorBacktestConfig

        # Copy mutable fields explicitly to avoid aliasing
        cfg = OrchestratorBacktestConfig(
            symbol=template.symbol,
            initial_balance=template.initial_balance,
            lookback=template.lookback,
            warmup_bars=template.warmup_bars,
            default_analyze_every_n=template.default_analyze_every_n,
            smc_analyze_every_n=template.smc_analyze_every_n,
            enable_grid=template.enable_grid,
            enable_dca=template.enable_dca,
            enable_trend_follower=template.enable_trend_follower,
            enable_smc=template.enable_smc,
            enable_strategy_router=template.enable_strategy_router,
            router_cooldown_bars=template.router_cooldown_bars,
            regime_check_every_n=template.regime_check_every_n,
            grid_params=dict(template.grid_params),
            dca_params=dict(template.dca_params),
            tf_params=dict(template.tf_params),
            smc_params=dict(template.smc_params),
            enable_risk_manager=template.enable_risk_manager,
            max_position_size_pct=template.max_position_size_pct,
            max_daily_loss_pct=template.max_daily_loss_pct,
            portfolio_stop_loss_pct=template.portfolio_stop_loss_pct,
            risk_per_trade=template.risk_per_trade,
            max_position_pct=template.max_position_pct,
            # Fees — ensure they carry over from template (Bybit VIP0 by default)
            maker_fee=template.maker_fee,
            taker_fee=template.taker_fee,
            slippage=template.slippage,
        )

        _prefix_map = {
            "dca_": "dca_params",
            "tf_": "tf_params",
            "grid_": "grid_params",
            "smc_": "smc_params",
        }

        import dataclasses

        top_fields = {f.name for f in dataclasses.fields(cfg)}

        for key, val in params.items():
            if key in top_fields:
                object.__setattr__(cfg, key, val)
            else:
                routed = False
                for prefix, sub_dict_name in _prefix_map.items():
                    if key.startswith(prefix):
                        sub_key = key[len(prefix) :]
                        getattr(cfg, sub_dict_name)[sub_key] = val
                        routed = True
                        break
                if not routed:
                    # Unknown key — try top-level as fallback
                    if hasattr(cfg, key):
                        object.__setattr__(cfg, key, val)

        return cfg

    async def optimize_with_core(
        self,
        param_grid: dict[str, list[Any]],
        data: MultiTimeframeData,
        core: TradingCore,
        *,
        bar_duration_seconds: int = 300,
        lookback: int = 100,
        warmup_bars: int = 14400,
        max_workers: int | None = None,
    ) -> OptimizationResult:
        """
        Grid-search optimization using TradingCore as configuration source.

        This is the parity-safe entry point for optimization — all base
        parameters (cooldown_bars, fees, risk limits) are derived from
        TradingCore, preventing divergence with the live bot.

        The ``param_grid`` may override any OrchestratorBacktestConfig field,
        using the same prefix routing as ``optimize_orchestrator()``::

            {
                # Routing params (override derived values for search)
                "router_cooldown_bars": [1, 2, 4],       # bars (M5)
                "regime_check_every_n": [6, 12, 24],
                # DCA params
                "dca_trigger_pct": [0.03, 0.05, 0.07],
                # TF params
                "tf_ema_fast": [10, 15, 20],
            }

        Args:
            param_grid:           Parameter grid (name → list of values).
            data:                 Pre-loaded multi-timeframe OHLCV data.
            core:                 TradingCore instance (base config source).
            bar_duration_seconds: Bar length in seconds. Default 300 = M5.
            lookback:             OHLCV lookback window.
            warmup_bars:          Warmup bars before strategy execution.
            max_workers:          Parallel workers (None = sequential).

        Returns:
            OptimizationResult with best params and all trials.
        """
        from bot.tests.backtesting.unified_engine import (
            UnifiedBacktestEngine,
            trading_core_to_backtest_config,
        )

        # Derive base config from TradingCore — guaranteed parity
        base_config = trading_core_to_backtest_config(
            core,
            lookback=lookback,
            warmup_bars=warmup_bars,
            bar_duration_seconds=bar_duration_seconds,
        )

        combinations = self._generate_combinations(param_grid)
        trials: list[OptimizationTrial] = []

        async def _run_trial(params: dict[str, Any]) -> OptimizationTrial:
            cfg = self._apply_orchestrator_params(base_config, params)
            engine = UnifiedBacktestEngine()
            if hasattr(self, "_strategy_factories"):
                for name, factory in self._strategy_factories.items():
                    engine.register_strategy_factory(name, factory)
            result = await engine.run(data, cfg)
            obj_val = self._get_objective_value(result)
            return OptimizationTrial(params=params, result=result, objective_value=obj_val)

        if max_workers and max_workers > 1:
            loop = asyncio.get_event_loop()

            def _run_sync(p: dict[str, Any]) -> OptimizationTrial:
                return asyncio.run(_run_trial(p))

            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [loop.run_in_executor(pool, _run_sync, p) for p in combinations]
                trials = list(await asyncio.gather(*futures))
        else:
            for params in combinations:
                trial = await _run_trial(params)
                trials.append(trial)

        trials.sort(key=lambda t: t.objective_value, reverse=self.config.higher_is_better)
        best = trials[0] if trials else None

        return OptimizationResult(
            best_params=best.params if best else {},
            best_result=best.result if best else self._empty_result(),
            best_objective=best.objective_value if best else 0.0,
            all_trials=trials,
            objective_metric=self.config.objective,
            param_grid=param_grid,
        )
