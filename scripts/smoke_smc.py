#!/usr/bin/env python3
"""
SMC Smoke Test — запускает только SMC-стратегию в изоляции на синтетических данных.

Цель: диагностика причин SMC=0 trades в Phase 1.

Выводит:
  - Сколько раз вызывался generate_signal (после engine warmup)
  - Сколько пропущено из-за SMC-внутреннего warmup
  - Сколько пропущено из-за RANGING H1
  - Сколько вернули сигналы (non-empty)
  - Сколько trade открыто

Запуск:
    python scripts/smoke_smc.py
    python scripts/smoke_smc.py --bars 3000 --trend up
    python scripts/smoke_smc.py --bars 3000 --trend down --warmup 200
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import types
import warnings
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

warnings.filterwarnings("ignore")  # suppress synthetic-data UserWarning


# ---------------------------------------------------------------------------
# Counters — populated by wrapped methods
# ---------------------------------------------------------------------------

class _Counters:
    def __init__(self):
        self.generate_signal_calls: int = 0   # adapter.generate_signal() calls
        self.warmup_skip: int = 0              # SMC internal warmup
        self.empty_df: int = 0                 # df_h1 or df_m5 empty
        self.ranging_h1: int = 0               # H1 trend == RANGING
        self.no_pattern: int = 0               # generate_signals_m5 returned []
        self.signals_produced: int = 0         # non-empty signals returned
        self.signal_to_trade: int = 0          # non-None from adapter.generate_signal

CTR = _Counters()


def _wrap_adapter(adapter) -> None:
    """Wrap adapter.generate_signal to count calls and non-None returns."""
    _orig = adapter.generate_signal

    def _wrapped(df, balance):
        CTR.generate_signal_calls += 1
        result = _orig(df, balance)
        if result is not None:
            CTR.signal_to_trade += 1
        return result

    adapter.generate_signal = _wrapped


def _wrap_strategy(strategy) -> None:
    """Wrap strategy.generate_signals_m5 to classify skip reasons."""
    _orig = strategy.generate_signals_m5

    def _wrapped(df_h1, df_m5):
        # Peek before calling (original increments _generate_call_count inside)
        next_count = strategy._generate_call_count + 1
        will_warmup = next_count <= strategy.config.warmup_bars

        result = _orig(df_h1, df_m5)  # real call — increments counter internally

        if will_warmup:
            CTR.warmup_skip += 1
        elif df_h1.empty or df_m5.empty:
            CTR.empty_df += 1
        elif result:
            CTR.signals_produced += len(result)
        else:
            # Call returned empty — check why: ranging or no pattern
            try:
                from bot.strategies.smc.smc_strategy import TrendDirection
                h1_struct = strategy.market_structure.analyze(df_h1)
                h1_trend = h1_struct.get("trend", strategy.current_trend)
                if h1_trend == TrendDirection.RANGING:
                    CTR.ranging_h1 += 1
                else:
                    CTR.no_pattern += 1
            except Exception:
                CTR.no_pattern += 1

        return result

    strategy.generate_signals_m5 = _wrapped


# ---------------------------------------------------------------------------
# Patched factory — intercepts adapter creation to install wrappers
# ---------------------------------------------------------------------------

def _make_smc_factory(symbol: str, smc_params: dict):
    """Build a patched SMC factory that instruments the adapter."""
    from bot.strategies.smc_adapter import SMCStrategyAdapter
    from bot.strategies.smc.config import SMCConfig

    _smc_fields = {f.name for f in SMCConfig.__dataclass_fields__.values()}  # type: ignore

    def factory(params: dict):
        merged = {**smc_params, **params}
        merged.setdefault("require_volume_confirmation", False)
        merged.setdefault("debug_mode", True)
        merged.setdefault("log_all_signals", True)
        cfg_kwargs = {k: v for k, v in merged.items() if k in _smc_fields}
        cfg = SMCConfig(**cfg_kwargs)
        adapter = SMCStrategyAdapter(
            config=cfg,
            account_balance=Decimal("10000"),
            name="smc-smoke",
        )
        # Install wrappers immediately after creation
        _wrap_adapter(adapter)
        if hasattr(adapter, "_strategy"):
            _wrap_strategy(adapter._strategy)
        return adapter

    return factory


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_smoke(bars: int, trend: str, engine_warmup: int, rr: float) -> None:
    from bot.tests.backtesting.multi_tf_data_loader import MultiTimeframeDataLoader
    from bot.tests.backtesting.orchestrator_engine import (
        BacktestOrchestratorEngine,
        OrchestratorBacktestConfig,
    )

    print(f"\n{'='*60}")
    print(f"  SMC Smoke Test")
    print(f"  bars={bars}  trend={trend}  engine_warmup={engine_warmup}  min_rr={rr}")
    print(f"{'='*60}\n")

    # 1. Synthetic multi-TF data
    loader = MultiTimeframeDataLoader()
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5 * bars)
    data = loader.load("BTC/USDT", start, end, trend=trend)
    print(f"  Data: M5={len(data.m5)}  H1={len(data.h1)}  D1={len(data.d1)} bars")

    smc_params = {
        "swing_length": 10,
        "warmup_bars": 40,                       # minimal internal warmup
        "require_volume_confirmation": False,
        "min_risk_reward": Decimal(str(rr)),
        "debug_mode": True,
        "log_all_signals": True,
    }

    config = OrchestratorBacktestConfig(
        symbol="BTC/USDT",
        initial_balance=Decimal("10000"),
        warmup_bars=engine_warmup,
        enable_grid=False,
        enable_dca=False,
        enable_trend_follower=False,
        enable_smc=True,
        enable_strategy_router=False,    # no regime routing — SMC always active
        smc_generate_signal_every_n=1,   # every bar (remove throttle for max coverage)
        smc_analyze_every_n=1,
        smc_params=smc_params,
    )

    # 2. Register patched factory
    engine = BacktestOrchestratorEngine()
    engine.register_strategy_factory("smc", _make_smc_factory("BTC/USDT", smc_params))
    print(f"  Engine warmup: {engine_warmup} bars")
    print(f"  SMC internal warmup: {smc_params['warmup_bars']} calls")
    print(f"  Effective dead bars: ~{engine_warmup + smc_params['warmup_bars']}")
    print(f"  Signal bars available: ~{max(0, bars - engine_warmup - smc_params['warmup_bars'])}\n")

    # 3. Run
    result = await engine.run(data, config)

    # 4. Report
    total_calls = (CTR.warmup_skip + CTR.empty_df + CTR.ranging_h1 +
                   CTR.no_pattern + CTR.signals_produced)

    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"  Total trades:  {result.total_trades}")
    print(f"  Total return:  {result.total_return_pct:+.2f}%")
    print(f"  Win rate:      {result.win_rate:.1f}%")
    sharpe = f"{result.sharpe_ratio:.2f}" if result.sharpe_ratio is not None else "N/A"
    print(f"  Sharpe:        {sharpe}")
    dd = result.max_drawdown_pct if result.max_drawdown_pct is not None else 0.0
    print(f"  Max drawdown:  {dd:.1f}%")

    print(f"\n{'='*60}")
    print(f"  SMC SIGNAL DIAGNOSTICS")
    print(f"{'='*60}")
    print(f"  adapter.generate_signal() calls: {CTR.generate_signal_calls}")
    print(f"  generate_signals_m5() calls:     {total_calls}")
    print()
    print(f"  {'Reason':<35} {'Count':>7}  {'%':>6}")
    print(f"  {'-'*52}")

    def row(label, count):
        pct = count / max(total_calls, 1) * 100
        print(f"  {label:<35} {count:>7}  {pct:>5.1f}%")

    row("SMC internal warmup skip",    CTR.warmup_skip)
    row("Empty df_h1 or df_m5",        CTR.empty_df)
    row("H1 trend = RANGING (skip)",   CTR.ranging_h1)
    row("No BOS/CHoCH pattern found",  CTR.no_pattern)
    row("Signals produced",            CTR.signals_produced)
    print()
    print(f"  Signals → non-None from adapter: {CTR.signal_to_trade}")
    print(f"  Signals → trades opened:         {result.total_trades}")

    # Engine-level signal blocking stats (router / arbiter / risk mgr)
    smc_stats = (result.signal_stats or {}).get("smc", {})
    if smc_stats:
        print()
        print(f"  Engine-level blocks:")
        for k, v in sorted(smc_stats.items(), key=lambda x: -x[1]):
            if v > 0:
                print(f"    {k:<35} {v:>7}")

    # 5. Diagnosis
    print(f"\n{'='*60}")
    print(f"  DIAGNOSIS")
    print(f"{'='*60}")

    if result.total_trades > 0:
        print(f"  OK — SMC produced {result.total_trades} trades with {CTR.signals_produced} signals")
    elif CTR.signal_to_trade > 0 and result.total_trades == 0:
        print(f"  PROBLEM: adapter returned {CTR.signal_to_trade} signals but 0 trades opened")
        print(f"  -> Risk manager / position sizing / _handle_signal blocks execution")
        print(f"  -> Check RiskManager limits or position_weight=0 from router")
    elif CTR.signals_produced > 0 and CTR.signal_to_trade == 0:
        print(f"  PROBLEM: generate_signals_m5 produced {CTR.signals_produced} raw signals")
        print(f"  but adapter.generate_signal returned None for all")
        print(f"  -> Check confidence threshold / direction filter in smc_adapter.py")
    elif CTR.ranging_h1 > 0 and CTR.no_pattern == 0 and CTR.signals_produced == 0:
        pct = CTR.ranging_h1 / max(total_calls, 1) * 100
        print(f"  PROBLEM: H1 RANGING blocks {pct:.0f}% of signal calls")
        print(f"  -> Try --trend up/down with stronger trend, or lower swing_length")
        print(f"  -> In Phase 1 real data: check if market was in long consolidation")
    elif CTR.no_pattern > 0 and CTR.signals_produced == 0:
        print(f"  PROBLEM: H1 not ranging but signal_generator.analyze() returns empty")
        print(f"  -> No BOS/CHoCH detected — check swing_length, min_risk_reward, data range")
        print(f"  -> Try: --rr 1.0 to relax R:R filter")
    elif CTR.warmup_skip == total_calls:
        print(f"  PROBLEM: All {CTR.warmup_skip} calls were in SMC warmup")
        print(f"  -> Increase --bars or reduce --warmup")
    else:
        print(f"  PROBLEM: generate_signal never called after engine warmup")
        print(f"  -> Check engine warmup_bars vs total bars ({bars} bars)")

    print()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SMC Smoke Test — diagnose SMC=0 trades")
    p.add_argument("--bars", type=int, default=2000,
                   help="Number of M5 bars (default: 2000)")
    p.add_argument("--trend", choices=["up", "down", "sideways"], default="up",
                   help="Synthetic data trend (default: up)")
    p.add_argument("--warmup", type=int, default=200,
                   help="Engine warmup bars (default: 200)")
    p.add_argument("--rr", type=float, default=1.5,
                   help="min_risk_reward (default: 1.5)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run_smoke(
        bars=args.bars,
        trend=args.trend,
        engine_warmup=args.warmup,
        rr=args.rr,
    ))
